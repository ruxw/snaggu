"""MongoDB layer (motor). All datetimes are stored in UTC (tz-aware)."""
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

import config
from utils import utcnow

log = logging.getLogger(__name__)

# A deal counts for /deals and /leaderboard once its group was created.
# (TAT expiry never changes status, so expired deals stay "active" until handled.)
COUNTED = ["active", "completed", "held", "released", "closed", "deal_confirmed"]
SESSION_TTL = timedelta(seconds=config.SESSION_TIMEOUT_SECONDS)

client = None
users = sessions = deals = referrals = counters = callbacks = meta = settings = livechats = livechat_relays = broadcasts = None


async def init():
    global client, users, sessions, deals, referrals, counters, callbacks, meta, settings, livechats, livechat_relays, broadcasts
    client = AsyncIOMotorClient(config.MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=15000)
    database = client[config.MONGO_DB]
    users, sessions, deals = database.users, database.sessions, database.deals
    referrals, counters = database.referrals, database.counters
    callbacks, meta = database.processed_callbacks, database.meta
    settings, livechats, livechat_relays = database.settings, database.livechats, database.livechat_relays
    broadcasts = database.broadcasts

    await client.admin.command("ping")
    await users.create_index("username_lower")
    await users.create_index("ref_code", unique=True, sparse=True)
    await sessions.create_index("expires_at", expireAfterSeconds=0)  # TTL cleanup
    await deals.create_index("deal_no", unique=True, sparse=True)
    await deals.create_index([("status", 1), ("tat_deadline", 1)])
    await deals.create_index("buyer_id")
    await deals.create_index("seller_id")
    await deals.create_index("group_chat_id")
    await callbacks.create_index("created_at", expireAfterSeconds=86400)
    await livechats.create_index("user_id")
    await livechat_relays.create_index("created_at", expireAfterSeconds=7 * 86400)
    log.info("MongoDB connected (db=%s)", config.MONGO_DB)


async def close():
    if client:
        client.close()


# ---------------------------------------------------------------- users
async def upsert_user(u) -> bool:
    """Create/update the user. Returns True if the user is new."""
    uname = u.username
    for _ in range(5):
        try:
            res = await users.update_one(
                {"_id": u.id},
                {
                    "$set": {
                        "username": uname,
                        "username_lower": uname.lower() if uname else None,
                        "first_name": u.first_name,
                        "last_seen": utcnow(),
                    },
                    "$setOnInsert": {
                        "points": 0,
                        "ref_code": secrets.token_hex(4),
                        "joined_at": utcnow(),
                    },
                },
                upsert=True,
            )
            return res.upserted_id is not None
        except DuplicateKeyError:
            continue
    raise RuntimeError("Could not upsert user")


async def get_user(uid: int):
    return await users.find_one({"_id": uid})


async def find_user_by_username(username: str):
    return await users.find_one({"username_lower": username.lower()}, sort=[("last_seen", -1)])


async def process_referral(new_user_id: int, code: str):
    """+2 points to the referrer, once per referred user (unique _id on referrals)."""
    referrer = await users.find_one({"ref_code": code})
    if not referrer or referrer["_id"] == new_user_id:
        return
    try:
        await referrals.insert_one({"_id": new_user_id, "referrer_id": referrer["_id"], "at": utcnow()})
    except DuplicateKeyError:
        return
    await users.update_one({"_id": referrer["_id"]}, {"$inc": {"points": 2}})


# ------------------------------------------------------------- sessions
async def start_session(uid: int, msg_ids: list):
    await sessions.replace_one(
        {"_id": uid},
        {"_id": uid, "step": "type", "data": {}, "msg_ids": msg_ids, "expires_at": utcnow() + SESSION_TTL},
        upsert=True,
    )


async def get_session(uid: int):
    s = await sessions.find_one({"_id": uid})
    if not s:
        return None
    if s["expires_at"] <= utcnow():  # TTL monitor runs only every ~60s
        await sessions.delete_one({"_id": uid})
        return None
    return s


