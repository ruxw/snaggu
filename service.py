"""Deal lifecycle: group creation, TAT extension, expiry notification, hold/release, and closure."""
import asyncio
from html import escape as esc
import logging
from datetime import timedelta

from pymongo import ReturnDocument
from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

import config
import database as db
import emojis
import settings
import texts
import userbot
from utils import fmt_dt, fmt_duration, get_currency_symbol, safe_format, user_mention, utcnow

log = logging.getLogger(__name__)
HTML = ParseMode.HTML
MAX_NOTIFY_RETRIES = 10


def ext_keyboard(deal_id) -> InlineKeyboardMarkup:
    mm_url = settings.get("mm_channel_url", config.CHANNEL_URL)
    kw = {"api_kwargs": {"icon_custom_emoji_id": emojis.get_custom_id("CHANNEL")}} if emojis.get_custom_id("CHANNEL") else {}
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Middleman Channel", url=mm_url, **kw)],
    ])


async def safe_send(bot, chat_id, text, **kw) -> bool:
    try:
        await bot.send_message(chat_id, text, parse_mode=HTML, **kw)
        return True
    except TelegramError as e:
        log.warning("send_message to %s failed: %s", chat_id, e)
        return False


async def _send_with_retry(bot, chat_id, text, markup=None, tries=4):
    last = None
    for i in range(tries):
        try:
            return await bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=markup)
        except TelegramError as e:
            last = e
            await asyncio.sleep(2 * (i + 1))
    raise last


async def _dm_mm(bot, text, reply_markup=None) -> bool:
    target = config.MM_USER_ID or userbot.mm_user_id
    if target and await safe_send(bot, target, text, reply_markup=reply_markup):
        return True
    try:
        await userbot.client.send_message("me", emojis.emojify(text), parse_mode="html")
        return True
    except Exception:
        log.exception("Fallback DM to middleman failed")
        return False


async def apply_gc_photo(bot, chat_id: int):
    """Applies the configured GC profile photo to a newly created deal group."""
    file_id = settings.get("gc_photo_file_id")
    if not file_id:
        return
    try:
        f = await bot.get_file(file_id)
        photo_bytes = await f.download_as_bytearray()
        await bot.set_chat_photo(chat_id=chat_id, photo=bytes(photo_bytes))
        log.info("Applied GC profile photo to chat %s", chat_id)
    except Exception as e:
        log.warning("Could not set GC photo for chat %s: %s", chat_id, e)


async def finalize_deal(app, deal):
    bot = app.bot
    label = texts.deal_label(deal)
    info = None
    try:
        gc_title = settings.render_gc_title(
            label,
            deal.get("buyer_username") or "Buyer",
            deal.get("seller_username") or "Seller",
            deal.get("amount") or "",
            deal_no=deal.get("deal_no"),
        )
        info = await userbot.create_deal_group(gc_title, bot.username)
        await apply_gc_photo(bot, info.chat_id)

        deadline = utcnow() + timedelta(seconds=deal["tat_seconds"])
        deal["tat_deadline"] = deadline
        deal["invite_link"] = info.invite_link
        cid = emojis.get_custom_id("SUCCESS")
        btn_kw = {"api_kwargs": {"style": "success", "icon_custom_emoji_id": str(cid)}} if cid else {"api_kwargs": {"style": "success"}}
        confirm_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Confirm Deal", callback_data=f"inst:confirm:{deal['deal_no']}", **btn_kw),
                InlineKeyboardButton("✏️ Edit Deal", callback_data=f"editdeal:menu:{deal['deal_no']}"),
            ]
        ])
        msg = await _send_with_retry(bot, info.chat_id, texts.pinned_text(deal), confirm_kb)
        try:
            await bot.pin_chat_message(info.chat_id, msg.message_id, disable_notification=True)
        except TelegramError:
            log.warning("Could not pin message in %s", info.chat_id)

        await db.deals.update_one({"_id": deal["_id"]}, {"$set": {
            "status": "instant_confirmation_pending",
            "group_chat_id": info.chat_id,
            "invite_link": info.invite_link,
            "pinned_message_id": msg.message_id,
            "pinned_deal_info_id": msg.message_id,
            "tat_started_at": utcnow(),
            "tat_deadline": deadline,
            "expiry_notified": False,
            "notify_retries": 0,
            "extensions": [],
        }})
    except Exception:
        log.exception("Deal %s: group creation failed", label)
        await db.deals.update_one({"_id": deal["_id"]}, {"$set": {"status": "failed"}})
        for uid in (deal["buyer_id"], deal["seller_id"]):
            await safe_send(bot, uid, "❌ Couldn't create the deal group. Please try again with /deal.")
        extra = f"\nGC Link: {info.invite_link}" if info else ""
        await _dm_mm(bot, f"⚠️ Group creation failed for Deal {label} "
                          f"(@{deal['buyer_username']} / @{deal['seller_username']}).{extra}")
        return

    for uid in (deal["buyer_id"], deal["seller_id"]):
        await safe_send(bot, uid, texts.gc_created_text(info.invite_link))
    await _dm_mm(bot, texts.mm_dm_text(deal))


