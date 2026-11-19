"""Persistent TAT watcher. One poll loop over MongoDB -> survives restarts, no per-deal timers to leak."""
import asyncio
import logging
from datetime import timedelta

from pymongo import ReturnDocument

import config
import database as db
import service
from utils import utcnow

log = logging.getLogger(__name__)


async def _process_due(app):
    now = utcnow()
    # crash recovery: claimed but never finished
    await db.deals.update_many(
        {"status": "active", "expiry_notified": True, "expiry_notified_at": {"$exists": False},
         "expiry_claimed_at": {"$lt": now - timedelta(minutes=2)}},
        {"$set": {"expiry_notified": False}},
    )
    while True:
        now = utcnow()
        # atomic claim => exactly one notification per deadline, deals handled independently
        deal = await db.deals.find_one_and_update(
            {
                "status": "active",
                "expiry_notified": False,
                "tat_deadline": {"$lte": now},
                "$or": [{"next_notify_at": {"$exists": False}}, {"next_notify_at": None},
                        {"next_notify_at": {"$lte": now}}],
            },
            {"$set": {"expiry_notified": True, "expiry_claimed_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if not deal:
            return
        try:
            ok = await service.notify_expiry(app, deal)
        except Exception:
            log.exception("notify_expiry crashed for %s", deal["_id"])
            ok = False
        if ok:
            await db.deals.update_one({"_id": deal["_id"], "tat_deadline": deal["tat_deadline"]},
                                      {"$set": {"expiry_notified_at": utcnow()}})
            continue
        retries = deal.get("notify_retries", 0) + 1
        if retries < service.MAX_NOTIFY_RETRIES:
            await db.deals.update_one(
                {"_id": deal["_id"], "tat_deadline": deal["tat_deadline"]},  # don't clobber an extension
                {"$set": {"expiry_notified": False, "notify_retries": retries,
                          "next_notify_at": utcnow() + timedelta(seconds=60 * retries)}},
            )
        else:
            log.error("Giving up expiry notification for deal %s", deal["_id"])
            await db.deals.update_one(
                {"_id": deal["_id"], "tat_deadline": deal["tat_deadline"]},
                {"$set": {"expiry_notified_at": utcnow(), "expiry_notify_failed": True}},
            )


async def check_month_end_report(app):
    """Automatically compiles and sends the previous month's top 10 dealers & referrals report
    to the middleman when the month rolls over in Indian Standard Time (IST)."""
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime
        tz_ist = ZoneInfo(config.DISPLAY_TZ or "Asia/Kolkata")
        now_ist = datetime.now(tz_ist)
        current_month_key = now_ist.strftime("%Y-%m")

        meta = await db.meta.find_one({"_id": "monthly_report_state"})
        if not meta:
            # First initialization: seed with current month key
            await db.meta.update_one(
                {"_id": "monthly_report_state"},
                {"$set": {"last_reported_month": current_month_key}},
                upsert=True,
            )
            return

        last_month = meta.get("last_reported_month")
        if last_month and last_month != current_month_key:
            start_utc, end_utc, prev_month_name = db.get_previous_month_bounds()
            top_dealers = await db.leaderboard_dealers(limit=10, custom_bounds=(start_utc, end_utc))
            top_refs = await db.leaderboard_referrals(limit=10, custom_bounds=(start_utc, end_utc))

            report_lines = [
                f"🎉 <b>Top Users of {prev_month_name}</b>\n",
                f"🎁 <i>Monthly reward time! Top dealers and referrals get a gift from @{config.MM_USERNAME}:</i>\n",
                "🏆 <b>Top 10 Deal Makers:</b>",
            ]
            if not top_dealers:
                report_lines.append("<i>No deals recorded during this month.</i>")
            else:
                for i, (u, uid, cnt) in enumerate(top_dealers, 1):
                    name = f"@{u['username'].lstrip('@')}" if u and u.get("username") else (u.get("first_name") if u and u.get("first_name") else "Dealer")
                    report_lines.append(f"{i}. {name} — {cnt} Deal{'s' if cnt != 1 else ''}")

            report_lines.append("\n👥 <b>Top 10 Referrals:</b>")
            if not top_refs:
                report_lines.append("<i>No referrals recorded during this month.</i>")
            else:
                for i, (u, uid, cnt) in enumerate(top_refs, 1):
                    name = f"@{u['username'].lstrip('@')}" if u and u.get("username") else (u.get("first_name") if u and u.get("first_name") else "Member")
                    report_lines.append(f"{i}. {name} — {cnt} Referral{'s' if cnt != 1 else ''}")

            report_lines.append(f"\n✨ Middleman @{config.MM_USERNAME} can now send rewards to the winners!")
            report_text = "\n".join(report_lines)

            await service._dm_mm(app.bot, report_text)
            await db.meta.update_one(
                {"_id": "monthly_report_state"},
                {"$set": {"last_reported_month": current_month_key, "sent_at": utcnow()}},
                upsert=True,
            )
            log.info("Monthly top users report sent to middleman for month: %s", prev_month_name)
    except Exception as e:
        log.warning("Could not check/send month-end report: %s", e)


async def check_cleanup_reminder(app):
    """Periodically reminds the middleman every 2-3 days if there are closed deal groups
    older than 2 days that have not yet been cleaned up via /dlt."""
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        meta = await db.meta.find_one({"_id": "cleanup_reminder_state"})
        now = utcnow()
        if meta and meta.get("last_reminded_at"):
            last_reminded = meta["last_reminded_at"]
            if now - last_reminded < timedelta(days=2.5):
                return

        old_count = await db.get_old_closed_deals_for_reminder(days=2.0)
        if old_count > 0:
            reminder_text = (
                "🧹 <b>Old Deal Groups Cleanup Reminder</b>\n\n"
                f"You have <b>{old_count}</b> closed deal group(s) older than 2 days waiting for cleanup.\n\n"
                "Bulk cleanup will revoke invite links, remove participants, and have the bot leave the group.\n"
                "Use <code>/dlt</code> or click below to clean them in batches."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🧹 Clean Deal Groups (/dlt)", callback_data="dlt:menu")]
            ])
            await service._dm_mm(app.bot, reminder_text, reply_markup=kb)
            log.info("Sent cleanup reminder to middleman (%d closed groups pending)", old_count)

        await db.meta.update_one(
            {"_id": "cleanup_reminder_state"},
            {"$set": {"last_reminded_at": now}},
            upsert=True,
        )
    except Exception as e:
        log.warning("Could not check/send cleanup reminder: %s", e)


async def run(app):
    log.info("TAT watcher started (poll=%ss)", config.TAT_POLL_SECONDS)
    while True:
        try:
            await _process_due(app)
            await check_month_end_report(app)
            await check_cleanup_reminder(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("TAT loop error")
        await asyncio.sleep(config.TAT_POLL_SECONDS)