async def update_session(uid: int, *, step=None, data=None, push_ids=None, msg_ids=None):
    sets = {"expires_at": utcnow() + SESSION_TTL}  # sliding 10-minute inactivity window
    if step:
        sets["step"] = step
    for k, v in (data or {}).items():
        sets[f"data.{k}"] = v
    if msg_ids is not None:
        sets["msg_ids"] = msg_ids
    upd = {"$set": sets}
    if push_ids:
        upd["$push"] = {"msg_ids": {"$each": push_ids}}
    await sessions.update_one({"_id": uid}, upd)


async def delete_session(uid: int):
    await sessions.delete_one({"_id": uid})


# ---------------------------------------------------------------- deals
async def get_current_deal_no() -> int:
    """Returns the current deal counter sequence without incrementing."""
    if counters is None:
        return 0
    doc = await counters.find_one({"_id": "deal"})
    return int(doc["seq"]) if doc and "seq" in doc else 0


async def next_deal_no() -> int:
    r = await counters.find_one_and_update(
        {"_id": "deal"}, {"$inc": {"seq": 1}}, upsert=True, return_document=ReturnDocument.AFTER
    )
    return r["seq"]


async def claim_pending(deal_oid, by_id: int):
    """Atomically pending -> creating (only the intended party, only once)."""
    deal = await deals.find_one_and_update(
        {"_id": deal_oid, "status": "pending", "other_id": by_id},
        {"$set": {"status": "creating", "accepted_at": utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if not deal:
        return None
    n = await next_deal_no()
    await deals.update_one({"_id": deal_oid}, {"$set": {"deal_no": n}})
    deal["deal_no"] = n
    return deal


async def count_user_deals(uid: int) -> int:
    return await deals.count_documents(
        {"status": {"$in": COUNTED}, "$or": [{"buyer_id": uid}, {"seller_id": uid}]}
    )


def get_ist_month_bounds() -> tuple:
    """Returns (start_utc, end_utc) for the current month in Indian Standard Time (IST)."""
    tz_ist = ZoneInfo(config.DISPLAY_TZ or "Asia/Kolkata")
    now_ist = datetime.now(tz_ist)
    start_ist = datetime(now_ist.year, now_ist.month, 1, 0, 0, 0, tzinfo=tz_ist)
    if now_ist.month == 12:
        end_ist = datetime(now_ist.year + 1, 1, 1, 0, 0, 0, tzinfo=tz_ist)
    else:
        end_ist = datetime(now_ist.year, now_ist.month + 1, 1, 0, 0, 0, tzinfo=tz_ist)
    return (
        start_ist.astimezone(timezone.utc).replace(tzinfo=None),
        end_ist.astimezone(timezone.utc).replace(tzinfo=None),
    )


def get_previous_month_bounds() -> tuple:
    """Returns (start_utc, end_utc, month_name) for the previous month in IST."""
    tz_ist = ZoneInfo(config.DISPLAY_TZ or "Asia/Kolkata")
    now_ist = datetime.now(tz_ist)
    curr_month_start = datetime(now_ist.year, now_ist.month, 1, 0, 0, 0, tzinfo=tz_ist)
    prev_month_end = curr_month_start
    if now_ist.month == 1:
        prev_month_start = datetime(now_ist.year - 1, 12, 1, 0, 0, 0, tzinfo=tz_ist)
    else:
        prev_month_start = datetime(now_ist.year, now_ist.month - 1, 1, 0, 0, 0, tzinfo=tz_ist)
    return (
        prev_month_start.astimezone(timezone.utc).replace(tzinfo=None),
        prev_month_end.astimezone(timezone.utc).replace(tzinfo=None),
        prev_month_start.strftime("%B %Y"),
    )


async def leaderboard_dealers(limit: int = 10, monthly: bool = False, custom_bounds: tuple = None):
    match_q: Dict[str, Any] = {"status": {"$in": COUNTED}}
    if custom_bounds:
        start_utc, end_utc = custom_bounds
        match_q["created_at"] = {"$gte": start_utc, "$lt": end_utc}
    elif monthly:
        start_utc, end_utc = get_ist_month_bounds()
        match_q["created_at"] = {"$gte": start_utc, "$lt": end_utc}

    pipeline = [
        {"$match": match_q},
        {"$project": {"p": ["$buyer_id", "$seller_id"]}},
        {"$unwind": "$p"},
        {"$match": {"p": {"$ne": None}}},
        {"$group": {"_id": "$p", "n": {"$sum": 1}}},
        {"$sort": {"n": -1, "_id": 1}},
        {"$limit": limit},
    ]
    rows = await deals.aggregate(pipeline).to_list(limit)
    out = []
    for r in rows:
        uid = r["_id"]
        u = await users.find_one({"_id": uid})
        if not u or (not u.get("username") and not u.get("first_name")):
            d = await deals.find_one(
                {"$or": [{"buyer_id": uid}, {"seller_id": uid}]},
                sort=[("created_at", -1)],
            )
            if d:
                un = None
                if d.get("buyer_id") == uid:
                    un = d.get("buyer_username")
                elif d.get("seller_id") == uid:
                    un = d.get("seller_username")
                if un:
                    un = un.lstrip("@")
                    if not u:
                        u = {"_id": uid, "username": un}
                    elif not u.get("username"):
                        u["username"] = un
        out.append((u, uid, r["n"]))
    return out


async def leaderboard_referrals(limit: int = 10, monthly: bool = False, custom_bounds: tuple = None):
    match_q: Dict[str, Any] = {}
    if custom_bounds:
        start_utc, end_utc = custom_bounds
        match_q["at"] = {"$gte": start_utc, "$lt": end_utc}
    elif monthly:
        start_utc, end_utc = get_ist_month_bounds()
        match_q["at"] = {"$gte": start_utc, "$lt": end_utc}

    pipeline = []
    if match_q:
        pipeline.append({"$match": match_q})
    pipeline.extend([
        {"$group": {"_id": "$referrer_id", "n": {"$sum": 1}}},
        {"$sort": {"n": -1, "_id": 1}},
        {"$limit": limit},
    ])
    rows = await referrals.aggregate(pipeline).to_list(limit)
    out = []
    for r in rows:
        u = await users.find_one({"_id": r["_id"]})
        out.append((u, r["_id"], r["n"]))
    return out


async def leaderboard(limit: int = 10):
    return await leaderboard_dealers(limit=limit, monthly=False)


# ------------------------------------------------------------- livechat
async def get_active_livechat(uid: int):
    return await livechats.find_one({"user_id": uid, "active": True})


async def start_livechat(uid: int, username: str, first_name: str):
    await livechats.update_one(
        {"user_id": uid},
        {
            "$set": {
                "user_id": uid,
                "username": username,
                "first_name": first_name,
                "active": True,
                "started_at": utcnow(),
                "last_activity": utcnow(),
            }
        },
        upsert=True,
    )


async def end_livechat(uid: int):
    await livechats.update_one(
        {"user_id": uid},
        {"$set": {"active": False, "ended_at": utcnow()}},
    )


async def map_livechat_relay(bot_msg_id: int, user_id: int, user_msg_id: int):
    await livechat_relays.insert_one({
        "_id": bot_msg_id,
        "user_id": user_id,
        "user_msg_id": user_msg_id,
        "created_at": utcnow(),
    })


async def get_livechat_relay(bot_msg_id: int):
    return await livechat_relays.find_one({"_id": bot_msg_id})


# -------------------------------------------------------- group / instant deals
async def get_deal_by_group(group_chat_id: int):
    return await deals.find_one({"group_chat_id": group_chat_id})


async def get_deal_by_no(deal_no: int):
    return await deals.find_one({"deal_no": deal_no})


async def update_deal(deal_id, updates: dict):
    return await deals.update_one({"_id": deal_id}, {"$set": updates})


async def record_deal_audit(deal_id, action: str, actor_id: int, details: dict = None):
    entry = {
        "action": action,
        "actor_id": actor_id,
        "details": details or {},
        "at": utcnow(),
    }
    await deals.update_one({"_id": deal_id}, {"$push": {"status_history": entry}})


async def close_deal(deal_id, closed_by: int):
    now = utcnow()
    res = await deals.find_one_and_update(
        {"_id": deal_id, "status": {"$ne": "closed"}},
        {
            "$set": {
                "status": "closed",
                "closed_at": now,
                "closed_by": closed_by,
                "tat_active": False,
            },
            "$push": {
                "status_history": {
                    "action": "closed",
                    "actor_id": closed_by,
                    "at": now,
                }
            },
        },
        return_document=ReturnDocument.AFTER,
    )
    return res



# ----------------------------------------------------------------- stats
async def get_full_stats() -> dict:
    from datetime import timedelta
    now = utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    total_deals = await deals.count_documents({})
    active_deals = await deals.count_documents({"status": "active"})
    completed_deals = await deals.count_documents({"status": "completed"})
    pending_deals = await deals.count_documents({"status": {"$in": ["pending", "instant_role_selection", "instant_details_pending"]}})
    cancelled_deals = await deals.count_documents({"status": {"$in": ["rejected", "failed"]}})

    total_users = await users.count_documents({})
    users_today = await users.count_documents({"joined_at": {"$gte": today_start}})
    active_livechats = await livechats.count_documents({"active": True})

    pipeline = [
        {"$group": {"_id": None, "total_points": {"$sum": "$points"}}}
    ]
    points_res = await users.aggregate(pipeline).to_list(1)
    total_points = points_res[0]["total_points"] if points_res else 0

    return {
        "total_deals": total_deals,
        "active_deals": active_deals,
        "completed_deals": completed_deals,
        "pending_deals": pending_deals,
        "cancelled_deals": cancelled_deals,
        "total_users": total_users,
        "users_today": users_today,
        "active_livechats": active_livechats,
        "total_points": total_points,
    }


# ----------------------------------------------------------------- broadcast
async def count_broadcast_targets() -> tuple:
    user_cnt = await users.count_documents({})
    gc_ids = await deals.distinct("group_chat_id", {
        "group_chat_id": {"$exists": True, "$ne": None},
        "cleaned": {"$ne": True},
    })
    return user_cnt, len(gc_ids)


async def get_broadcast_targets(target_type: str = "both") -> list:
    targets = []
    if target_type in ("users", "both"):
        cursor = users.find({}, {"_id": 1, "first_name": 1, "username": 1})
        async for u in cursor:
            targets.append({
                "id": u["_id"],
                "name": u.get("first_name") or "User",
                "username": u.get("username"),
                "is_group": False,
            })
    if target_type in ("groups", "both"):
        cursor = deals.find({
            "group_chat_id": {"$exists": True, "$ne": None},
            "cleaned": {"$ne": True},
        }, {"group_chat_id": 1, "deal_no": 1})
        seen_gids = set()
        async for d in cursor:
            gid = d.get("group_chat_id")
            if gid and gid not in seen_gids:
                seen_gids.add(gid)
                targets.append({
                    "id": gid,
                    "name": f"Deal #{d.get('deal_no', 'GC')}",
                    "username": None,
                    "is_group": True,
                })
    return targets


async def record_broadcast(doc: dict) -> Any:
    if broadcasts is not None:
        return await broadcasts.insert_one(doc)
    return None


# ----------------------------------------------------------------- cleanup (/dlt)
async def get_closed_deals_pending_cleanup_count() -> int:
    return await deals.count_documents({
        "status": "closed",
        "group_chat_id": {"$exists": True, "$ne": None},
        "cleaned": {"$ne": True},
    })


async def get_closed_deals_pending_cleanup(limit: Optional[int] = None) -> list:
    query = {
        "status": "closed",
        "group_chat_id": {"$exists": True, "$ne": None},
        "cleaned": {"$ne": True},
    }
    cursor = deals.find(query).sort("closed_at", 1)
    if limit:
        cursor = cursor.limit(limit)
    return await cursor.to_list(length=limit or 1000)


async def mark_deal_cleaned(deal_id) -> None:
    await deals.update_one(
        {"_id": deal_id},
        {"$set": {"cleaned": True, "cleaned_at": utcnow()}},
    )


async def get_old_closed_deals_for_reminder(days: float = 2.0) -> int:
    cutoff = utcnow() - timedelta(days=days)
    return await deals.count_documents({
        "status": "closed",
        "group_chat_id": {"$exists": True, "$ne": None},
        "cleaned": {"$ne": True},
        "closed_at": {"$lte": cutoff},
    })