async def init_instant_deal_room(app, deal):
    bot = app.bot
    chat_id = deal["group_chat_id"]

    # Apply configured GC profile photo if available
    await apply_gc_photo(bot, chat_id)

    dno = deal["deal_no"]
    b_id = emojis.get_custom_id("BUYER")
    s_id = emojis.get_custom_id("SELLER")
    b_kw = {"style": "primary"}
    if b_id:
        b_kw["icon_custom_emoji_id"] = b_id
    s_kw = {"style": "success"}
    if s_id:
        s_kw["icon_custom_emoji_id"] = s_id
    b_text = "Buyer" if b_id else "🛒 Buyer"
    s_text = "Seller" if s_id else "💼 Seller"
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(b_text, callback_data=f"inst:role:buyer:{dno}", api_kwargs=b_kw),
            InlineKeyboardButton(s_text, callback_data=f"inst:role:seller:{dno}", api_kwargs=s_kw),
        ]
    ])

    welcome_text = (
        f"🤝 <b>Deal #{config.DEAL_ID_PREFIX}{dno}</b>\n\n"
        f"Escrower: @{config.MM_USERNAME}\n\n"
        "Both parties, confirm your roles below:"
    )

    try:
        pinned_msg = await bot.send_message(chat_id, welcome_text, reply_markup=kb, parse_mode=HTML)
        await bot.pin_chat_message(chat_id, pinned_msg.message_id, disable_notification=True)
        await db.update_deal(deal["_id"], {"pinned_role_message_id": pinned_msg.message_id})
    except Exception as e:
        log.warning("Could not pin role message: %s", e)


async def notify_expiry(app, deal) -> bool:
    bot = app.bot
    text = texts.expiry_text(deal)
    in_group = await safe_send(bot, deal["group_chat_id"], text)

    # Dedicated DM to middleman with GC link
    dno = deal.get("deal_no", "")
    amt = deal.get("amount", "")
    curr = deal.get("currency", "INR")
    tat_dur = fmt_duration(deal.get("tat_seconds", 0))
    gc_link = deal.get("invite_link")
    if not gc_link:
        try:
            gc_link = await bot.export_chat_invite_link(deal["group_chat_id"])
            await db.update_deal(deal["_id"], {"invite_link": gc_link})
        except Exception:
            pass

    b_un = deal.get("buyer_username")
    s_un = deal.get("seller_username")
    b_tag = f"@{esc(b_un)}" if b_un else "Buyer"
    s_tag = f"@{esc(s_un)}" if s_un else "Seller"

    dm_text = (
        "⚠️ <b>TAT Expired — Action Required</b>\n\n"
        f"📦 <b>Deal:</b> #{config.DEAL_ID_PREFIX}{dno}\n"
        f"💰 <b>Amount:</b> {esc(str(amt))} {esc(str(curr))}\n"
        f"⏱ <b>TAT Duration:</b> {esc(tat_dur)}\n"
        f"👤 <b>Buyer:</b> {b_tag} | <b>Seller:</b> {s_tag}\n\n"
    )
    if gc_link:
        dm_text += f"🔗 <b>Group Chat:</b> {gc_link}\n\n"
    dm_text += "Turnaround time has expired. Please check the deal group."

    kb = None
    if gc_link:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Open Deal Group", url=gc_link)]
        ])

    in_dm = await _dm_mm(bot, dm_text, reply_markup=kb)
    return in_group or in_dm


async def set_deal_title_hold(bot, deal) -> str:
    amt = str(deal.get("amount") or deal.get("deal_no") or "")
    curr_sym = get_currency_symbol(deal.get("currency", "INR"))
    new_title = f"MM {amt} {curr_sym} hold"
    try:
        await bot.set_chat_title(deal["group_chat_id"], new_title[:128])
    except TelegramError as e:
        log.warning("Could not set chat title to '%s': %s", new_title, e)
    return new_title


async def set_deal_title_unhold(bot, deal) -> str:
    label = texts.deal_label(deal)
    new_title = deal.get("prev_title") or settings.render_gc_title(
        label,
        deal.get("buyer_username") or "Buyer",
        deal.get("seller_username") or "Seller",
        deal.get("amount") or "",
        deal_no=deal.get("deal_no"),
    )
    try:
        await bot.set_chat_title(deal["group_chat_id"], new_title[:128])
    except TelegramError as e:
        log.warning("Could not set chat title to '%s': %s", new_title, e)
    return new_title


async def set_deal_title_release(bot, deal) -> str:
    amt = str(deal.get("amount") or deal.get("deal_no") or "")
    curr_sym = get_currency_symbol(deal.get("currency", "INR"))
    new_title = f"MM {amt} {curr_sym} Released"
    try:
        await bot.set_chat_title(deal["group_chat_id"], new_title[:128])
    except TelegramError as e:
        log.warning("Could not set chat title to '%s': %s", new_title, e)
    return new_title


async def revoke_deal_invite(bot, deal):
    chat_id = deal.get("group_chat_id")
    link = deal.get("invite_link")
    if chat_id and link and not deal.get("invite_link_revoked"):
        try:
            await bot.revoke_chat_invite_link(chat_id, link)
            await db.update_deal(deal["_id"], {"invite_link_revoked": True})
            log.info("Invite link revoked for deal %s", deal.get("deal_no"))
        except Exception as e:
            log.warning("Could not revoke invite link for deal %s: %s", deal.get("deal_no"), e)


async def mute_deal_participants(bot, chat_id, buyer_id: int = None, seller_id: int = None):
    mute_perm = ChatPermissions(
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
        can_change_info=False,
        can_invite_users=False,
        can_pin_messages=False,
    )
    mm_ids = {userbot.mm_user_id, config.MM_USER_ID} | getattr(config, "MM_USER_IDS", set())
    for uid in (buyer_id, seller_id):
        if uid and uid not in mm_ids:
            try:
                await bot.restrict_chat_member(chat_id, uid, permissions=mute_perm)
            except Exception as e:
                log.warning("Could not mute user %s: %s", uid, e)


async def start_deal_tat(bot, deal, user_id: int):
    now = utcnow()
    tat_seconds = deal.get("tat_seconds")
    deadline = now + timedelta(seconds=tat_seconds)

    await db.update_deal(deal["_id"], {
        "status": "active",
        "payment_status": "confirmed",
        "tat_started_at": now,
        "tat_deadline": deadline,
        "expiry_notified": False,
        "notify_retries": 0,
    })
    await db.record_deal_audit(deal["_id"], "begin", user_id)

    chat_id = deal["group_chat_id"]
    buyer_tag = user_mention(deal.get("buyer_id", 0), deal.get("buyer_username"))
    seller_tag = user_mention(deal.get("seller_id", 0), deal.get("seller_username"))

    # 1. Message: Payment confirmed
    begin_msg = settings.render_template("payment_confirmed", buyer=buyer_tag, seller=seller_tag)
    await safe_send(bot, chat_id, begin_msg)
    await asyncio.sleep(0.5)

    # 2. Compute dynamic native Telegram date_time entities (Bot API 8.0+)
    # Telegram client updates relative countdown dynamically on screen without message spam
    deadline_ts = int(deadline.timestamp())
    timer_fallback = _compute_timer(tat_seconds)
    dynamic_timer = f'<tg-time unix="{deadline_ts}" format="r">{timer_fallback}</tg-time>'
    dynamic_deadline = f'<tg-time unix="{deadline_ts}">{fmt_dt(deadline)}</tg-time>'

    # 3. Countdown message with native dynamic timestamp
    countdown_msg = settings.render_template(
        "tat_notification",
        deal_id=str(deal.get("deal_no", "")),
        buyer=buyer_tag,
        seller=seller_tag,
        amount=str(deal.get("amount", "0")),
        currency=str(deal.get("currency", "")),
        tat=fmt_duration(tat_seconds),
        deadline=dynamic_deadline,
        timer=dynamic_timer,
        deadline_unix=str(deadline_ts),
    )
    mm_url = settings.get("mm_channel_url", config.CHANNEL_URL)
    cid = emojis.get_custom_id("CHANNEL")
    btn_kw = {"api_kwargs": {"icon_custom_emoji_id": cid}} if cid else {}
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Middleman Channel", url=mm_url, **btn_kw)],
        [InlineKeyboardButton("⏱ Edit TAT", callback_data=f"tatedit:menu:{deal.get('deal_no')}")],
    ])
    sent_card = await bot.send_message(chat_id, countdown_msg, parse_mode=HTML, reply_markup=kb)
    try:
        await bot.pin_chat_message(chat_id, sent_card.message_id, disable_notification=True)
    except Exception:
        pass

    await db.update_deal(deal["_id"], {
        "tat_message_id": sent_card.message_id,
        "pinned_message_id": sent_card.message_id,
    })


def _compute_timer(remaining_seconds: int) -> str:
    """Compute a compact timer string like '23h 59m' or '1d 12h'."""
    if remaining_seconds <= 0:
        return "Expired"
    days = remaining_seconds // 86400
    hours = (remaining_seconds % 86400) // 3600
    mins = (remaining_seconds % 3600) // 60
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if mins > 0 and days == 0:
        parts.append(f"{mins}m")
    return " ".join(parts) or "< 1m"


async def extend_deal_tat(bot, deal, ext_seconds: int, duration_str: str, user_id: int):
    now = utcnow()
    current_deadline = deal.get("tat_deadline") or now
    new_deadline = max(current_deadline, now) + timedelta(seconds=ext_seconds)
    new_tat_secs = deal.get("tat_seconds", 0) + ext_seconds

    audit_entry = {
        "by": user_id,
        "seconds": ext_seconds,
        "duration": duration_str,
        "at": now,
        "from": current_deadline,
        "to": new_deadline,
    }

    await db.deals.update_one(
        {"_id": deal["_id"]},
        {
            "$set": {
                "tat_deadline": new_deadline,
                "tat_seconds": new_tat_secs,
                "expiry_notified": False,
                "notify_retries": 0,
            },
            "$push": {"extensions": audit_entry},
            "$unset": {"expiry_notified_at": "", "expiry_claimed_at": "", "next_notify_at": "", "expiry_notify_failed": ""},
        },
    )

    chat_id = deal["group_chat_id"]
    new_deadline_ts = int(new_deadline.timestamp())
    dynamic_new_timer = f'<tg-time unix="{new_deadline_ts}" format="r">+{duration_str}</tg-time>'
    dynamic_new_deadline = f'<tg-time unix="{new_deadline_ts}">{fmt_dt(new_deadline)}</tg-time>'

    ext_msg = settings.render_template(
        "tat_extension",
        duration=duration_str,
        deadline=dynamic_new_deadline,
        deal_id=str(deal.get("deal_no", "")),
        timer=dynamic_new_timer,
        deadline_unix=str(new_deadline_ts),
    )
    await safe_send(bot, chat_id, ext_msg)

    # Update pinned countdown message if it exists
    tat_msg_id = deal.get("tat_message_id") or deal.get("pinned_message_id")
    if tat_msg_id:
        buyer_tag = user_mention(deal.get("buyer_id", 0), deal.get("buyer_username"))
        seller_tag = user_mention(deal.get("seller_id", 0), deal.get("seller_username"))
        remaining = int((new_deadline - now).total_seconds())
        timer_fallback = _compute_timer(remaining)
        dynamic_timer = f'<tg-time unix="{new_deadline_ts}" format="r">{timer_fallback}</tg-time>'
        dynamic_deadline = f'<tg-time unix="{new_deadline_ts}">{fmt_dt(new_deadline)}</tg-time>'

        updated_card = settings.render_template(
            "tat_notification",
            deal_id=str(deal.get("deal_no", "")),
            buyer=buyer_tag,
            seller=seller_tag,
            amount=str(deal.get("amount", "0")),
            currency=str(deal.get("currency", "")),
            tat=fmt_duration(new_tat_secs),
            deadline=dynamic_deadline,
            timer=dynamic_timer,
            deadline_unix=str(new_deadline_ts),
        )
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=tat_msg_id,
                text=updated_card,
                parse_mode=HTML,
            )
        except Exception as e:
            log.warning("Could not refresh pinned countdown card: %s", e)


async def recover_interrupted(app):
    cur = db.deals.find({"status": "creating"})
    async for d in cur:
        await db.deals.update_one({"_id": d["_id"]}, {"$set": {"status": "failed"}})
        log.warning("Deal %s was interrupted during group creation; marked failed", d["_id"])
        for uid in (d["buyer_id"], d["seller_id"]):
            await safe_send(app.bot, uid, "❌ Couldn't create the deal group. Please try again with /deal.")
