import asyncio
import difflib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo
from html import escape as _stdlib_esc


def esc(s, quote=True) -> str:
    """Safe HTML escape that never crashes on None or non-string inputs."""
    if s is None:
        return ""
    return _stdlib_esc(str(s), quote=quote)

from bson import ObjectId
from bson.errors import InvalidId
from pymongo.errors import DuplicateKeyError
from telegram import (
    ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
    KeyboardButtonRequestUsers, LinkPreviewOptions, MessageOriginChannel, MessageOriginChat,
    MessageOriginHiddenUser, MessageOriginUser, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

import ai_parser
import config
import database as db
import emojis
import service
import settings
import texts
import userbot
import utils
from utils import (
    fmt_dt, fmt_duration, get_currency_symbol, parse_amount, parse_channel_target, parse_tat,
    parse_username, safe_format, user_mention, utcnow,
)

log = logging.getLogger(__name__)
HTML = ParseMode.HTML
BLUE, GREEN, RED = "primary", "success", "danger"   # Bot API button styles
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
_tasks: set = set()

# In-memory admin settings prompt state {user_id: {"action": "...", "data": "..."}}
_admin_sessions = {}

# In-memory group edit state {(chat_id, user_id): {"action": "...", "deal_no": 123}}
_group_edit_sessions = {}


def _spawn(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def _clean_btn_text(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    # Strip HTML tags (e.g. <tg-emoji emoji-id="...">📢</tg-emoji> -> 📢)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _btn(text, data=None, color=None, url=None, icon=None):
    text = _clean_btn_text(text)
    stripped, detected_icon = emojis.split_leading_emoji(text)
    if icon:
        text = emojis.strip_leading_emoji(text)
    elif detected_icon:
        text = stripped
        icon = detected_icon
    api_kw = {}
    if color:
        api_kw["style"] = color
    if icon:
        api_kw["icon_custom_emoji_id"] = str(icon)
    kw = {"api_kwargs": api_kw} if api_kw else {}
    if url:
        return InlineKeyboardButton(text, url=url, **kw)
    return InlineKeyboardButton(text, callback_data=data, **kw)


def _kb(*btns):  # always one button per row
    return InlineKeyboardMarkup([[b] for b in btns])


def _is_mm(user_id: int, username: str = None) -> bool:
    if user_id in config.MM_USER_IDS:
        return True
    if config.MM_USER_ID and user_id == config.MM_USER_ID:
        return True
    if userbot.mm_user_id and user_id == userbot.mm_user_id:
        return True
    if username and username.lower().lstrip("@") == config.MM_USERNAME.lower():
        return True
    return False


def _parse_ex_value(text: str) -> str:
    """Normalise an exchange value like '500inr', '50INR', '5$usdt', '5$ usdt', '10 BTC' -> '500 INR', '5 $USDT', etc.
    Returns a cleaned, human-readable string that the user can confirm."""
    import re as _re
    text = text.strip()
    # Case: number followed by $ (or other symbol) then optional currency letters — e.g. "5$ usdt", "5$usdt"
    m0 = _re.match(r'^(\d+(?:\.\d+)?)\s*([€$₹£¥])\s*([a-zA-Z]+)?$', text)
    if m0:
        num, sym, label = m0.group(1), m0.group(2), (m0.group(3) or "").strip()
        return f"{num} {sym}{label.upper()}" if label else f"{num} {sym}"
    # Case: number followed directly by alpha currency — "500inr", "10USDT", "1BTC"
    m1 = _re.match(r'^(\d+(?:\.\d+)?)\s+([a-zA-Z]+.*)$', text)
    m1b = _re.match(r'^(\d+(?:\.\d+)?)([a-zA-Z]+.*)$', text)
    m = m1 or m1b
    if m:
        num, label = m.group(1), m.group(2).strip()
        return f"{num} {label.upper()}" if label.isalpha() else f"{num} {label}"
    # Case: currency-symbol first — "$50", "₹500", "$50 usdt"
    m2 = _re.match(r'^([€$₹£¥])(\d+(?:\.\d+)?)\s*([a-zA-Z]*)$', text)
    if m2:
        sym, num, label = m2.group(1), m2.group(2), m2.group(3).strip()
        return f"{num} {sym}{label.upper()}" if label else f"{num} {sym}"
    # Fallback: return as-is but stripped
    return text


# ------------------------------------------------------------------ /start
async def start_cmd(update, ctx):
    user = update.effective_user
    is_new = await db.upsert_user(user)
    if is_new and ctx.args and ctx.args[0].startswith("ref_"):
        await db.process_referral(user.id, ctx.args[0][4:])

    # Buttonised main menu
    kb = InlineKeyboardMarkup([
        [
            _btn("🤝 Start Deal", "menu:start_deal", GREEN, icon=emojis.get_custom_id("DEALS")),
            _btn("💬 Support / Chat", "lc:start", BLUE, icon=emojis.get_custom_id("CHAT")),
        ],
        [
            _btn("📢 Channel", color=BLUE, url=settings.get("mm_channel_url", config.CHANNEL_URL), icon=emojis.get_custom_id("CHANNEL")),
            _btn("⭐️ Vouches", color=BLUE, url=settings.get("vouch_channel_url", config.CHANNEL_URL), icon=emojis.get_custom_id("VOUCH")),
        ],
        [
            _btn("💫 Referral Program", "ref", GREEN, icon=emojis.get_custom_id("REF")),
        ],
    ])

    await update.message.reply_text(
        texts.welcome(),
        reply_markup=kb,
        parse_mode=HTML,
        link_preview_options=NO_PREVIEW,
    )


async def menu_cb(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.data == "menu:start_deal":
        user = q.from_user
        await db.upsert_user(user)
        if not user.username:
            await q.message.reply_text("⚠️ Please set a Telegram username in your profile settings, then send /deal again.")
            return
        sent = await q.message.reply_text(
            "<b>Select Deal Type</b>",
            reply_markup=_kb(
                _btn("🤝 P2P", "d:type:p2p", BLUE),
                _btn("🔄 Exchange", "d:type:exchange", GREEN, icon=emojis.get_custom_id("EXCHANGE")),
                _btn("❌ Cancel", "d:cancel", RED),
            ),
            parse_mode=HTML,
        )
        await db.start_session(user.id, [q.message.message_id, sent.message_id])
    elif q.data == "menu:stats":
        if not _is_mm(q.from_user.id, q.from_user.username):
            await q.answer("Stats are restricted to the middleman.", show_alert=True)
            return
        s = await db.get_full_stats()
        kb = _kb(_btn("🔄 Refresh Stats", "stats:refresh", BLUE))
        await q.message.reply_text(texts.stats_display(s), reply_markup=kb, parse_mode=HTML)


# ------------------------------------------------------------------ Live Chat Feature
async def livechat_cmd(update, ctx):
    user = update.effective_user
    await db.upsert_user(user)
    await db.start_livechat(user.id, user.username or "", user.first_name or "")
    kb = _kb(_btn("❌ Exit Live Chat", "lc:end", RED))
    await update.message.reply_text(texts.livechat_welcome(), reply_markup=kb, parse_mode=HTML)


async def endchat_cmd(update, ctx):
    user = update.effective_user
    await db.end_livechat(user.id)
    await update.message.reply_text(
        f"🚪 <b>Live Chat closed.</b> Send /livechat anytime to reconnect with @{config.MM_USERNAME}.",
        parse_mode=HTML,
    )


async def livechat_callback(update, ctx):
    q = update.callback_query
    await q.answer()
    user = q.from_user
    action = q.data

    if action == "lc:start":
        await db.start_livechat(user.id, user.username or "", user.first_name or "")
        kb = _kb(_btn("❌ Exit Live Chat", "lc:end", RED))
        await q.message.reply_text(texts.livechat_welcome(), reply_markup=kb, parse_mode=HTML)
    elif action == "lc:end":
        await db.end_livechat(user.id)
        await q.edit_message_text(
            f"🚪 <b>Live Chat closed.</b> Send /livechat anytime to talk to @{config.MM_USERNAME}.",
            parse_mode=HTML,
        )
    elif action.startswith("lc:reply_to:"):
        target_uid = int(action.split(":")[2])
        _admin_sessions[user.id] = {"action": "livechat_reply", "target_uid": target_uid}
        await q.message.reply_text(
            f"✏️ <b>Type your reply to user <code>{target_uid}</code>:</b>",
            parse_mode=HTML,
        )


async def reply_cmd(update, ctx):
    user = update.effective_user
    if not _is_mm(user.id, user.username):
        await update.message.reply_text("⚠️ This command is reserved for the middleman.")
        return

    if not ctx.args or len(ctx.args) < 2:
        await update.message.reply_text("Usage: <code>/reply &lt;user_id&gt; &lt;message&gt;</code>", parse_mode=HTML)
        return

    try:
        target_uid = int(ctx.args[0])
        reply_text = " ".join(ctx.args[1:])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    ok = await _relay_reply_to_user(ctx.bot, target_uid, reply_text)
    if ok:
        await update.message.reply_text(f"✅ Reply delivered to user <code>{target_uid}</code>.", parse_mode=HTML)
    else:
        await update.message.reply_text(f"❌ Failed to deliver reply to user <code>{target_uid}</code>.", parse_mode=HTML)


async def _relay_reply_to_user(bot, target_uid: int, text: str) -> bool:
    try:
        await bot.send_message(
            target_uid,
            f"💬 <b>Message from Middleman (@{config.MM_USERNAME}):</b>\n\n{esc(text)}",
            parse_mode=HTML,
        )
        return True
    except TelegramError as e:
        log.warning("Could not deliver reply to user %s: %s", target_uid, e)
        return False


# ------------------------------------------------------------------ /settings Dashboard
async def settings_cmd(update, ctx):
    user = update.effective_user
    if not _is_mm(user.id, user.username):
        await update.message.reply_text("⚠️ Unauthorized. The /settings dashboard is reserved for the middleman.")
        return

    await _show_settings_dashboard(update.message.reply_text)


async def _show_settings_dashboard(send_or_edit_fn):
    cur_fee = settings.get("default_fee", "4%")
    upi_id = settings.get("upi_id") or "Not set"

    kb = InlineKeyboardMarkup([
        [
            _btn(f"💰 Fee: {cur_fee}", "set:fee", GREEN),
            _btn("💳 UPI", "set:upi", BLUE),
        ],
        [
            _btn("🪙 Cryptos", "set:crypto_page:0", GREEN),
            _btn("📢 Channels", "set:channels", BLUE),
        ],
        [
            _btn("🏷️ GC Style & Photo", "set:gc_appearance", GREEN),
            _btn("⭐ Vouch Format", "set:vouch_format", BLUE),
        ],
        [
            _btn("📝 Templates", "set:templates", GREEN),
            _btn("📢 Sponsored Ad", "ad:manage", BLUE),
        ],
        [
            _btn("📢 Broadcast", "set:broadcast", BLUE),
            _btn("🧹 Clean GCs (/dlt)", "set:dlt", GREEN),
        ],
        [
            _btn("🏆 Top Users (Leaderboard)", "set:leaderboard", GREEN),
            _btn("❌ Close", "set:close", RED),
        ],
    ])

    text = (
        f"{emojis.EMOJI_SETTINGS} <b>@{config.MM_USERNAME} Settings</b>\n\n"
        f"• 💰 <b>Fee:</b> <code>{esc(str(cur_fee))}</code>\n"
        f"• 💳 <b>UPI:</b> <code>{esc(str(upi_id))}</code>\n"
        f"• 📢 <b>Official:</b> {settings.get('mm_channel_url', 'Not set')}\n"
        f"• ⭐ <b>Vouch:</b> {settings.get('vouch_channel_url', 'Not set')}\n"
    )
    await send_or_edit_fn(text, reply_markup=kb, parse_mode=HTML, link_preview_options=NO_PREVIEW)


async def settings_callback(update, ctx):
    q = update.callback_query
    user = q.from_user
    if not _is_mm(user.id, user.username):
        await q.answer("Unauthorized", show_alert=True)
        return

    data = q.data
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "cancel_input":
        await q.answer("Operation cancelled.")
        _admin_sessions.pop(user.id, None)
        try:
            await q.message.delete()
        except Exception:
            await q.edit_message_text("❌ Operation cancelled.")
        return

    if action == "close":
        await q.answer()
        await q.edit_message_text("⚙️ Settings dashboard closed.")
        return

    if action == "main":
        await q.answer()
        await _show_settings_dashboard(q.edit_message_text)
        return

    if action in ("broadcast", "bd"):
        await q.answer()
        await bd_cmd(update, ctx)
        return

    if action in ("dlt", "cleanup"):
        await q.answer()
        await dlt_cmd(update, ctx)
        return

    if action in ("leaderboard", "lb"):
        await q.answer()
        category = parts[2] if len(parts) > 2 else "dealers"
        period = parts[3] if len(parts) > 3 else "monthly"
        text, kb = await _render_leaderboard(category, period, is_admin=True)
        await q.edit_message_text(text, reply_markup=kb, parse_mode=HTML)
        return

    if action == "send_month_report":
        await q.answer("Compiling month-end report...")
        start_utc, end_utc, prev_month_name = db.get_previous_month_bounds()
        top_dealers = await db.leaderboard_dealers(limit=10, custom_bounds=(start_utc, end_utc))
        top_refs = await db.leaderboard_referrals(limit=10, custom_bounds=(start_utc, end_utc))

        report_lines = [
            f"🎉 <b>Top Users of {prev_month_name}</b>\n",
            f"🎁 <i>Monthly gift reminder for @{config.MM_USERNAME}:</i>\n",
            "🏆 <b>Top 10 Deal Makers:</b>",
        ]
        if not top_dealers:
            report_lines.append("<i>No deals recorded in concluded month.</i>")
        else:
            for i, (u, uid, cnt) in enumerate(top_dealers, 1):
                name = f"@{u['username'].lstrip('@')}" if u and u.get("username") else (u.get("first_name") if u and u.get("first_name") else "Dealer")
                report_lines.append(f"{i}. {name} — {cnt} Deal{'s' if cnt != 1 else ''}")

        report_lines.append("\n👥 <b>Top 10 Referrals:</b>")
        if not top_refs:
            report_lines.append("<i>No referrals recorded in concluded month.</i>")
        else:
            for i, (u, uid, cnt) in enumerate(top_refs, 1):
                name = f"@{u['username'].lstrip('@')}" if u and u.get("username") else (u.get("first_name") if u and u.get("first_name") else "Member")
                report_lines.append(f"{i}. {name} — {cnt} Referral{'s' if cnt != 1 else ''}")

        report_lines.append(f"\n✨ Middleman @{config.MM_USERNAME} can now send rewards to the winners!")
        report_text = "\n".join(report_lines)

        await service._dm_mm(ctx.bot, report_text)
        await q.message.reply_text("✅ <b>Month-end top users report has been delivered to your DM!</b>", parse_mode=HTML)
        return

    # Template list
    if action == "templates":
        await q.answer()
        btns = []
        for key, label in settings.TEMPLATE_LABELS.items():
            btns.append([_btn(f"📝 {label}", f"set:tmpl_view:{key}", BLUE)])
        btns.append([_btn("⬅️ Back to Settings", "set:main", GREEN), _btn("❌ Close", "set:close", RED)])
        t = (
            "📝 <b>Message Templates</b>\n"
            "Select any template to view or edit:"
        )
        await q.edit_message_text(t, reply_markup=InlineKeyboardMarkup(btns), parse_mode=HTML)
        return

    # View individual template
    if action == "tmpl_view":
        await q.answer()
        tmpl_key = parts[2]
        label = settings.TEMPLATE_LABELS.get(tmpl_key, tmpl_key)
        current_content = settings.get_template(tmpl_key)

        kb = InlineKeyboardMarkup([
            [
                _btn("👁️ Preview", f"set:tmpl_prev:{tmpl_key}", BLUE),
                _btn("✏️ Edit", f"set:tmpl_edit:{tmpl_key}", GREEN),
            ],
            [
                _btn("🔄 Reset Default", f"set:tmpl_reset:{tmpl_key}", RED),
                _btn("⬅️ Back", "set:templates", BLUE),
            ],
        ])
        t = (
            f"📝 <b>Template: {label}</b>\n\n"
            f"<code>{esc(current_content)}</code>\n\n"
            "<b>Tags:</b> <code>{deal_id}</code>, <code>{buyer}</code>, <code>{seller}</code>, <code>{currency}</code>, <code>{amount}</code>, <code>{tat}</code>"
        )
        await q.edit_message_text(t, reply_markup=kb, parse_mode=HTML)
        return

    # Preview template
    if action == "tmpl_prev":
        await q.answer()
        tmpl_key = parts[2]
        label = settings.TEMPLATE_LABELS.get(tmpl_key, tmpl_key)
        rendered_preview = settings.preview_template(tmpl_key)

        kb = _kb(_btn("⬅️ Back", f"set:tmpl_view:{tmpl_key}", GREEN), _btn("❌ Close", "set:close", RED))
        t = (
            f"👁️ <b>Preview: {label}</b>\n\n"
            f"{rendered_preview}"
        )
        await q.edit_message_text(t, reply_markup=kb, parse_mode=HTML, link_preview_options=NO_PREVIEW)
        return

    # Reset template
    if action == "tmpl_reset":
        tmpl_key = parts[2]
        await settings.reset_template(tmpl_key)
        await q.answer("Template reset to default!", show_alert=True)
        label = settings.TEMPLATE_LABELS.get(tmpl_key, tmpl_key)
        current_content = settings.get_template(tmpl_key)
        kb = InlineKeyboardMarkup([
            [
                _btn("👁️ Preview", f"set:tmpl_prev:{tmpl_key}", BLUE),
                _btn("✏️ Edit", f"set:tmpl_edit:{tmpl_key}", GREEN),
            ],
            [
                _btn("🔄 Reset Default", f"set:tmpl_reset:{tmpl_key}", RED),
                _btn("⬅️ Back", "set:templates", BLUE),
            ],
        ])
        t = (
            f"📝 <b>Template: {label} (Reset)</b>\n\n"
            f"<code>{esc(current_content)}</code>"
        )
        await q.edit_message_text(t, reply_markup=kb, parse_mode=HTML)
        return

    # Edit template
    if action == "tmpl_edit":
        await q.answer()
        tmpl_key = parts[2]
        label = settings.TEMPLATE_LABELS.get(tmpl_key, tmpl_key)
        _admin_sessions[user.id] = {"action": "set_template", "tmpl_key": tmpl_key}
        kb = InlineKeyboardMarkup([
            [_btn("⬅️ Back", f"set:tmpl_view:{tmpl_key}", BLUE), _btn("❌ Cancel", "set:cancel_input", RED)],
        ])
        await q.message.reply_text(
            f"✏️ <b>Edit Template: {label}</b>\n\n"
            "Send the new template message below:\n"
            "Tags: <code>{deal_id}</code>, <code>{buyer}</code>, <code>{seller}</code>, <code>{currency}</code>, <code>{amount}</code>, <code>{tat}</code>",
            reply_markup=kb,
            parse_mode=HTML,
        )
        return

    if action == "channels":
        await q.answer()
        kb = InlineKeyboardMarkup([
            [_btn("✏️ Edit Official", "set:edit:mm_channel_url", BLUE), _btn("✏️ Edit Vouch", "set:edit:vouch_channel_url", BLUE)],
            [_btn("✏️ Edit Rules / TOS", "set:edit:tos_channel_url", BLUE)],
            [_btn("⬅️ Back to Settings", "set:main", GREEN), _btn("❌ Close", "set:close", RED)],
        ])
        t = (
            "📢 <b>Channels & Links</b>\n\n"
            f"• <b>Official:</b> {settings.get('mm_channel_url')}\n"
            f"• <b>Vouch:</b> {settings.get('vouch_channel_url')}\n"
            f"• <b>TOS / Rules:</b> {settings.get('tos_channel_url')}"
        )
        await q.edit_message_text(t, reply_markup=kb, parse_mode=HTML, link_preview_options=NO_PREVIEW)

    elif action == "upi":
        await q.answer()
        kb = InlineKeyboardMarkup([
            [_btn("✏️ Update UPI ID", "set:edit:upi_id", GREEN)],
            [_btn("⬅️ Back to Settings", "set:main", BLUE), _btn("❌ Close", "set:close", RED)],
        ])
        t = (
            "💳 <b>UPI Configuration</b>\n\n"
            f"• <b>UPI ID:</b> <code>{esc(settings.get('upi_id', 'Not set'))}</code>\n"
            f"• <b>Name:</b> <code>{esc(settings.get('upi_name', ''))}</code>"
        )
        await q.edit_message_text(t, reply_markup=kb, parse_mode=HTML)

    elif action == "fee":
        await q.answer()
        cur_fee = settings.get("default_fee", "4%")
        kb = InlineKeyboardMarkup([
            [
                _btn("4%", "set:fee_pick:4%", GREEN),
                _btn("5%", "set:fee_pick:5%", GREEN),
                _btn("3%", "set:fee_pick:3%", GREEN),
                _btn("2%", "set:fee_pick:2%", GREEN),
            ],
            [
                _btn("50 INR", "set:fee_pick:50 INR", BLUE),
                _btn("100 INR", "set:fee_pick:100 INR", BLUE),
                _btn("✏️ Custom Fee", "set:edit:default_fee", BLUE),
            ],
            [
                _btn("⬅️ Back to Settings", "set:main", BLUE),
                _btn("❌ Close", "set:close", RED),
            ],
        ])
        t = (
            "💰 <b>Middleman Fee Setting</b>\n\n"
            f"Current Fee: <b>{esc(str(cur_fee))}</b>\n"
            "Choose a preset or set custom fee:"
        )
        await q.edit_message_text(t, reply_markup=kb, parse_mode=HTML)

    elif action == "fee_pick":
        new_fee = parts[2]
        await settings.set_setting("default_fee", new_fee)
        await q.answer(f"Fee set to {new_fee}!", show_alert=True)
        await _show_settings_dashboard(q.edit_message_text)
        return

    elif action == "vouch_format":
        await q.answer()
        vouch_tmpl = settings.get_template("vouch")
        sample_deal = {
            "deal_no": 19,
            "amount": "5005",
            "currency": "INR",
            "purpose": "Instagram Account",
            "buyer_username": "buyer",
            "seller_username": "seller",
        }
        sample_preview = settings.render_vouch(sample_deal)
        kb = InlineKeyboardMarkup([
            [
                _btn("Preset 1: Vouch @{mm} for MM'd {amount} {currency}", "set:vouch_preset:1", BLUE),
            ],
            [
                _btn("Preset 2: +rep @{mm} for MM {amount} {currency}", "set:vouch_preset:2", BLUE),
            ],
            [
                _btn("✏️ Custom Format", "set:tmpl_edit:vouch", GREEN),
                _btn("🔄 Reset Default", "set:tmpl_reset:vouch", RED),
            ],
            [
                _btn("⬅️ Back to Settings", "set:main", BLUE),
                _btn("❌ Close", "set:close", RED),
            ],
        ])
        t = (
            "⭐ <b>Vouch Format</b>\n\n"
            f"• <b>Format:</b> <code>{esc(vouch_tmpl)}</code>\n"
            f"• <b>Preview:</b> <i>{esc(sample_preview)}</i>"
        )
        await q.edit_message_text(t, reply_markup=kb, parse_mode=HTML)
        return

    elif action == "vouch_preset":
        p_idx = parts[2]
        if p_idx == "1":
            new_v = "Vouch @{mm} for MM'd {amount} {currency}"
        else:
            new_v = "+rep @{mm} for MM {amount} {currency}"
        await settings.set_template("vouch", new_v)
        await q.answer("Vouch format updated!", show_alert=True)
        await _show_settings_dashboard(q.edit_message_text)
        return

    elif action == "gc_appearance":
        await q.answer()
        has_photo = "✅ Configured" if settings.get("gc_photo_file_id") else "❌ Not configured"
        tmpl = settings.get("gc_name_template", settings.GC_NAME_PRESETS[0])
        kb = InlineKeyboardMarkup([
            [_btn("🏷️ Name Style", "set:gcstyle", BLUE)],
            [_btn("🖼️ Profile Photo", "set:gc_photo_menu", GREEN)],
            [_btn("⬅️ Back to Settings", "set:main", GREEN), _btn("❌ Close", "set:close", RED)],
        ])
        t = (
            "🎨 <b>Group Chat Appearance</b>\n\n"
            f"• <b>Format:</b> <code>{esc(tmpl)}</code>\n"
            f"• <b>Photo:</b> {has_photo}"
        )
        await q.edit_message_text(t, reply_markup=kb, parse_mode=HTML)

    elif action == "gc_photo_menu":
        await q.answer()
        photo_id = settings.get("gc_photo_file_id")
        btns = []
        if photo_id:
            btns.append([
                _btn("👁️ Preview Photo", "set:gc_photo_prev", BLUE),
                _btn("🔄 Replace Photo", "set:gc_photo_upload", GREEN),
            ])
            btns.append([_btn("🗑️ Remove Photo", "set:gc_photo_remove", RED)])
            status_text = "✅ <b>Configured</b> (Applied to new deal groups)"
        else:
            btns.append([_btn("📤 Upload Photo", "set:gc_photo_upload", GREEN)])
            status_text = "❌ <b>Not configured</b>"
        btns.append([_btn("⬅️ Back", "set:gc_appearance", BLUE), _btn("❌ Close", "set:close", RED)])
        t = (
            "🖼️ <b>GC Profile Photo Settings</b>\n\n"
            f"• Status: {status_text}"
        )
        await q.edit_message_text(t, reply_markup=InlineKeyboardMarkup(btns), parse_mode=HTML)

    elif action == "gc_photo_prev":
        await q.answer()
        photo_id = settings.get("gc_photo_file_id")
        if not photo_id:
            await q.answer("No photo configured.", show_alert=True)
            return
        try:
            await ctx.bot.send_photo(
                chat_id=user.id,
                photo=photo_id,
                caption="🖼️ <b>Current GC Profile Photo</b>\nApplied to all newly created deal groups.",
                parse_mode=HTML,
            )
        except Exception as e:
            await q.answer(f"Could not load photo preview: {e}", show_alert=True)

    elif action == "gc_photo_upload":
        await q.answer()
        _admin_sessions[user.id] = {"action": "set_gc_photo"}
        kb = InlineKeyboardMarkup([
            [_btn("⬅️ Back", "set:gc_appearance", BLUE), _btn("❌ Cancel", "set:cancel_input", RED)],
        ])
        await q.message.reply_text(
            "📸 <b>Upload GC Profile Photo</b>\n\n"
            "Send the photo you want to use for new deal groups:",
            reply_markup=kb,
            parse_mode=HTML,
        )

    elif action == "gc_photo_remove":
        await settings.set_setting("gc_photo_file_id", None)
        await q.answer("GC Profile Photo removed!", show_alert=True)
        kb = InlineKeyboardMarkup([
            [_btn("📤 Upload Photo", "set:gc_photo_upload", GREEN)],
            [_btn("⬅️ Back to GC Appearance", "set:gc_appearance", BLUE), _btn("❌ Close", "set:close", RED)],
        ])
        t = (
            "🖼️ <b>GC Profile Photo Settings</b>\n\n"
            "Status: ❌ <b>Removed</b>"
        )
        await q.edit_message_text(t, reply_markup=kb, parse_mode=HTML)

    elif action == "gcstyle":
        await q.answer()
        btns = []
        for i, preset in enumerate(settings.GC_NAME_PRESETS):
            btns.append([_btn(f"Preset {i+1}: {preset}", f"set:gc_pick:{i}", BLUE)])
        btns.append([_btn("✏️ Custom Template", "set:gc_custom_name", GREEN)])
        btns.append([_btn("⬅️ Back", "set:gc_appearance", GREEN), _btn("❌ Close", "set:close", RED)])
        t = (
            "🏷️ <b>Deal Group Name Template</b>\n\n"
            f"Current Format: <code>{esc(settings.get('gc_name_template'))}</code>\n\n"
            "Select a preset or write your own custom template.\n"
            "<b>Available tags:</b> <code>{label}</code>, <code>{mm}</code>, <code>{buyer}</code>, "
            "<code>{seller}</code>, <code>{amount}</code>, <code>{startno}</code>"
        )
        await q.edit_message_text(t, reply_markup=InlineKeyboardMarkup(btns), parse_mode=HTML)

    elif action == "gc_pick":
        idx = int(parts[2])
        preset = settings.GC_NAME_PRESETS[idx]
        if "{startno}" in preset:
            await q.answer()
            _admin_sessions[user.id] = {"action": "set_gc_start_num", "preset_idx": idx}
            kb = InlineKeyboardMarkup([
                [_btn("⬅️ Back", "set:gcstyle", BLUE), _btn("❌ Cancel", "set:cancel_input", RED)],
            ])
            await q.message.reply_text(
                "🔢 <b>Starting Deal Number</b>\n\n"
                "Enter starting positive integer deal number (e.g. <code>121</code>):",
                reply_markup=kb,
                parse_mode=HTML,
            )
            return

        await settings.set_setting("gc_name_template", preset)
        await q.answer("GC Naming Template Updated!", show_alert=True)
        await _show_settings_dashboard(q.edit_message_text)

    elif action == "gc_custom_name":
        await q.answer()
        _admin_sessions[user.id] = {"action": "set_gc_custom_name"}
        kb = InlineKeyboardMarkup([
            [_btn("⬅️ Back", "set:gcstyle", BLUE), _btn("❌ Cancel", "set:cancel_input", RED)],
        ])
        await q.message.reply_text(
            "✏️ <b>Custom GC Name Template</b>\n\n"
            "Send your custom group name template.\n\n"
            "<b>Available tags:</b>\n"
            "• <code>{label}</code> — deal label (e.g. #42)\n"
            "• <code>{mm}</code> — middleman username\n"
            "• <code>{buyer}</code> — buyer's username\n"
            "• <code>{seller}</code> — seller's username\n"
            "• <code>{amount}</code> — deal amount\n"
            "• <code>{startno}</code> — sequential display number\n\n"
            "<b>Example:</b> <code>🤝 Deal {label} | @{mm}</code>",
            reply_markup=kb,
            parse_mode=HTML,
        )

    elif action == "crypto_page":
        await q.answer()
        page = int(parts[2]) if len(parts) > 2 else 0
        all_coins = list(settings.CRYPTO_NETWORKS.items())
        page_size = 6
        total_pages = (len(all_coins) + page_size - 1) // page_size
        page = max(0, min(page, total_pages - 1))

        current_slice = all_coins[page * page_size : (page + 1) * page_size]
        btns = []
        wallets = settings.get("crypto_wallets", {})

        for key, info in current_slice:
            has_addr = "✅" if wallets.get(key) else "❌"
            btns.append([_btn(f"{has_addr} {info['symbol']} ({info['network']})", f"set:coin:{key}", BLUE)])

        # Pagination controls
        nav = []
        if page > 0:
            nav.append(_btn("⬅️ Prev", f"set:crypto_page:{page-1}", BLUE))
        if page < total_pages - 1:
            nav.append(_btn("Next ➡️", f"set:crypto_page:{page+1}", BLUE))
        if nav:
            btns.append(nav)
        btns.append([_btn("⬅️ Back to Settings", "set:main", GREEN), _btn("❌ Close", "set:close", RED)])

        t = f"🪙 <b>Crypto Wallets ({page+1}/{total_pages})</b>\nTap any coin to configure its address:"
        await q.edit_message_text(t, reply_markup=InlineKeyboardMarkup(btns), parse_mode=HTML)

    elif action == "coin":
        await q.answer()
        coin_key = parts[2]
        info = settings.CRYPTO_NETWORKS.get(coin_key, {})
        current_addr = settings.get_crypto_wallet(coin_key) or "Not configured"

        kb = InlineKeyboardMarkup([
            [_btn("✏️ Edit Address", f"set:edit_coin:{coin_key}", GREEN)],
            [_btn("⬅️ Back to Cryptos", "set:crypto_page:0", BLUE), _btn("❌ Close", "set:close", RED)],
        ])
        t = (
            f"🪙 <b>{info.get('name', 'Crypto')} ({info.get('symbol', '')})</b>\n"
            f"🌐 <b>Network:</b> <code>{info.get('network', '')}</code>\n"
            f"📥 <b>Address:</b> <code>{esc(current_addr)}</code>"
        )
        await q.edit_message_text(t, reply_markup=kb, parse_mode=HTML)

    elif action == "edit_coin":
        await q.answer()
        coin_key = parts[2]
        _admin_sessions[user.id] = {"action": "set_crypto", "coin_key": coin_key}
        info = settings.CRYPTO_NETWORKS.get(coin_key, {})
        kb = InlineKeyboardMarkup([
            [_btn("⬅️ Back to Coin", f"set:coin:{coin_key}", BLUE), _btn("❌ Cancel", "set:cancel_input", RED)],
        ])
        await q.message.reply_text(
            f"✏️ Send new wallet address for <b>{info.get('name')} ({info.get('network')})</b>:",
            reply_markup=kb,
            parse_mode=HTML,
        )

    elif action == "edit":
        await q.answer()
        field_key = parts[2]
        _admin_sessions[user.id] = {"action": "set_field", "field": field_key}
        kb = InlineKeyboardMarkup([
            [_btn("⬅️ Back to Settings", "set:main", BLUE), _btn("❌ Cancel", "set:cancel_input", RED)],
        ])
        await q.message.reply_text(
            f"✏️ Send new value for <b>{field_key}</b>:",
            reply_markup=kb,
            parse_mode=HTML,
        )


# ------------------------------------------------------------------ /ad Advertisement Manager
async def ad_cmd(update, ctx):
    user = update.effective_user
    if not _is_mm(user.id, user.username):
        await update.message.reply_text("⚠️ This command is reserved for the middleman.")
        return

    ad = settings.get_ad()
    is_active = ad.get("enabled") and bool(ad.get("text"))
    status_str = "🟢 <b>Active</b>" if is_active else "🔴 <b>Disabled</b>"
    raw_ad = (ad.get("text") or "").strip()

    content = (
        "📢 <b>Ad Footer Settings</b>\n\n"
        f"• <b>Status:</b> {status_str}\n"
    )
    if raw_ad:
        content += f"• <b>Current Ad:</b>\n<blockquote>{raw_ad}</blockquote>\n"
    else:
        content += "• <b>Current Ad:</b> <i>None</i>\n"

    btns = [
        [_btn("✏️ Set / Replace Ad", "ad:set", BLUE)],
    ]
    if raw_ad:
        if is_active:
            btns.append([_btn("🔴 Disable", "ad:disable", RED), _btn("👁 Preview", "ad:preview", BLUE)])
        else:
            btns.append([_btn("🟢 Enable", "ad:enable", GREEN), _btn("👁 Preview", "ad:preview", BLUE)])
    btns.append([_btn("⬅️ Back to Settings", "set:main", GREEN), _btn("❌ Close", "set:close", RED)])

    await update.message.reply_text(content, reply_markup=InlineKeyboardMarkup(btns), parse_mode=HTML)


async def ad_callback(update, ctx):
    q = update.callback_query
    user = q.from_user
    if not _is_mm(user.id, user.username):
        await q.answer("Only the middleman can configure advertisements.", show_alert=True)
        return

    parts = q.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action in ("manage", "main"):
        await q.answer()
        ad = settings.get_ad()
        is_active = ad.get("enabled") and bool(ad.get("text"))
        status_str = "🟢 <b>Active</b>" if is_active else "🔴 <b>Disabled</b>"
        raw_ad = (ad.get("text") or "").strip()
        content = (
            "📢 <b>Ad Footer Settings</b>\n\n"
            f"• <b>Status:</b> {status_str}\n"
        )
        if raw_ad:
            content += f"• <b>Current Ad:</b>\n<blockquote>{raw_ad}</blockquote>\n"
        else:
            content += "• <b>Current Ad:</b> <i>None</i>\n"

        btns = [
            [_btn("✏️ Set / Replace Ad", "ad:set", BLUE)],
        ]
        if raw_ad:
            if is_active:
                btns.append([_btn("🔴 Disable", "ad:disable", RED), _btn("👁 Preview", "ad:preview", BLUE)])
            else:
                btns.append([_btn("🟢 Enable", "ad:enable", GREEN), _btn("👁 Preview", "ad:preview", BLUE)])
        btns.append([_btn("⬅️ Back to Settings", "set:main", GREEN), _btn("❌ Close", "set:close", RED)])

        await q.edit_message_text(content, reply_markup=InlineKeyboardMarkup(btns), parse_mode=HTML)
        return

    elif action == "set":
        await q.answer()
        _admin_sessions[user.id] = {"action": "set_ad"}
        kb = InlineKeyboardMarkup([
            [_btn("⬅️ Back to Ad Manager", "ad:manage", BLUE), _btn("❌ Cancel", "ad:cancel", RED)],
        ])
        await q.message.reply_text(
            "📢 <b>Set Ad Footer</b>\n\n"
            "Send your ad text below (custom emojis & formatting supported):",
            reply_markup=kb,
            parse_mode=HTML,
        )

    elif action == "cancel":
        await q.answer("Ad setup cancelled.")
        _admin_sessions.pop(user.id, None)
        try:
            await q.message.delete()
        except Exception:
            await q.edit_message_text("❌ Cancelled.")
        return

    elif action == "preview":
        await q.answer()
        ad = settings.get_ad()
        ad_txt = ad.get("text") or "🎰 @Rainbetcom #1 Non-KYC Crypto Casino & Sportsbook"
        kb = InlineKeyboardMarkup([
            [_btn("⬅️ Back to Ad Manager", "ad:manage", BLUE), _btn("❌ Close", "set:close", RED)],
        ])
        await q.message.reply_text(
            f"👁 <b>Ad Preview:</b>\n\n"
            f"Sample notification message...\n\n"
            f"<blockquote>{ad_txt}</blockquote>",
            reply_markup=kb,
            parse_mode=HTML,
        )

    elif action == "disable":
        ad = settings.get_ad()
        await settings.set_ad(ad.get("text", ""), enabled=False)
        await q.answer("Ad disabled.")
        await ad_callback(update, ctx)

    elif action == "enable":
        ad = settings.get_ad()
        if not ad.get("text"):
            await q.answer("No ad configured. Tap Set / Replace first.", show_alert=True)
            return
        await settings.set_ad(ad.get("text", ""), enabled=True)
        await q.answer("Ad enabled!")
        await ad_callback(update, ctx)



# ------------------------------------------------------------------ /stats
async def stats_cmd(update, ctx):
    user = update.effective_user
    if not _is_mm(user.id, user.username):
        await update.message.reply_text("⚠️ Stats are restricted to the middleman.")
        return
    s = await db.get_full_stats()
    kb = _kb(_btn("🔄 Refresh Stats", "stats:refresh", BLUE))
    await update.message.reply_text(texts.stats_display(s), reply_markup=kb, parse_mode=HTML)


async def stats_callback(update, ctx):
    q = update.callback_query
    if not _is_mm(q.from_user.id, q.from_user.username):
        await q.answer("Stats are restricted to the middleman.", show_alert=True)
        return
    await q.answer("Stats updated!")
    s = await db.get_full_stats()
    kb = _kb(_btn("🔄 Refresh Stats", "stats:refresh", BLUE))
    try:
        await q.edit_message_text(texts.stats_display(s), reply_markup=kb, parse_mode=HTML)
    except BadRequest:
        pass


# ------------------------------------------------------------------ /referral, /deals, /leaderboard
async def _referral_text(ctx, uid) -> tuple:
    u = await db.get_user(uid)
    link = f"https://t.me/{ctx.bot.username}?start=ref_{u['ref_code']}"
    text = (
        f"{emojis.EMOJI_REF} <b>Referral Program</b>\n\n"
        "Invite friends and earn <b>2 points</b> for every referral!\n\n"
        f"🔗 <b>Your Link:</b> {link}\n"
        f"⭐ <b>Your Points:</b> {u['points']}\n\n"
        "🎁 <b>Monthly Rewards:</b>\n"
        f"Top 10 Referrals & Top 10 Dealers get an exclusive gift from @{config.MM_USERNAME} at every month end!"
    )
    kb = InlineKeyboardMarkup([
        [_btn("🏆 Top Users Leaderboard", "lb:referrals:monthly", GREEN)],
    ])
    return text, kb


async def referral_cb(update, ctx):
    q = update.callback_query
    await q.answer()
    await db.upsert_user(q.from_user)
    text, kb = await _referral_text(ctx, q.from_user.id)
    await q.message.reply_text(text, reply_markup=kb, parse_mode=HTML, link_preview_options=NO_PREVIEW)


async def referral_cmd(update, ctx):
    await db.upsert_user(update.effective_user)
    text, kb = await _referral_text(ctx, update.effective_user.id)
    await update.message.reply_text(text, reply_markup=kb, parse_mode=HTML, link_preview_options=NO_PREVIEW)


async def redeem_cmd(update, ctx):
    await db.upsert_user(update.effective_user)
    await update.message.reply_text("🎁 Redeem is coming soon.")


async def deals_cmd(update, ctx):
    user = update.effective_user
    await db.upsert_user(user)
    n = await db.count_user_deals(user.id)
    if n == 0:
        await update.message.reply_text(
            f"📋 <b>Deal History</b>\n\nYou haven't participated in any deals yet.\n\nUse /deal to get started. 🔒",
            parse_mode=HTML,
        )
    else:
        await update.message.reply_text(
            f"📋 <b>Deal History</b>\n\nYou participated in {n} deals with {texts.MM}.\n\nUse /deal to get started. 🔒",
            parse_mode=HTML,
        )


async def _render_leaderboard(category: str = "dealers", period: str = "monthly", is_admin: bool = False) -> tuple:
    is_monthly = (period == "monthly")
    tz_ist = ZoneInfo(config.DISPLAY_TZ or "Asia/Kolkata")
    now_ist = datetime.now(tz_ist)
    month_abbr = now_ist.strftime("%b %Y")

    if category == "dealers":
        rows = await db.leaderboard_dealers(limit=10, monthly=is_monthly)
        time_label = f"of {month_abbr}" if is_monthly else "(All-Time)"
        header = f"🏆 <b>Top 10 Deal Makers {time_label}</b>\n\n"
        unit = "Deal"
    else:
        rows = await db.leaderboard_referrals(limit=10, monthly=is_monthly)
        time_label = f"of {month_abbr}" if is_monthly else "(All-Time)"
        header = f"👥 <b>Top 10 Referrals {time_label}</b>\n\n"
        unit = "Referral"

    lines = []
    if not rows:
        lines.append("<i>No records found yet for this period.</i>")
    else:
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for i, (u, uid, count) in enumerate(rows, 1):
            badge = medals.get(i, f"{i}.")
            if u and u.get("username"):
                uname = f"@{esc(u['username'].lstrip('@'))}"
            elif u and u.get("first_name"):
                fname = u["first_name"]
                lname = u.get("last_name")
                uname = esc(f"{fname} {lname}".strip() if lname else fname)
            elif u and u.get("name"):
                uname = esc(u["name"])
            else:
                uname = "Dealer" if category == "dealers" else "Member"
            plural = "" if count == 1 else "s"
            lines.append(f"{badge} {uname} — <b>{count}</b> {unit}{plural}")

    body = "\n".join(lines)
    footer = (
        "\n\n🎁 <b>Monthly Rewards:</b>\n"
        f"Top 10 Referrals and Top 10 Deal Makers get an exclusive gift from @{config.MM_USERNAME} at every month end!"
    )
    full_text = f"{header}{body}{footer}"

    d_m_active = "• " if (category == "dealers" and is_monthly) else ""
    d_a_active = "• " if (category == "dealers" and not is_monthly) else ""
    r_m_active = "• " if (category == "referrals" and is_monthly) else ""
    r_a_active = "• " if (category == "referrals" and not is_monthly) else ""

    prefix = "set:lb" if is_admin else "lb"
    btns = [
        [
            _btn(f"{d_m_active}🏆 Deals (Month)", f"{prefix}:dealers:monthly", GREEN if d_m_active else BLUE),
            _btn(f"{d_a_active}🏆 Deals (All-Time)", f"{prefix}:dealers:alltime", GREEN if d_a_active else BLUE),
        ],
        [
            _btn(f"{r_m_active}👥 Refs (Month)", f"{prefix}:referrals:monthly", GREEN if r_m_active else BLUE),
            _btn(f"{r_a_active}👥 Refs (All-Time)", f"{prefix}:referrals:alltime", GREEN if r_a_active else BLUE),
        ],
    ]
    if is_admin:
        btns.append([_btn("📢 Send Month-End Report to My DM", "set:send_month_report", BLUE)])
        btns.append([_btn("⬅️ Back to Settings", "set:main", GREEN), _btn("❌ Close", "set:close", RED)])
    else:
        btns.append([_btn("🔄 Refresh", f"lb:{category}:{period}", BLUE), _btn("❌ Close", "lb:close", RED)])

    return full_text, InlineKeyboardMarkup(btns)


async def leaderboard_cmd(update, ctx):
    text, kb = await _render_leaderboard(category="dealers", period="monthly")
    await update.message.reply_text(text, reply_markup=kb, parse_mode=HTML)


async def leaderboard_callback(update, ctx):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    if len(parts) > 1 and parts[1] == "close":
        try:
            await q.message.delete()
        except Exception:
            await q.edit_message_text("❌ Closed.")
        return

    category = parts[1] if len(parts) > 1 else "dealers"
    period = parts[2] if len(parts) > 2 else "monthly"

    text, kb = await _render_leaderboard(category=category, period=period)
    try:
        await q.edit_message_text(text, reply_markup=kb, parse_mode=HTML)
    except Exception:
        pass


async def id_cmd(update, ctx):
    uid = update.effective_user.id
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("Select a user", request_users=KeyboardButtonRequestUsers(request_id=1, user_is_bot=False, max_quantity=1),
                         api_kwargs={"icon_custom_emoji_id": emojis.get_custom_id("USER")} if emojis.get_custom_id("USER") else None)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text(
        f"Your User ID: <code>{uid}</code>\n\nGet any user's ID by tapping the button below.",
        parse_mode=HTML,
        reply_markup=kb,
    )


async def _users_shared(msg):
    ids = [u.user_id for u in msg.users_shared.users]
    text = "\n".join(f"User ID: <code>{i}</code>" for i in ids) or "No user selected."
    await msg.reply_text(text, parse_mode=HTML, reply_markup=ReplyKeyboardRemove())


async def _forwarded(msg):
    o = msg.forward_origin
    if isinstance(o, MessageOriginUser):
        text = f"👤 Original sender's User ID: <code>{o.sender_user.id}</code>"
    elif isinstance(o, MessageOriginHiddenUser):
        text = f"🔒 {esc(o.sender_user_name)} has hidden their account, so Telegram doesn't share their User ID."
    elif isinstance(o, MessageOriginChat):
        text = f"💬 Original chat's Chat ID: <code>{o.sender_chat.id}</code>"
    elif isinstance(o, MessageOriginChannel):
        text = f"📢 Original chat's Chat ID: <code>{o.chat.id}</code>"
    else:
        text = "Telegram didn't provide an ID for this forwarded message."
    await msg.reply_text(text, parse_mode=HTML)


# ------------------------------------------------------------------ Standard /deal setup
async def deal_cmd(update, ctx):
    user, msg = update.effective_user, update.message
    await db.upsert_user(user)
    if not user.username:
        await msg.reply_text("⚠️ Please set a Telegram username in your profile settings, then send /deal again.")
        return
    sent = await msg.reply_text(
        "<b>Select Deal Type</b>",
        reply_markup=_kb(
            _btn("🤝 P2P", "d:type:p2p", BLUE),
            _btn("🔄 Exchange", "d:type:exchange", GREEN, icon=emojis.get_custom_id("EXCHANGE")),
            _btn("❌ Cancel", "d:cancel", RED),
        ),
        parse_mode=HTML,
    )
    await db.start_session(user.id, [msg.message_id, sent.message_id])


def _parties(d, user) -> dict:
    me = user.username
    buyer, seller = (me, d["cp"]) if d["role"] == "buyer" else (d["cp"], me)
    return {
        "buyer_username": buyer, "seller_username": seller, "purpose": d["purpose"],
        "tat_seconds": d["tat_seconds"], "amount": d["amount"], "currency": d["currency"],
        "conditions": d.get("conditions", "None"),
    }


async def _delete_msgs(bot, chat_id, ids):
    ids = list(dict.fromkeys(ids))
    for i in range(0, len(ids), 100):
        try:
            await bot.delete_messages(chat_id, ids[i:i + 100])
        except TelegramError:
            for mid in ids[i:i + 100]:
                try:
                    await bot.delete_message(chat_id, mid)
                except TelegramError:
                    pass


async def _show_summary(ctx, user, chat_id, s, extra_ids, conditions):
    d = dict(s["data"])
    d["conditions"] = conditions
    await db.update_session(user.id, data={"conditions": conditions})
    await _delete_msgs(ctx.bot, chat_id, list(s["msg_ids"]) + list(extra_ids))
    sent = await ctx.bot.send_message(
        chat_id,
        texts.summary_text(_parties(d, user)),
        parse_mode=HTML,
        reply_markup=_kb(_btn("✅ Confirm Deal", "d:confirm", GREEN), _btn("❌ Cancel", "d:cancel", RED)),
    )
    await db.update_session(user.id, step="summary", msg_ids=[sent.message_id])


async def _session_text(update, ctx, s):
    msg, user = update.message, update.effective_user
    text, step = msg.text.strip(), s["step"]

    async def reject(err):
        sent = await msg.reply_text(
            err,
            parse_mode=HTML,
            reply_markup=_kb(_btn("❌ Cancel", "d:cancel", RED)),
        )
        await db.update_session(user.id, push_ids=[msg.message_id, sent.message_id])

    async def advance(prompt, next_step, data, markup=None):
        # Clean up prior wizard bot prompt so the DM stays clean
        prev_ids = list(s.get("msg_ids", []))
        if prev_ids:
            try:
                await ctx.bot.delete_message(chat_id=msg.chat_id, message_id=prev_ids[-1])
            except Exception:
                pass
        sent = await msg.reply_text(prompt, parse_mode=HTML, reply_markup=markup)
        await db.update_session(user.id, step=next_step, data=data, push_ids=[msg.message_id, sent.message_id])

    if step == "cp":
        un = parse_username(text)
        if not un:
            return await reject("❌ Invalid username.\n\nEnter a valid Telegram username (e.g. @username):")
        if un.lower() == (user.username or "").lower():
            return await reject("❌ That's your own username.\n\nEnter the other party's @username:")
        await advance(
            "<b>What is being traded?</b>\n\nExample: Instagram account",
            "purpose",
            {"cp": un},
            _kb(_btn("❌ Cancel", "d:cancel", RED)),
        )

    elif step == "purpose":
        if len(text) > 200:
            return await reject("❌ Description is too long (max 200 characters). Please enter a shorter description:")
        await advance(
            "<b>Select Currency</b>",
            "currency",
            {"purpose": text},
            _kb(
                _btn("₹ INR", "d:cur:INR", BLUE),
                _btn("$ USD", "d:cur:USD", BLUE),
                _btn("🪙 Crypto", "d:cur:Crypto", GREEN),
                _btn("❌ Cancel", "d:cancel", RED),
            ),
        )

    elif step == "amount":
        try:
            amount = parse_amount(text)
        except ValueError as e:
            return await reject(f"{e}\n\nEnter a valid numeric amount (e.g. 5000):")
        await advance(
            "⏱ <b>Enter delivery time (TAT)</b>\n\nExample: 30m, 2h, 1d",
            "tat",
            {"amount": amount},
            _kb(_btn("❌ Cancel", "d:cancel", RED)),
        )

    elif step == "tat":
        try:
            secs = parse_tat(text)
        except ValueError:
            return await reject("⚠️ <b>Invalid TAT format.</b>\n\nExample: <code>30m</code>, <code>2h</code>, <code>1d</code>")
        await advance(
            "📜 <b>Deal Conditions</b>\n\nEnter agreed terms or tap Skip.",
            "terms",
            {"tat_seconds": secs},
            _kb(_btn("⏩ Skip", "d:skip", GREEN), _btn("❌ Cancel", "d:cancel", RED)),
        )

    elif step == "terms":
        if len(text) > 500:
            return await reject("❌ Conditions too long (max 500 characters).")
        await _show_summary(ctx, user, msg.chat_id, s, [msg.message_id], text)

    elif step == "ex_give":
        if len(text) > 150:
            return await reject("❌ Too long. Keep it under 150 characters.")
        parsed = _parse_ex_value(text)
        await advance(
            "<b>What are you receiving?</b>\n\nExample: <code>5$ usdt</code>, <code>10 USDT</code>, <code>4800 INR</code>",
            "ex_receive",
            {"ex_give_amount": parsed},
            _kb(_btn("❌ Cancel", "d:cancel", RED)),
        )

    elif step == "ex_receive":
        if len(text) > 150:
            return await reject("❌ Too long. Keep it under 150 characters.")
        parsed = _parse_ex_value(text)
        await advance(
            "<b>Counterparty Username</b>\n\nEnter the other party's @username.\n\nExample: @username",
            "ex_cp",
            {"ex_receive_amount": parsed},
            _kb(_btn("❌ Cancel", "d:cancel", RED)),
        )

    elif step == "ex_cp":
        un = parse_username(text)
        if not un:
            return await reject("❌ Invalid username.\n\nEnter a valid Telegram username starting with @:")
        if un.lower() == (user.username or "").lower():
            return await reject("❌ That's your own username. Enter the other party's @username:")
        await advance(
            "⏱ <b>Enter delivery time (TAT)</b>\n\nExample: 30m, 2h, 1d",
            "ex_tat",
            {"ex_cp": un},
            _kb(_btn("❌ Cancel", "d:cancel", RED)),
        )

    elif step == "ex_tat":
        try:
            secs = parse_tat(text)
        except ValueError:
            return await reject(
                "⚠️ <b>Invalid TAT format.</b>\n\n"
                "Example: <code>30m</code>, <code>2h</code>, <code>1d</code>"
            )
        await advance(
            "📜 <b>Deal Conditions</b>\n\nEnter agreed terms or tap Skip.",
            "ex_terms",
            {"ex_tat_seconds": secs},
            _kb(_btn("⏩ Skip", "d:ex_skip", GREEN), _btn("❌ Cancel", "d:cancel", RED)),
        )

    elif step == "ex_terms":
        if len(text) > 500:
            return await reject("❌ Too long. Keep it under 500 characters.")
        await _show_exchange_summary(ctx, user, msg.chat_id, s, [msg.message_id], text)


async def deal_callback(update, ctx):
    q, user = update.callback_query, update.callback_query.from_user
    s = await db.get_session(user.id)
    if not s:
        await q.answer("⏳ This deal setup has expired. Send /deal to start again.", show_alert=True)
        return
    parts, step, d = q.data.split(":"), s["step"], s["data"]
    action = parts[1]

    if action == "type" and step == "type":
        if parts[2] == "exchange":
            await q.answer()
            await db.update_session(user.id, step="ex_give", data={"deal_type": "exchange"})
            await q.edit_message_text(
                "🔄 <b>Exchange Deal</b>\n\n"
                "<b>What are you giving?</b>\n\n"
                "Example: <code>500 INR</code>, <code>10 USDT</code>, <code>1 BTC</code>, <code>5$ usdt</code>",
                reply_markup=_kb(_btn("❌ Cancel", "d:cancel", RED)),
                parse_mode=HTML,
            )
            return
        await q.answer()
        await db.update_session(user.id, step="role")
        await q.edit_message_text(
            "<b>Select your role:</b>",
            reply_markup=_kb(
                _btn("🛒 Buyer", "d:role:buyer", BLUE),
                _btn("💼 Seller", "d:role:seller", GREEN),
                _btn("❌ Cancel", "d:cancel", RED),
            ),
            parse_mode=HTML,
        )

    elif action == "role" and step == "role":
        await q.answer()
        role = parts[2]
        who = "Seller" if role == "buyer" else "Buyer"
        await db.update_session(user.id, step="cp", data={"role": role})
        await q.edit_message_text(
            f"<b>Counterparty Username</b>\n\n"
            f"Enter the @username of the <b>{who}</b>.\n\n"
            "Example: @username",
            reply_markup=_kb(_btn("❌ Cancel", "d:cancel", RED)),
            parse_mode=HTML,
        )

    elif action == "cur" and step == "currency":
        await q.answer()
        cur_code = parts[2]
        await db.update_session(user.id, step="amount", data={"currency": cur_code})
        await q.edit_message_text(
            f"💰 <b>Deal Amount ({cur_code})</b>\n\n"
            "Enter the agreed amount.\n\n"
            "Example: 5000",
            reply_markup=_kb(_btn("❌ Cancel", "d:cancel", RED)),
            parse_mode=HTML,
        )

    elif action == "skip" and step == "terms":
        await q.answer()
        await _show_summary(ctx, user, q.message.chat_id, s, [], "None")

    # ---- Exchange button callbacks (and backward-compatibility handlers) ----
    elif action == "ex_give_ok" and step == "ex_give":
        await q.answer()
        s2 = await db.get_session(user.id)
        d2 = s2["data"] if s2 else {}
        confirmed_val = d2.get("_ex_give_pending", "")
        await db.update_session(user.id, step="ex_receive", data={"ex_give_amount": confirmed_val})
        await q.edit_message_text(
            "<b>What are you receiving?</b>\n\n"
            "Example: <code>5$ usdt</code>, <code>9.5 USDT</code>, <code>10 BTC</code>",
            reply_markup=_kb(_btn("❌ Cancel", "d:cancel", RED)),
            parse_mode=HTML,
        )

    elif action == "ex_give_retry" and step == "ex_give":
        await q.answer()
        await q.edit_message_text(
            "🔄 <b>What are you giving?</b>\n\n"
            "Example: <code>500 INR</code>, <code>10 USDT</code>, <code>1 BTC</code>",
            reply_markup=_kb(_btn("❌ Cancel", "d:cancel", RED)),
            parse_mode=HTML,
        )

    elif action == "ex_receive_ok" and step == "ex_receive":
        await q.answer()
        s2 = await db.get_session(user.id)
        d2 = s2["data"] if s2 else {}
        confirmed_val = d2.get("_ex_receive_pending", "")
        await db.update_session(user.id, step="ex_cp", data={"ex_receive_amount": confirmed_val})
        await q.edit_message_text(
            "<b>Counterparty Username</b>\n\nEnter the other party's @username.\n\nExample: @username",
            reply_markup=_kb(_btn("❌ Cancel", "d:cancel", RED)),
            parse_mode=HTML,
        )

    elif action == "ex_receive_retry" and step == "ex_receive":
        await q.answer()
        await q.edit_message_text(
            "<b>What are you receiving?</b>\n\n"
            "Example: <code>5$ usdt</code>, <code>9.5 USDT</code>, <code>4800 INR</code>",
            reply_markup=_kb(_btn("❌ Cancel", "d:cancel", RED)),
            parse_mode=HTML,
        )

    elif action == "ex_tat_ok" and step == "ex_tat":
        await q.answer()
        s2 = await db.get_session(user.id)
        d2 = s2["data"] if s2 else {}
        confirmed_secs = d2.get("_ex_tat_pending", 3600)
        await db.update_session(user.id, step="ex_terms", data={"ex_tat_seconds": confirmed_secs})
        await q.edit_message_text(
            "📜 <b>Deal Conditions</b>\n\nEnter agreed terms or tap Skip.",
            parse_mode=HTML,
            reply_markup=_kb(_btn("⏩ Skip", "d:ex_skip", GREEN), _btn("❌ Cancel", "d:cancel", RED)),
        )

    elif action == "ex_tat_retry" and step == "ex_tat":
        await q.answer()
        await q.edit_message_text(
            "⏱ <b>Enter delivery time (TAT)</b>\n\n"
            "Example: <code>30m</code>, <code>1h</code>, <code>2h</code>, <code>1d</code>",
            reply_markup=_kb(_btn("❌ Cancel", "d:cancel", RED)),
            parse_mode=HTML,
        )

    elif action == "ex_skip" and step == "ex_terms":
        await q.answer()
        await _show_exchange_summary(ctx, user, q.message.chat_id, s, [], "None")

    elif action == "ex_confirm" and step == "ex_summary":
        await _confirm_exchange(q, ctx, s, user)

    elif action == "cancel":
        await q.answer("Deal setup cancelled.")
        await db.delete_session(user.id)
        await q.edit_message_text("❌ Deal setup cancelled. Send /deal to start a new one.")

    elif action == "confirm" and step == "summary":
        await _confirm(q, ctx, s, user)
    else:
        await q.answer()


async def _confirm(q, ctx, s, user):
    d = s["data"]
    cp = await db.find_user_by_username(d["cp"])
    if not cp or cp["_id"] == user.id:
        await q.answer("The seller/buyer both parties must have started the bot.", show_alert=True)
        return
    p = _parties(d, user)
    me_buyer = d["role"] == "buyer"
    deal = {
        **p,
        "status": "pending",
        "initiator_id": user.id, "initiator_username": user.username, "initiator_role": d["role"],
        "other_id": cp["_id"],
        "buyer_id": user.id if me_buyer else cp["_id"],
        "seller_id": cp["_id"] if me_buyer else user.id,
        "created_at": utcnow(),
    }
    deal["seller_username" if me_buyer else "buyer_username"] = cp.get("username") or d["cp"]
    res = await db.deals.insert_one(deal)
    did = str(res.inserted_id)
    try:
        req = await ctx.bot.send_message(
            cp["_id"],
            texts.request_text(deal),
            parse_mode=HTML,
            reply_markup=_kb(_btn("✅ Accept", f"acc:{did}", GREEN), _btn("❌ Reject", f"rej:{did}", RED)),
        )
    except (Forbidden, BadRequest):
        await db.deals.delete_one({"_id": res.inserted_id})
        await q.answer("The seller/buyer both parties must have started the bot.", show_alert=True)
        return
    await db.deals.update_one({"_id": res.inserted_id}, {"$set": {"request_message_id": req.message_id}})
    await db.delete_session(user.id)
    await q.answer()
    await q.edit_message_text(
        f"📨 Deal request sent to @{esc(deal['seller_username' if me_buyer else 'buyer_username'])}.",
        parse_mode=HTML,
    )


async def _show_exchange_summary(ctx, user, chat_id, s, extra_ids, conditions):
    d = dict(s["data"])
    d["conditions"] = conditions
    await db.update_session(user.id, data={"conditions": conditions})
    await _delete_msgs(ctx.bot, chat_id, list(s["msg_ids"]) + list(extra_ids))
    ex_text = texts.exchange_summary_text(d, user.username)
    sent = await ctx.bot.send_message(
        chat_id,
        ex_text,
        parse_mode=HTML,
        reply_markup=_kb(
            _btn("✅ Confirm Exchange", "d:ex_confirm", GREEN),
            _btn("❌ Cancel", "d:cancel", RED),
        ),
    )
    await db.update_session(user.id, step="ex_summary", msg_ids=[sent.message_id])


async def _confirm_exchange(q, ctx, s, user):
    d = s["data"]
    cp_username = d.get("ex_cp", "")
    cp = await db.find_user_by_username(cp_username)
    if not cp or cp["_id"] == user.id:
        await q.answer("The other party must have started the bot first.", show_alert=True)
        return
    # ex_give_amount holds the full value e.g. "500 INR"
    # ex_receive_amount holds the full value e.g. "5$ usdt"
    give_val = d.get("ex_give_amount", "")
    receive_val = d.get("ex_receive_amount", "")
    exchange_deal = {
        "deal_type": "exchange",
        "status": "pending",
        "initiator_id": user.id,
        "initiator_username": user.username,
        "other_id": cp["_id"],
        "buyer_id": user.id,
        "buyer_username": user.username,
        "seller_id": cp["_id"],
        "seller_username": cp.get("username") or cp_username,
        "ex_give_amount": give_val,
        "ex_receive_amount": receive_val,
        "conditions": d.get("conditions", "None"),
        "tat_seconds": d.get("ex_tat_seconds", 3600),
        "purpose": f"Exchange: {give_val} → {receive_val}",
        "amount": give_val,
        "currency": "",
        "created_at": utcnow(),
    }
    res = await db.deals.insert_one(exchange_deal)
    did = str(res.inserted_id)
    req_text = texts.exchange_request_text(exchange_deal)
    try:
        req = await ctx.bot.send_message(
            cp["_id"],
            req_text,
            parse_mode=HTML,
            reply_markup=_kb(
                _btn("✅ Accept", f"acc:{did}", GREEN),
                _btn("❌ Reject", f"rej:{did}", RED),
            ),
        )
    except (Forbidden, BadRequest):
        await db.deals.delete_one({"_id": res.inserted_id})
        await q.answer("The other party must have started the bot first.", show_alert=True)
        return
    await db.deals.update_one({"_id": res.inserted_id}, {"$set": {"request_message_id": req.message_id}})
    await db.delete_session(user.id)
    await q.answer()
    await q.edit_message_text(
        f"📨 Exchange request sent to @{esc(cp.get('username') or cp_username)}.",
        parse_mode=HTML,
    )


def _oid(s):
    try:
        return ObjectId(s)
    except (InvalidId, TypeError):
        return None


async def accept_cb(update, ctx):
    q = update.callback_query
    oid = _oid(q.data.split(":", 1)[1])
    deal = await db.claim_pending(oid, q.from_user.id) if oid else None
    if not deal:
        await q.answer("This deal request is no longer available.", show_alert=True)
        return
    await q.answer()
    try:
        await q.edit_message_text("✅ Deal accepted. Creating deal room...")
    except TelegramError:
        pass
    _spawn(service.finalize_deal(ctx.application, deal))


async def reject_cb(update, ctx):
    q = update.callback_query
    oid = _oid(q.data.split(":", 1)[1])
    deal = await db.deals.find_one_and_update(
        {"_id": oid, "status": "pending", "other_id": q.from_user.id},
        {"$set": {"status": "rejected", "rejected_at": utcnow()}},
    ) if oid else None
    if not deal:
        await q.answer("This deal request is no longer available.", show_alert=True)
        return
    await q.answer()
    await q.edit_message_text("❌ Deal request declined.")


# ------------------------------------------------------------------ TAT extension (inline button callback)
async def extend_cb(update, ctx):
    q = update.callback_query
    if not _is_mm(q.from_user.id, q.from_user.username):
        await q.answer("Only the middleman can extend the TAT.", show_alert=True)
        return
    try:
        await db.callbacks.insert_one({"_id": q.id, "created_at": utcnow()})
    except DuplicateKeyError:
        await q.answer()
        return
    try:
        _, did, hours = q.data.split(":")
        hours, oid = int(hours), _oid(did)
    except ValueError:
        await q.answer()
        return
    if hours not in (1, 5, 24) or not oid or not q.message:
        await q.answer()
        return

    deal = await db.deals.find_one({"_id": oid})
    if not deal or deal.get("status") == "closed":
        await q.answer("This deal isn't active.", show_alert=True)
        return

    await service.extend_deal_tat(ctx.bot, deal, hours * 3600, f"{hours}h", q.from_user.id)
    await q.answer(f"TAT extended by {hours} hour{'s' if hours != 1 else ''}.")


# ------------------------------------------------------------------ Instant /mm Group Workflow
async def mm_dm_cmd(update, ctx):
    """Fallback when /mm is sent directly to the bot in DM."""
    user = update.effective_user
    if not _is_mm(user.id, user.username):
        await update.message.reply_text("⚠️ Only the middleman can use /mm to create deal groups.")
        return

    deal_no = await db.next_deal_no()
    label = f"#{config.DEAL_ID_PREFIX}{deal_no}"
    title = settings.render_gc_title(label, "Buyer", "Seller", deal_no=deal_no)

    info = await userbot.create_deal_group(title, ctx.bot.username)
    await service.apply_gc_photo(ctx.bot, info.chat_id)
    deal = {
        "deal_no": deal_no,
        "status": "instant_role_selection",
        "created_via": "bot_dm_mm",
        "group_chat_id": info.chat_id,
        "invite_link": info.invite_link,
        "dm_user_id": user.id,
        "dm_username": user.username,
        "buyer_id": None,
        "buyer_username": None,
        "seller_id": None,
        "seller_username": None,
        "created_at": utcnow(),
    }
    res = await db.deals.insert_one(deal)
    deal["_id"] = res.inserted_id

    await update.message.reply_text(
        f"{emojis.EMOJI_CELEBRATE} <b>Deal GC Created</b>\n\n{emojis.EMOJI_LINK} <b>Link:</b> {info.invite_link}\n\nPlease share the link with both parties and have them join to proceed.",
        parse_mode=HTML,
    )
    _spawn(service.init_instant_deal_room(ctx.application, deal))


async def instant_role_callback(update, ctx):
    q = update.callback_query
    user = q.from_user
    parts = q.data.split(":")  # inst:role:buyer:{deal_no}
    role, deal_no = parts[2], int(parts[3])

    deal = await db.get_deal_by_no(deal_no)
    if not deal:
        await q.answer("Deal not found.", show_alert=True)
        return

    if deal.get("status") == "closed":
        await q.answer("This deal is closed.", show_alert=True)
        return

    buyer_id = deal.get("buyer_id")
    seller_id = deal.get("seller_id")

    if role == "buyer":
        if buyer_id and buyer_id != user.id:
            await q.answer("Buyer role is already taken. Middleman can reassign via /buyer.", show_alert=True)
            return
        updates = {"buyer_id": user.id, "buyer_username": user.username or user.first_name}
        if seller_id == user.id:
            updates["seller_id"] = None
            updates["seller_username"] = None
        await db.update_deal(deal["_id"], updates)
        await q.answer("You are registered as the Buyer!")
        await q.message.reply_text(f"🛒 <b>Role Claimed:</b> @{esc(user.username or user.first_name)} is the <b>Buyer</b>.", parse_mode=HTML)

    elif role == "seller":
        if seller_id and seller_id != user.id:
            await q.answer("Seller role is already taken. Middleman can reassign via /seller.", show_alert=True)
            return
        updates = {"seller_id": user.id, "seller_username": user.username or user.first_name}
        if buyer_id == user.id:
            updates["buyer_id"] = None
            updates["buyer_username"] = None
        await db.update_deal(deal["_id"], updates)
        await q.answer("You are registered as the Seller!")
        await q.message.reply_text(f"💼 <b>Role Claimed:</b> @{esc(user.username or user.first_name)} is the <b>Seller</b>.", parse_mode=HTML)

    # Check if both are now claimed
    fresh = await db.get_deal_by_no(deal_no)
    if fresh.get("buyer_id") and fresh.get("seller_id"):
        # Auto-revoke invite link once buyer and seller are claimed
        await service.revoke_deal_invite(ctx.bot, fresh)

        if fresh.get("status") == "instant_role_selection":
            await db.update_deal(fresh["_id"], {"status": "instant_details_pending"})
            await q.message.reply_text(texts.instant_deal_details_prompt(), parse_mode=HTML, link_preview_options=NO_PREVIEW)


async def group_buyer_cmd(update, ctx):
    """Middleman replies to any message in group with /buyer to set that user as buyer."""
    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("Please reply to a user's message to set them as Buyer.")
        return
    if not _is_mm(msg.from_user.id, msg.from_user.username):
        await msg.reply_text("Only the middleman can reassign roles.")
        return

    deal = await db.get_deal_by_group(msg.chat_id)
    if not deal:
        return
    if deal.get("status") == "closed":
        await msg.reply_text("This deal is closed.")
        return

    target = msg.reply_to_message.from_user
    updates = {"buyer_id": target.id, "buyer_username": target.username or target.first_name}
    if deal.get("seller_id") == target.id:
        updates["seller_id"] = None
        updates["seller_username"] = None
    await db.update_deal(deal["_id"], updates)
    await msg.reply_text(f"🛒 <b>Role Updated:</b> @{esc(target.username or target.first_name)} is now the <b>Buyer</b>.", parse_mode=HTML)

    fresh = await db.get_deal_by_no(deal["deal_no"])
    if fresh.get("buyer_id") and fresh.get("seller_id"):
        await service.revoke_deal_invite(ctx.bot, fresh)
        if fresh.get("status") == "instant_role_selection":
            await db.update_deal(fresh["_id"], {"status": "instant_details_pending"})
            await msg.reply_text(texts.instant_deal_details_prompt(), parse_mode=HTML, link_preview_options=NO_PREVIEW)


async def group_seller_cmd(update, ctx):
    """Middleman replies to any message in group with /seller to set that user as seller."""
    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("Please reply to a user's message to set them as Seller.")
        return
    if not _is_mm(msg.from_user.id, msg.from_user.username):
        await msg.reply_text("Only the middleman can reassign roles.")
        return

    deal = await db.get_deal_by_group(msg.chat_id)
    if not deal:
        return
    if deal.get("status") == "closed":
        await msg.reply_text("This deal is closed.")
        return

    target = msg.reply_to_message.from_user
    updates = {"seller_id": target.id, "seller_username": target.username or target.first_name}
    if deal.get("buyer_id") == target.id:
        updates["buyer_id"] = None
        updates["buyer_username"] = None
    await db.update_deal(deal["_id"], updates)
    await msg.reply_text(f"💼 <b>Role Updated:</b> @{esc(target.username or target.first_name)} is now the <b>Seller</b>.", parse_mode=HTML)

    fresh = await db.get_deal_by_no(deal["deal_no"])
    if fresh.get("buyer_id") and fresh.get("seller_id"):
        await service.revoke_deal_invite(ctx.bot, fresh)
        if fresh.get("status") == "instant_role_selection":
            await db.update_deal(fresh["_id"], {"status": "instant_details_pending"})
            await msg.reply_text(texts.instant_deal_details_prompt(), parse_mode=HTML, link_preview_options=NO_PREVIEW)


async def buyer_confirm_deal_cb(update, ctx):
    q = update.callback_query
    deal_no = int(q.data.split(":")[2])
    deal = await db.get_deal_by_no(deal_no)
    if not deal:
        await q.answer("Deal not found.", show_alert=True)
        return

    if deal.get("status") == "closed":
        await q.answer("This deal is closed.", show_alert=True)
        return

    # Check that caller is Buyer or Middleman
    if q.from_user.id != deal.get("buyer_id") and not _is_mm(q.from_user.id, q.from_user.username):
        await q.answer("Only the designated Buyer can confirm this deal.", show_alert=True)
        return

    if deal.get("status") in ("buyer_confirmed", "deal_confirmed", "active"):
        await q.answer("Deal is already confirmed!", show_alert=True)
        return

    await q.answer("Deal confirmed!")
    await db.update_deal(deal["_id"], {"status": "buyer_confirmed", "confirmed_at": utcnow()})

    buyer_tag = f"@{esc(deal.get('buyer_username') or 'Buyer')}"
    chat_id = deal.get("group_chat_id")

    # Update Confirm button to indicate buyer confirmation
    try:
        await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirmed by Buyer", callback_data="noop")]
        ]))
    except Exception:
        pass

    # Unpin the role-selection message if it exists
    role_msg_id = deal.get("pinned_role_message_id")
    if role_msg_id and chat_id:
        try:
            await ctx.bot.unpin_chat_message(chat_id, role_msg_id)
        except Exception:
            pass

    # Pin the deal summary message
    if chat_id:
        try:
            await ctx.bot.pin_chat_message(chat_id, q.message.message_id, disable_notification=True)
        except Exception:
            pass
        await db.update_deal(deal["_id"], {"pinned_deal_info_id": q.message.message_id})

    # Mention Middleman with button to confirm and proceed to payment
    mm_kb = InlineKeyboardMarkup([
        [_btn("Proceed to Payment", f"inst:mm_proceed:{deal_no}", GREEN, icon=emojis.get_custom_id("SUCCESS"))],
        [_btn("✏️ Edit Deal", f"editdeal:menu:{deal_no}", BLUE)],
    ])
    await q.message.reply_text(
        texts.buyer_confirmed_wait_mm_text(buyer_tag),
        reply_markup=mm_kb,
        parse_mode=HTML,
    )


async def mm_proceed_payment_cb(update, ctx):
    q = update.callback_query
    deal_no = int(q.data.split(":")[2])
    deal = await db.get_deal_by_no(deal_no)
    if not deal:
        await q.answer("Deal not found.", show_alert=True)
        return

    if not _is_mm(q.from_user.id, q.from_user.username):
        await q.answer("Only the middleman can confirm to proceed for payment.", show_alert=True)
        return

    if deal.get("status") == "closed":
        await q.answer("This deal is closed.", show_alert=True)
        return

    await q.answer("Proceeding to payment!")
    await db.update_deal(deal["_id"], {"status": "deal_confirmed", "mm_approved_at": utcnow()})

    # Update the MM proceed button so it cannot be double-tapped
    try:
        await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Approved by Middleman", callback_data="noop")]
        ]))
    except Exception:
        pass

    fee = deal.get("custom_fee") or settings.get("default_fee", "4%")
    amt = str(deal.get("amount", "0"))
    curr = str(deal.get("currency", "INR")).upper()
    net = str(deal.get("payment_network", "UPI")).upper()

    is_crypto = (
        curr in ("USDT", "BTC", "ETH", "SOL", "TON", "CRYPTO", "LTC", "TRX", "BNB", "XRP", "DOGE",
                 "USD", "EUR", "GBP", "USDC", "ARB", "BASE",
                 "POL", "MATIC", "AVAX", "XMR", "ADA", "DOT", "NEAR", "BCH", "SUI", "APT")
        or "CRYPTO" in curr
        or ("CRYPTO" in net and curr != "INR")
    )

    if is_crypto:
        # Step 1: Crypto payment network selection
        kb = _build_crypto_menu(deal_no, curr, page=0)
        kb.inline_keyboard.append([
            _btn("✏️ Edit Amount / Payment", f"editamt:start:{deal_no}", BLUE)
        ])
        await q.message.reply_text(
            texts.payment_details_crypto_prompt(amt, curr, fee),
            reply_markup=kb,
            parse_mode=HTML,
        )
    else:
        # Step 2: Consolidated INR payment card with immediate action button
        upi_id = settings.get("upi_id", config.DEFAULT_UPI_ID) or f"Contact @{config.MM_USERNAME} for UPI ID"
        buyer_kb = InlineKeyboardMarkup([
            [_btn("✅ I've sent the payment", f"inst:buyer_paid:{deal_no}", GREEN, icon=emojis.get_custom_id("SUCCESS"))],
            [_btn("✏️ Edit Amount / Payment", f"editamt:start:{deal_no}", BLUE)],
        ])
        await q.message.reply_text(
            texts.payment_details_upi(amt, curr, fee, upi_id),
            reply_markup=buyer_kb,
            parse_mode=HTML,
        )


async def noop_cb(update, ctx):
    if update.callback_query:
        await update.callback_query.answer()


def _build_crypto_menu(deal_no: int, currency: str = "Crypto", page: int = 0) -> InlineKeyboardMarkup:
    all_coins = list(settings.CRYPTO_NETWORKS.items())
    curr_upper = (currency or "CRYPTO").upper()

    if curr_upper == "USDT":
        filtered = [item for item in all_coins if item[0].startswith("usdt_")]
    elif curr_upper in ("BTC", "BITCOIN"):
        filtered = [item for item in all_coins if item[0] == "btc"]
    elif curr_upper in ("ETH", "ETHEREUM"):
        filtered = [item for item in all_coins if item[0] in ("eth", "base", "arb")]
    elif curr_upper in ("SOL", "SOLANA"):
        filtered = [item for item in all_coins if item[0] in ("sol", "usdt_sol")]
    elif curr_upper in ("TON", "TONCOIN"):
        filtered = [item for item in all_coins if item[0] in ("ton", "usdt_ton")]
    elif curr_upper in ("LTC", "TRX", "BNB", "XRP", "DOGE", "POL", "MATIC", "AVAX", "XMR", "ADA", "DOT", "NEAR", "BCH", "SUI", "APT", "USDC"):
        k_key = curr_upper.lower()
        filtered = [item for item in all_coins if item[0] == k_key or item[1].get("symbol") == curr_upper]
    else:
        filtered = all_coins

    if not filtered:
        filtered = all_coins

    page_size = 6
    total_pages = (len(filtered) + page_size - 1) // page_size
    page = max(0, min(page, max(0, total_pages - 1)))

    current_slice = filtered[page * page_size : (page + 1) * page_size]
    btns = []
    for key, info in current_slice:
        btns.append([_btn(f"🪙 {info['symbol']} ({info['network']})", f"inst:crypto_pick:{deal_no}:{key}", BLUE)])

    nav = []
    if page > 0:
        nav.append(_btn("⬅️ Prev", f"inst:crypto_page:{deal_no}:{page-1}", BLUE))
    if page < total_pages - 1:
        nav.append(_btn("Next ➡️", f"inst:crypto_page:{deal_no}:{page+1}", BLUE))
    if nav:
        btns.append(nav)

    return InlineKeyboardMarkup(btns)


async def instant_crypto_callback(update, ctx):
    q = update.callback_query
    parts = q.data.split(":")
    action = parts[1]
    deal_no = int(parts[2])
    deal = await db.get_deal_by_no(deal_no)
    curr = deal.get("currency", "Crypto") if deal else "Crypto"

    if action == "crypto_page":
        page = int(parts[3])
        await q.answer()
        await q.edit_reply_markup(_build_crypto_menu(deal_no, curr, page))

    elif action == "crypto_pick":
        coin_key = parts[3]
        info = settings.CRYPTO_NETWORKS.get(coin_key, {})
        addr = settings.get_crypto_wallet(coin_key)

        if not addr:
            await q.answer(f"⚠️ No wallet configured for {info.get('name', coin_key)}. Contact @{config.MM_USERNAME}", show_alert=True)
            return

        await q.answer()

        t = texts.payment_details_crypto_address(
            coin_name=info.get("name", coin_key),
            symbol=info.get("symbol", coin_key),
            network=info.get("network", "N/A"),
            address=addr,
        )

        buyer_kb = _kb(_btn("✅ I've sent the payment", f"inst:buyer_paid:{deal_no}", GREEN, icon=emojis.get_custom_id("SUCCESS")))
        try:
            await q.edit_message_text(t, parse_mode=HTML, reply_markup=buyer_kb)
        except Exception:
            await q.message.reply_text(t, parse_mode=HTML, reply_markup=buyer_kb)


async def payment_sent_cb(update, ctx):
    q = update.callback_query
    parts = q.data.split(":")
    action = parts[1]
    deal_no = int(parts[2])
    deal = await db.get_deal_by_no(deal_no)
    if not deal:
        await q.answer()
        return

    if deal.get("buyer_payment_confirmed"):
        await q.answer("Payment confirmation has already been recorded.", show_alert=True)
        return

    if action == "buyer_paid":
        # Buyer reports they sent payment
        if q.from_user.id != deal.get("buyer_id") and not _is_mm(q.from_user.id):
            await q.answer("Only the buyer can report payment.", show_alert=True)
            return
        await q.answer("Payment reported!")
        await db.update_deal(deal["_id"], {"buyer_payment_confirmed": True, "buyer_paid_at": utcnow()})

        buyer_tag = f"@{esc(deal.get('buyer_username') or 'Buyer')}"
        mm_kb = _kb(_btn("🛡️ Verify Payment Received", f"inst:mm_paid:{deal_no}", GREEN, icon=emojis.get_custom_id("MONEY")))
        await q.message.reply_text(
            f"⏳ <b>Payment Reported</b>\n\n"
            f"{buyer_tag} reported payment sent.\n"
            f"Waiting for escrower @{config.MM_USERNAME} to verify receipt.",
            reply_markup=mm_kb,
            parse_mode=HTML,
        )
        return

    # Legacy: "paid_sent" fallback
    await q.answer("Payment reported!")
    await db.update_deal(deal["_id"], {"buyer_payment_confirmed": True, "buyer_paid_at": utcnow()})

    buyer_tag = f"@{esc(deal.get('buyer_username') or 'Buyer')}"
    mm_kb = _kb(_btn("🛡️ Verify Payment Received", f"inst:mm_paid:{deal_no}", GREEN, icon=emojis.get_custom_id("MONEY")))
    await q.message.reply_text(
        f"⏳ <b>Payment Reported</b>\n\n"
        f"{buyer_tag} reported payment sent.\n"
        f"Waiting for escrower @{config.MM_USERNAME} to verify receipt.",
        reply_markup=mm_kb,
        parse_mode=HTML,
    )


async def mm_confirm_payment_cb(update, ctx):
    q = update.callback_query
    if not _is_mm(q.from_user.id, q.from_user.username):
        await q.answer("Only the middleman can verify payment receipt.", show_alert=True)
        return

    deal_no = int(q.data.split(":")[2])
    deal = await db.get_deal_by_no(deal_no)
    if not deal:
        await q.answer("Deal not found.", show_alert=True)
        return

    if deal.get("status") == "closed":
        await q.answer("Deal is closed.", show_alert=True)
        return

    await q.answer("Payment verified!")
    tat_secs = deal.get("tat_seconds")
    if not tat_secs or tat_secs <= 0:
        await q.message.reply_text(
            f"{emojis.EMOJI_WARNING} <b>TAT not set!</b>\n\n"
            "Configure turnaround time using <code>/settat &lt;duration&gt;</code> before starting the deal.",
            parse_mode=HTML,
        )
        return

    await service.start_deal_tat(ctx.bot, deal, q.from_user.id)


# ------------------------------------------------------------------ Middleman Deal Commands in Group
async def settat_cmd(update, ctx):
    """Middleman /settat command: sets the turnaround time for the deal."""
    msg = update.message
    if not _is_mm(msg.from_user.id, msg.from_user.username):
        await msg.reply_text("⚠️ This command is reserved for the middleman.")
        return

    deal = await db.get_deal_by_group(msg.chat_id)
    if not deal:
        await msg.reply_text("❌ No deal found in this group.")
        return

    if deal.get("status") == "closed":
        await msg.reply_text("❌ This deal is closed.")
        return

    if not ctx.args:
        await msg.reply_text(
            "⚠️ <b>Usage:</b> <code>/settat &lt;duration&gt;</code>\n\n"
            "<b>Examples:</b>\n"
            "• <code>/settat 15m</code> or <code>15mins</code>\n"
            "• <code>/settat 1h</code> or <code>2hours</code>\n"
            "• <code>/settat 1d</code> or <code>3days</code>\n"
            "• <code>/settat 1d 12h</code>",
            parse_mode=HTML,
        )
        return

    raw_arg = " ".join(ctx.args).strip()
    try:
        secs = parse_tat(raw_arg)
    except ValueError as e:
        await msg.reply_text(str(e), parse_mode=HTML)
        return

    updates = {"tat_seconds": secs, "tat_str": raw_arg}
    if deal.get("status") == "active" and deal.get("tat_started_at"):
        now = utcnow()
        new_deadline = now + timedelta(seconds=secs)
        updates["tat_deadline"] = new_deadline
        updates["expiry_notified"] = False

    await db.update_deal(deal["_id"], updates)
    await db.record_deal_audit(deal["_id"], "settat", msg.from_user.id, {"seconds": secs, "input": raw_arg})
    cid = emojis.get_custom_id("CHANNEL")
    btn_kw = {"api_kwargs": {"icon_custom_emoji_id": cid}} if cid else {}
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Middleman Channel", url=settings.get("mm_channel_url", config.CHANNEL_URL), **btn_kw)],
        [InlineKeyboardButton("⏱ Edit TAT", callback_data=f"tatedit:menu:{deal['deal_no']}")],
    ])
    await msg.reply_text(
        f"⏱ <b>TAT Configured</b>\n\n"
        f"Duration: <b>{fmt_duration(secs)}</b>\n"
        "Execute /begin when payment is verified to initiate countdown.",
        reply_markup=kb,
        parse_mode=HTML,
    )


async def begin_cmd(update, ctx):
    """Middleman /begin command: confirms payment and activates TAT countdown."""
    msg = update.message
    if not _is_mm(msg.from_user.id, msg.from_user.username):
        await msg.reply_text("⚠️ This command is reserved for the middleman.")
        return

    deal = await db.get_deal_by_group(msg.chat_id)
    if not deal:
        await msg.reply_text("❌ No deal found in this group.")
        return

    if deal.get("status") == "closed":
        await msg.reply_text("❌ This deal is closed.")
        return

    tat_secs = deal.get("tat_seconds")
    if not tat_secs or tat_secs <= 0:
        await msg.reply_text(
            f"{emojis.EMOJI_WARNING} <b>TAT duration is missing!</b>\n\n"
            "Please configure the TAT first using:\n"
            "<code>/settat &lt;duration&gt;</code> (e.g. <code>/settat 24h</code> or <code>/settat 1d</code>)",
            parse_mode=HTML,
        )
        return

    await service.start_deal_tat(ctx.bot, deal, msg.from_user.id)


async def extendtat_cmd(update, ctx):
    """Middleman /extendtat command: extends the active deadline."""
    msg = update.message
    if not _is_mm(msg.from_user.id, msg.from_user.username):
        await msg.reply_text("⚠️ This command is reserved for the middleman.")
        return

    deal = await db.get_deal_by_group(msg.chat_id)
    if not deal:
        await msg.reply_text("❌ No deal found in this group.")
        return

    if deal.get("status") == "closed":
        await msg.reply_text("❌ This deal is closed.")
        return

    if not ctx.args:
        await msg.reply_text(
            "⚠️ <b>Usage:</b> <code>/extendtat &lt;duration&gt;</code>\n\n"
            "<b>Examples:</b>\n"
            "• <code>/extendtat 30m</code>\n"
            "• <code>/extendtat 1h</code>\n"
            "• <code>/extendtat 5hours</code>\n"
            "• <code>/extendtat 1d</code>",
            parse_mode=HTML,
        )
        return

    raw_arg = " ".join(ctx.args).strip()
    try:
        ext_secs = parse_tat(raw_arg)
    except ValueError as e:
        await msg.reply_text(str(e), parse_mode=HTML)
        return

    await service.extend_deal_tat(ctx.bot, deal, ext_secs, raw_arg, msg.from_user.id)


async def hold_cmd(update, ctx):
    """Middleman /hold command: updates group title to 'MM <No> <Currency> hold', pauses TAT, and sets status to held."""
    msg = update.message
    if not _is_mm(msg.from_user.id, msg.from_user.username):
        await msg.reply_text("⚠️ This command is reserved for the middleman.")
        return

    deal = await db.get_deal_by_group(msg.chat_id)
    if not deal:
        await msg.reply_text("❌ No deal found in this group.")
        return

    if deal.get("status") == "closed":
        await msg.reply_text("❌ This deal is closed.")
        return

    if deal.get("status") == "held":
        resume_kb = _kb(_btn("🔓 Unhold / Resume Deal", f"hold:unhold:{deal['deal_no']}", GREEN))
        await msg.reply_text("ℹ️ This deal is already on hold. Tap below or send /unhold to resume.", reply_markup=resume_kb)
        return

    reason = " ".join(ctx.args).strip() if ctx.args else ""
    now = utcnow()
    tat_rem = None
    if deal.get("tat_deadline") and deal["tat_deadline"] > now:
        tat_rem = max(0, int((deal["tat_deadline"] - now).total_seconds()))

    new_title = await service.set_deal_title_hold(ctx.bot, deal)
    upd = {
        "status": "held",
        "prev_status": deal.get("status", "active"),
        "held_at": now,
        "tat_remaining_seconds": tat_rem,
        "hold_reason": reason,
    }
    await db.update_deal(deal["_id"], upd)
    await db.record_deal_audit(deal["_id"], "hold", msg.from_user.id, {"title": new_title, "reason": reason})

    b_un = deal.get("buyer_username")
    s_un = deal.get("seller_username")
    b_tag = f"@{esc(b_un)}" if b_un else "Buyer"
    s_tag = f"@{esc(s_un)}" if s_un else "Seller"
    amt = str(deal.get("amount", ""))
    curr = str(deal.get("currency", "INR"))
    tat_str = str(deal.get("tat", "24h"))
    deadline_str = fmt_dt(deal["tat_deadline"]) if deal.get("tat_deadline") else tat_str

    hold_text = settings.render_template(
        "hold",
        deal_id=str(deal.get("deal_no", "")),
        title=new_title,
        mm=config.MM_USERNAME,
        buyer=b_tag,
        seller=s_tag,
        amount=amt,
        currency=curr,
        tat=tat_str,
        deadline=deadline_str,
    )
    if reason:
        hold_text += f"\n\n<b>Reason:</b> {esc(reason)}"

    resume_kb = _kb(_btn("🔓 Unhold / Resume Deal", f"hold:unhold:{deal['deal_no']}", GREEN))
    await msg.reply_text(hold_text, reply_markup=resume_kb, parse_mode=HTML)


async def unhold_cmd(update, ctx):
    """Middleman /unhold command: restores deal status, resumes TAT countdown, and restores title."""
    msg = update.message
    if not _is_mm(msg.from_user.id, msg.from_user.username):
        await msg.reply_text("⚠️ This command is reserved for the middleman.")
        return

    deal = await db.get_deal_by_group(msg.chat_id)
    if not deal:
        await msg.reply_text("❌ No deal found in this group.")
        return

    if deal.get("status") != "held":
        await msg.reply_text("ℹ️ This deal is not on hold.")
        return

    await _resume_held_deal(ctx.bot, deal, msg.chat_id, msg.from_user.id)


async def _resume_held_deal(bot, deal, chat_id: int, actor_id: int):
    target_status = deal.get("prev_status") or "active"
    now = utcnow()
    upd = {"status": target_status, "unheld_at": now}

    if deal.get("tat_remaining_seconds"):
        new_deadline = now + timedelta(seconds=deal["tat_remaining_seconds"])
        upd["tat_deadline"] = new_deadline
        upd["expiry_notified"] = False

    new_title = await service.set_deal_title_unhold(bot, deal)
    await db.update_deal(deal["_id"], upd)
    await db.record_deal_audit(deal["_id"], "unhold", actor_id, {"title": new_title, "resumed_status": target_status})

    deal_no = deal.get("deal_no", "")
    deadline_info = f"\nTurnaround time resumed (ends {fmt_dt(upd['tat_deadline'])})." if "tat_deadline" in upd else ""
    text = (
        f"🔓 <b>DEAL RESUMED</b>\n\n"
        f"Deal #{deal_no} has been taken off hold by @{config.MM_USERNAME}.\n"
        f"Status: <b>{target_status.replace('_', ' ').title()}</b>"
        f"{deadline_info}"
    )
    await safe_send(bot, chat_id, text)


async def hold_callback(update, ctx):
    q = update.callback_query
    if not _is_mm(q.from_user.id, q.from_user.username):
        await q.answer("Only the middleman can resume this deal.", show_alert=True)
        return
    parts = q.data.split(":")
    deal_no = int(parts[2])
    deal = await db.get_deal_by_no(deal_no)
    if not deal:
        await q.answer("Deal not found.", show_alert=True)
        return
    if deal.get("status") != "held":
        await q.answer("Deal is not on hold.", show_alert=True)
        return
    await q.answer("Resuming deal...")
    chat_id = deal.get("group_chat_id") or q.message.chat_id
    await _resume_held_deal(ctx.bot, deal, chat_id, q.from_user.id)


async def release_cmd(update, ctx):
    """Middleman /release command: updates group title to 'MM <No> <Currency> Released' and sets status to released."""
    msg = update.message
    if not _is_mm(msg.from_user.id, msg.from_user.username):
        await msg.reply_text("⚠️ This command is reserved for the middleman.")
        return

    deal = await db.get_deal_by_group(msg.chat_id)
    if not deal:
        await msg.reply_text("❌ No deal found in this group.")
        return

    if deal.get("status") == "closed":
        await msg.reply_text("❌ This deal is closed.")
        return

    new_title = await service.set_deal_title_release(ctx.bot, deal)
    await db.update_deal(deal["_id"], {"status": "released", "released_at": utcnow()})
    await db.record_deal_audit(deal["_id"], "release", msg.from_user.id, {"title": new_title})

    b_un = deal.get("buyer_username")
    s_un = deal.get("seller_username")
    b_tag = f"@{esc(b_un)}" if b_un else "Buyer"
    s_tag = f"@{esc(s_un)}" if s_un else "Seller"
    amt = str(deal.get("amount", ""))
    curr = str(deal.get("currency", "INR"))
    tat_str = str(deal.get("tat", "24h"))

    release_text = settings.render_template(
        "release",
        deal_id=str(deal.get("deal_no", "")),
        title=new_title,
        mm=config.MM_USERNAME,
        buyer=b_tag,
        seller=s_tag,
        amount=amt,
        currency=curr,
        tat=tat_str,
    )
    await msg.reply_text(release_text, parse_mode=HTML)


async def close_cmd(update, ctx):
    """Middleman /close command: asks for confirmation before closing deal."""
    msg = update.message
    if not _is_mm(msg.from_user.id, msg.from_user.username):
        await msg.reply_text("⚠️ This command is reserved for the middleman.")
        return

    deal = await db.get_deal_by_group(msg.chat_id)
    if not deal:
        await msg.reply_text("❌ No deal found in this group.")
        return

    if deal.get("status") == "closed":
        await msg.reply_text("ℹ️ This deal is already closed.")
        return

    deal_no = deal["deal_no"]
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔴 Confirm Close", callback_data=f"deal:close_confirm:{deal_no}", api_kwargs={"style": "danger"}),
            InlineKeyboardButton("⚪ Cancel", callback_data=f"deal:close_cancel:{deal_no}"),
        ]
    ])
    await msg.reply_text(
        f"⚠️ <b>Close Deal #{deal_no}?</b>\n\n"
        "• Countdown will be stopped.\n"
        "• Buyer and seller will be muted.\n"
        "• Invite links will be revoked.",
        reply_markup=kb,
        parse_mode=HTML,
    )


async def deal_close_callback(update, ctx):
    q = update.callback_query
    if not _is_mm(q.from_user.id, q.from_user.username):
        await q.answer("Only the middleman can close this deal.", show_alert=True)
        return

    parts = q.data.split(":")
    action, deal_no = parts[1], int(parts[2])
    deal = await db.get_deal_by_no(deal_no)
    if not deal:
        await q.answer("Deal not found.", show_alert=True)
        return

    if action == "close_cancel":
        await q.answer("Closure cancelled.")
        await q.edit_message_text("❌ <b>Deal closure cancelled.</b>", parse_mode=HTML)
        return

    if action == "close_confirm":
        await q.answer("Closing deal...")
        if deal.get("status") == "closed":
            await q.edit_message_text("ℹ️ Deal is already closed.", parse_mode=HTML)
            return

        # 1. Update database atomically
        await db.close_deal(deal["_id"], q.from_user.id)

        # 2. Revoke invitation links
        await service.revoke_deal_invite(ctx.bot, deal)

        # 3. Mute participants
        await service.mute_deal_participants(
            ctx.bot,
            deal["group_chat_id"],
            deal.get("buyer_id"),
            deal.get("seller_id"),
        )

        # 4. Notify group
        closure_text = settings.render_template(
            "deal_closure",
            deal_id=str(deal_no),
            mm=config.MM_USERNAME,
        )
        await q.edit_message_text("🚪 <b>Deal Closed.</b>", parse_mode=HTML)
        await safe_send(ctx.bot, deal["group_chat_id"], closure_text)


async def paid_cmd(update, ctx):
    """Middleman /paid command in group chat."""
    msg = update.message
    if not _is_mm(msg.from_user.id, msg.from_user.username):
        await msg.reply_text("Only the middleman can confirm payments.")
        return

    deal = await db.get_deal_by_group(msg.chat_id)
    if not deal:
        await msg.reply_text("No active deal found in this group.")
        return

    if deal.get("status") == "closed":
        await msg.reply_text("This deal is closed.")
        return

    tat_secs = deal.get("tat_seconds")
    if not tat_secs or tat_secs <= 0:
        await msg.reply_text(
            f"{emojis.EMOJI_WARNING} <b>TAT not set!</b>\n\n"
            "Please configure the turnaround time using <code>/settat &lt;duration&gt;</code> before starting the deal.",
            parse_mode=HTML,
        )
        return

    await service.start_deal_tat(ctx.bot, deal, msg.from_user.id)


async def complete_cmd(update, ctx):
    """Middleman /complete or /done command to finish deal and output vouch."""
    msg = update.message
    if not _is_mm(msg.from_user.id, msg.from_user.username):
        await msg.reply_text("Only the middleman can complete deals.")
        return

    deal = await db.get_deal_by_group(msg.chat_id)
    if not deal:
        await msg.reply_text("No active deal found in this group.")
        return

    if deal.get("status") == "closed":
        await msg.reply_text("This deal is already closed.")
        return

    await db.update_deal(deal["_id"], {"status": "completed", "completed_at": utcnow()})
    vouch_text = settings.render_vouch(deal)

    vouch_msg = await msg.reply_text(
        f"✅ <b>Deal Completed Successfully</b>\n\n"
        f"Funds released. Deal #{deal['deal_no']} is complete.\n\n"
        f"<b>Drop a vouch:</b>\n<code>{vouch_text}</code>",
        parse_mode=HTML,
        link_preview_options=NO_PREVIEW,
    )

    # Prompt Middleman with confirmation button to forward vouch to vouches channel
    vouch_target = parse_channel_target(settings.get("vouch_channel_url", config.VOUCH_CHANNEL_URL))
    fwd_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📢 Forward to Vouches Channel", callback_data=f"vouch:fwd:{deal['deal_no']}:{vouch_msg.message_id}"),
            InlineKeyboardButton("❌ Don't Forward", callback_data=f"vouch:no:{deal['deal_no']}"),
        ]
    ])
    await msg.reply_text(
        f"📢 <b>Forward Vouch to Channel?</b>\n\n"
        f"Destination: <code>{esc(str(vouch_target))}</code>\n"
        f"<i>(Only middleman @{config.MM_USERNAME} can confirm)</i>",
        reply_markup=fwd_kb,
        parse_mode=HTML,
    )


async def vouch_callback(update, ctx):
    """Handles vouch forwarding confirmation (visible/actionable by middleman only)."""
    q = update.callback_query
    user = q.from_user
    if not _is_mm(user.id, user.username):
        await q.answer(f"⚠️ Visible only to middleman: only @{config.MM_USERNAME} can confirm forwarding.", show_alert=True)
        return

    parts = q.data.split(":")
    action = parts[1]
    deal_no = int(parts[2])

    if action == "no":
        await q.answer("Vouch forwarding dismissed.")
        await q.edit_message_text("❌ <b>Vouch forwarding dismissed.</b>", parse_mode=HTML)
        return

    deal = await db.get_deal_by_no(deal_no)
    target_channel = parse_channel_target(settings.get("vouch_channel_url", config.VOUCH_CHANNEL_URL))
    if not target_channel:
        await q.answer("Vouch channel is not configured in settings.", show_alert=True)
        return

    await q.answer("Forwarding vouch...")
    if action == "fwd_msg":
        msg_id = int(parts[3])
        chat_id = deal.get("group_chat_id") if deal else q.message.chat_id
        try:
            await ctx.bot.forward_message(chat_id=target_channel, from_chat_id=chat_id, message_id=msg_id)
            await q.edit_message_text(f"✅ <b>Vouch forwarded to {esc(str(target_channel))}!</b>", parse_mode=HTML)
            if deal:
                await db.update_deal(deal["_id"], {"vouch_forwarded": True, "vouch_forwarded_at": utcnow()})
        except Exception as e:
            log.warning("Could not forward vouch: %s", e)
            try:
                vouch_text = settings.render_vouch(deal) if deal else "Vouch received!"
                await ctx.bot.send_message(chat_id=target_channel, text=vouch_text, parse_mode=HTML, link_preview_options=NO_PREVIEW)
                await q.edit_message_text(f"✅ <b>Vouch posted to {esc(str(target_channel))}!</b>", parse_mode=HTML)
                if deal:
                    await db.update_deal(deal["_id"], {"vouch_forwarded": True, "vouch_forwarded_at": utcnow()})
            except Exception as e2:
                await q.edit_message_text(
                    f"⚠️ <b>Could not forward to {esc(str(target_channel))}:</b> {esc(str(e2))}\n\n"
                    f"Please ensure @{ctx.bot.username} is added as an <b>Admin</b> in the channel with <b>Post Messages</b> permission.",
                    parse_mode=HTML,
                )
    elif action == "fwd":
        vouch_text = settings.render_vouch(deal)
        try:
            await ctx.bot.send_message(chat_id=target_channel, text=vouch_text, parse_mode=HTML, link_preview_options=NO_PREVIEW)
            await q.edit_message_text(f"✅ <b>Vouch successfully posted to {esc(str(target_channel))}!</b>", parse_mode=HTML)
            if deal:
                await db.update_deal(deal["_id"], {"vouch_forwarded": True, "vouch_forwarded_at": utcnow()})
        except Exception as e:
            log.warning("Could not post vouch: %s", e)
            await q.edit_message_text(
                f"⚠️ <b>Could not post to {esc(str(target_channel))}:</b> {esc(str(e))}\n\n"
                f"Please ensure @{ctx.bot.username} is added as an <b>Admin</b> in the channel with <b>Post Messages</b> permission.",
                parse_mode=HTML,
            )


# ------------------------------------------------------------------ Strict Vouch Detection
def is_matching_vouch(text: str, deal: dict) -> bool:
    """Strict vouch detection.
    Checks if text closely resembles the configured vouch format in settings for this deal.
    Does NOT guess or trigger on casual words like 'vouch', '+rep', 'thanks', etc.
    Requires:
    1. Middleman username mention (@mm or mm)
    2. Vouch keyword (vouch, vouched, +rep, rep+)
    3. Resembles configured vouch format (mentions deal amount, deal no, or high template similarity)
    """
    if not text or not deal:
        return False

    t_clean = text.lower().strip()
    mm_name = config.MM_USERNAME.lower().lstrip("@")

    # 1. Must mention middleman
    if mm_name not in t_clean and f"@{mm_name}" not in t_clean:
        return False

    # 2. Must contain an explicit vouch action word
    has_vouch_kw = any(w in t_clean for w in ("vouch", "vouched", "+rep", "rep+"))
    if not has_vouch_kw:
        return False

    # 3. Reject casual chatter like "can you vouch", "please vouch", "thanks mm", "is he legit"
    if any(q in t_clean for q in ("can you vouch", "plz vouch", "please vouch", "need vouch", "how to vouch", "is he legit")):
        return False

    # 4. Compare with configured expected vouch template
    try:
        expected = settings.render_vouch(deal).lower()
    except Exception:
        expected = f"vouch @{mm_name} for mm'd {deal.get('amount', '')} {deal.get('currency', '')}".lower()

    deal_amt = str(deal.get("amount", "")).replace(",", "").strip()
    deal_no = str(deal.get("deal_no", "")).strip()

    # If message explicitly references the deal amount or deal number along with mm & vouch
    if deal_amt and deal_amt in t_clean:
        return True
    if deal_no and (f"#{deal_no}" in t_clean or f"mmd {deal_no}" in t_clean):
        return True

    # Similarity ratio check against configured template
    ratio = difflib.SequenceMatcher(None, t_clean, expected).ratio()
    if ratio >= 0.55:
        return True

    # Token overlap check with expected template words
    exp_tokens = set(re.findall(r"\w+", expected))
    msg_tokens = set(re.findall(r"\w+", t_clean))
    if exp_tokens and (len(exp_tokens & msg_tokens) / len(exp_tokens)) >= 0.6:
        return True

    return False


# ------------------------------------------------------------------ /ai Command in Group Chat (Hinglish Reasoning)
async def ai_cmd(update, ctx):
    """Middleman /ai command: Analyzes group chat buyer & seller messages and deal details
    and outputs a 5-6 line reasoning/conclusion in Hinglish."""
    msg = update.message
    if not _is_mm(msg.from_user.id, msg.from_user.username):
        await msg.reply_text("⚠️ This command is reserved for the middleman.")
        return

    deal = await db.get_deal_by_group(msg.chat_id)
    if not deal:
        await msg.reply_text("❌ No deal found in this group.")
        return

    status_msg = await msg.reply_text("🤖 <i>Analyzing chat & deal with AI (Hinglish)...</i>", parse_mode=HTML)

    chat_messages = []
    try:
        chat_messages = await userbot.get_recent_messages(msg.chat_id, limit=35)
    except Exception as e:
        log.warning("Could not fetch messages from userbot: %s", e)

    extra_context = ""
    if msg.reply_to_message and (msg.reply_to_message.text or msg.reply_to_message.caption):
        reply_txt = msg.reply_to_message.text or msg.reply_to_message.caption
        sender = msg.reply_to_message.from_user.first_name if msg.reply_to_message.from_user else "User"
        extra_context = f"{sender}: {reply_txt}"
    elif ctx.args:
        extra_context = " ".join(ctx.args).strip()

    reasoning = await ai_parser.generate_deal_reasoning(deal, chat_messages, extra_context)

    kb = InlineKeyboardMarkup([
        [
            _btn("🔄 Refresh Summary", f"ai:refresh:{deal['deal_no']}", BLUE),
            _btn("❌ Close", f"ai:close:{deal['deal_no']}", RED),
        ]
    ])

    out_text = (
        f"🛡️ <b>Deal #{deal['deal_no']} Summary</b>\n\n"
        f"{reasoning}"
    )

    await status_msg.edit_text(out_text, reply_markup=kb, parse_mode=HTML)


async def ai_callback(update, ctx):
    q = update.callback_query
    if not _is_mm(q.from_user.id, q.from_user.username):
        await q.answer("Only the middleman can view deal summary.", show_alert=True)
        return

    parts = q.data.split(":")
    action = parts[1]
    deal_no = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None

    if action == "close":
        await q.answer()
        try:
            await q.message.delete()
        except Exception:
            await q.edit_message_text("❌ Deal summary closed.")
        return

    if action == "refresh":
        await q.answer("Refreshing deal summary...")
        deal = await db.get_deal_by_no(deal_no) if deal_no else await db.get_deal_by_group(q.message.chat_id)
        if not deal:
            await q.answer("Deal not found.", show_alert=True)
            return

        chat_messages = []
        try:
            chat_messages = await userbot.get_recent_messages(q.message.chat_id, limit=35)
        except Exception as e:
            log.warning("Could not fetch messages from userbot: %s", e)

        reasoning = await ai_parser.generate_deal_reasoning(deal, chat_messages)
        kb = InlineKeyboardMarkup([
            [
                _btn("🔄 Refresh Summary", f"ai:refresh:{deal['deal_no']}", BLUE),
                _btn("❌ Close", f"ai:close:{deal['deal_no']}", RED),
            ]
        ])
        out_text = (
            f"🛡️ <b>Deal #{deal['deal_no']} Summary</b>\n\n"
            f"{reasoning}"
        )
        try:
            await q.edit_message_text(out_text, reply_markup=kb, parse_mode=HTML)
        except Exception:
            pass
        return


# ------------------------------------------------------------------ /edits Quick Deal Editor Command & Callbacks
async def edits_cmd(update, ctx):
    """Middleman /edits command: Quick deal editor with natural language & interactive menu."""
    msg = update.message
    if not _is_mm(msg.from_user.id, msg.from_user.username):
        await msg.reply_text("⚠️ This command is reserved for the middleman.")
        return

    deal = await db.get_deal_by_group(msg.chat_id)
    if not deal:
        await msg.reply_text("❌ No deal found in this group.")
        return

    if deal.get("status") == "closed":
        await msg.reply_text("❌ This deal is closed.")
        return

    text_to_parse = ""
    if msg.reply_to_message and (msg.reply_to_message.text or msg.reply_to_message.caption):
        text_to_parse = msg.reply_to_message.text or msg.reply_to_message.caption
    elif ctx.args:
        text_to_parse = " ".join(ctx.args).strip()

    if text_to_parse:
        status_msg = await msg.reply_text("✏️ <i>Applying deal edits...</i>", parse_mode=HTML)
        edits = await ai_parser.parse_deal_edits(text_to_parse, deal)
        if not edits:
            await status_msg.edit_text("⚠️ Could not detect changes from input. Opening deal editor:", parse_mode=HTML)
            await _show_editdeal_menu(msg.reply_text, deal)
            return

        upd = {}
        changes_desc = []
        if "amount" in edits:
            amt_val = edits["amount"]
            curr = edits.get("currency") or deal.get("currency", "INR")
            fee_setting = edits.get("custom_fee") or deal.get("custom_fee") or settings.get("default_fee", "4%")
            fee_val, fee_disp, tot_val, tot_disp = texts.calculate_fee_details(amt_val, fee_setting, curr)
            upd["amount"] = str(amt_val)
            upd["custom_fee"] = fee_disp
            upd["total_amount"] = str(tot_val)
            upd["currency"] = curr
            changes_desc.append(f"• 💰 <b>Amount:</b> {amt_val} {curr} (Fee: {fee_disp})")

        if "deal_type" in edits:
            upd["deal_type"] = edits["deal_type"]
            upd["purpose"] = edits["deal_type"]
            changes_desc.append(f"• 📦 <b>Item:</b> {esc(edits['deal_type'])}")

        if "tat" in edits:
            upd["tat"] = edits["tat"]
            upd["tat_seconds"] = edits["tat_seconds"]
            if deal.get("status") == "active" and deal.get("tat_started_at"):
                upd["tat_deadline"] = utcnow() + timedelta(seconds=edits["tat_seconds"])
                upd["expiry_notified"] = False
            changes_desc.append(f"• ⏱ <b>TAT:</b> {edits['tat']}")

        if "payment_network" in edits:
            upd["payment_network"] = edits["payment_network"]
            if "currency" not in upd:
                upd["currency"] = edits.get("currency", deal.get("currency", "INR"))
            changes_desc.append(f"• 💳 <b>Payment:</b> {edits['payment_network']}")

        if "conditions" in edits:
            upd["conditions"] = edits["conditions"]
            changes_desc.append(f"• 📜 <b>Conditions:</b> {esc(edits['conditions'])}")

        if "buyer_username" in edits:
            upd["buyer_username"] = edits["buyer_username"]
            changes_desc.append(f"• 👤 <b>Buyer:</b> @{esc(edits['buyer_username'])}")

        if "seller_username" in edits:
            upd["seller_username"] = edits["seller_username"]
            changes_desc.append(f"• 👤 <b>Seller:</b> @{esc(edits['seller_username'])}")

        if "custom_fee" in edits and "amount" not in edits:
            amt_val = deal.get("amount", "0")
            curr = deal.get("currency", "INR")
            fee_val, fee_disp, tot_val, tot_disp = texts.calculate_fee_details(amt_val, edits["custom_fee"], curr)
            upd["custom_fee"] = fee_disp
            upd["total_amount"] = str(tot_val)
            changes_desc.append(f"• 🏷 <b>Fee:</b> {fee_disp}")

        await db.update_deal(deal["_id"], upd)
        await db.record_deal_audit(deal["_id"], "quick_edits", msg.from_user.id, upd)

        fresh = await db.get_deal_by_no(deal["deal_no"])
        b_un = fresh.get("buyer_username")
        s_un = fresh.get("seller_username")
        buyer_tag = f"@{esc(b_un)}" if b_un else "Buyer"
        seller_tag = f"@{esc(s_un)}" if s_un else "Seller"

        changes_summary = "\n".join(changes_desc) if changes_desc else "Details updated."
        await status_msg.edit_text(
            f"✅ <b>Deal #{deal['deal_no']} Updated!</b>\n\n{changes_summary}",
            parse_mode=HTML,
        )

        summary = texts.instant_deal_summary(fresh, buyer_tag, seller_tag)
        kb = InlineKeyboardMarkup([
            [
                _btn("Confirm Deal", f"inst:confirm:{deal['deal_no']}", GREEN, icon=emojis.get_custom_id("SUCCESS")),
                _btn("✏️ Edit Deal", f"editdeal:menu:{deal['deal_no']}", BLUE),
            ]
        ])
        await msg.reply_text(summary, reply_markup=kb, parse_mode=HTML)
        return

    # No arguments provided: show interactive editor menu
    await _show_editdeal_menu(msg.reply_text, deal)


async def editdeal_cmd(update, ctx):
    """Middleman /editdeal command (alias for /edits)."""
    return await edits_cmd(update, ctx)


async def _show_editdeal_menu(send_or_edit_fn, deal):
    deal_no = deal.get("deal_no", "")
    amt = str(deal.get("amount", "0"))
    curr = deal.get("currency", "INR")
    pay = deal.get("payment_network", "UPI")
    tat = deal.get("tat", "24h")
    item = deal.get("purpose") or deal.get("deal_type") or "N/A"
    cond = deal.get("conditions", "None")
    fee = deal.get("custom_fee") or settings.get("default_fee", "4%")
    b_un = deal.get("buyer_username") or "Buyer"
    s_un = deal.get("seller_username") or "Seller"

    kb = InlineKeyboardMarkup([
        [
            _btn("📦 Item", f"editdeal:f_item:{deal_no}", BLUE),
            _btn("💰 Amount", f"editdeal:f_amt:{deal_no}", GREEN),
        ],
        [
            _btn("⏱ TAT", f"editdeal:f_tat:{deal_no}", BLUE),
            _btn("💳 Payment", f"editdeal:f_pay:{deal_no}", GREEN),
        ],
        [
            _btn("👤 Buyer", f"editdeal:f_buyer:{deal_no}", BLUE),
            _btn("👤 Seller", f"editdeal:f_seller:{deal_no}", GREEN),
        ],
        [
            _btn("📜 Conditions", f"editdeal:f_cond:{deal_no}", BLUE),
            _btn("🏷 MM Fee", f"editdeal:f_fee:{deal_no}", GREEN),
        ],
        [
            _btn("🔄 Resend Card", f"editdeal:resend:{deal_no}", GREEN),
            _btn("❌ Cancel", f"editdeal:close:{deal_no}", RED),
        ],
    ])

    text = (
        f"📝 <b>Edit Deal MMd {deal_no}</b>\n\n"
        f"• <b>Item:</b> {esc(str(item))}\n"
        f"• <b>Amount:</b> {esc(str(amt))} {esc(str(curr))} (Fee: {esc(str(fee))})\n"
        f"• <b>Payment:</b> {esc(str(pay))} | <b>TAT:</b> {esc(str(tat))}\n"
        f"• <b>Buyer:</b> @{esc(str(b_un))} | <b>Seller:</b> @{esc(str(s_un))}\n\n"
        "Tap any button below to edit:"
    )
    await send_or_edit_fn(text, reply_markup=kb, parse_mode=HTML)


async def editdeal_callback(update, ctx):
    q = update.callback_query
    if not _is_mm(q.from_user.id, q.from_user.username):
        await q.answer("Only the middleman can edit deal details.", show_alert=True)
        return

    parts = q.data.split(":")
    action = parts[1]
    deal_no = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None

    deal = await db.get_deal_by_no(deal_no) if deal_no else await db.get_deal_by_group(q.message.chat_id)
    if not deal:
        await q.answer("Deal not found.", show_alert=True)
        return

    chat_id = q.message.chat_id
    user_id = q.from_user.id

    if action == "cancel_input":
        await q.answer("Edit cancelled.")
        _group_edit_sessions.pop((chat_id, user_id), None)
        try:
            await q.message.delete()
        except Exception:
            await q.edit_message_text("❌ Edit cancelled.")
        return

    if action == "close":
        await q.answer()
        _group_edit_sessions.pop((chat_id, user_id), None)
        try:
            await q.message.delete()
        except Exception:
            await q.edit_message_text("❌ Edit menu closed.")
        return

    if action == "back":
        await q.answer()
        _group_edit_sessions.pop((chat_id, user_id), None)
        fresh = await db.get_deal_by_no(deal["deal_no"])
        await _show_editdeal_menu(q.edit_message_text, fresh)
        return

    if action == "menu":
        await q.answer()
        _group_edit_sessions.pop((chat_id, user_id), None)
        await _show_editdeal_menu(q.message.reply_text, deal)
        return

    if action == "resend":
        await q.answer("Re-sending deal card...")
        fresh = await db.get_deal_by_no(deal["deal_no"])
        b_un = fresh.get("buyer_username")
        s_un = fresh.get("seller_username")
        buyer_tag = f"@{esc(b_un)}" if b_un else "Buyer"
        seller_tag = f"@{esc(s_un)}" if s_un else "Seller"
        summary = texts.instant_deal_summary(fresh, buyer_tag, seller_tag)
        kb = InlineKeyboardMarkup([
            [
                _btn("Confirm Deal", f"inst:confirm:{deal['deal_no']}", GREEN, icon=emojis.get_custom_id("SUCCESS")),
                _btn("✏️ Edit Deal", f"editdeal:menu:{deal['deal_no']}", BLUE),
            ]
        ])
        await q.message.reply_text(summary, reply_markup=kb, parse_mode=HTML)
        return

    if action == "f_amt":
        await q.answer()
        await _show_editamt_prompt(ctx.bot, chat_id, deal, user_id)
        return

    if action == "f_item":
        await q.answer()
        _group_edit_sessions[(chat_id, user_id)] = {"action": "edit_item", "deal_no": deal["deal_no"]}
        kb = InlineKeyboardMarkup([
            [
                _btn("⬅️ Back to Editor", f"editdeal:back:{deal['deal_no']}", BLUE),
                _btn("❌ Cancel", f"editdeal:cancel_input:{deal['deal_no']}", RED),
            ]
        ])
        await q.message.reply_text(
            f"📦 <b>Edit Item for Deal #{deal['deal_no']}</b>\n\n"
            "Send the new item description below:",
            reply_markup=kb,
            parse_mode=HTML,
        )
        return

    if action == "f_cond":
        await q.answer()
        _group_edit_sessions[(chat_id, user_id)] = {"action": "edit_cond", "deal_no": deal["deal_no"]}
        kb = InlineKeyboardMarkup([
            [
                _btn("⬅️ Back to Editor", f"editdeal:back:{deal['deal_no']}", BLUE),
                _btn("❌ Cancel", f"editdeal:cancel_input:{deal['deal_no']}", RED),
            ]
        ])
        await q.message.reply_text(
            f"📜 <b>Edit Conditions for Deal #{deal['deal_no']}</b>\n\n"
            "Send new conditions (or send <code>None</code>):",
            reply_markup=kb,
            parse_mode=HTML,
        )
        return

    if action == "f_buyer":
        await q.answer()
        _group_edit_sessions[(chat_id, user_id)] = {"action": "edit_buyer", "deal_no": deal["deal_no"]}
        kb = InlineKeyboardMarkup([
            [
                _btn("⬅️ Back to Editor", f"editdeal:back:{deal['deal_no']}", BLUE),
                _btn("❌ Cancel", f"editdeal:cancel_input:{deal['deal_no']}", RED),
            ]
        ])
        await q.message.reply_text(
            f"👤 <b>Edit Buyer for Deal #{deal['deal_no']}</b>\n\n"
            "Send buyer @username or user ID:",
            reply_markup=kb,
            parse_mode=HTML,
        )
        return

    if action == "f_seller":
        await q.answer()
        _group_edit_sessions[(chat_id, user_id)] = {"action": "edit_seller", "deal_no": deal["deal_no"]}
        kb = InlineKeyboardMarkup([
            [
                _btn("⬅️ Back to Editor", f"editdeal:back:{deal['deal_no']}", BLUE),
                _btn("❌ Cancel", f"editdeal:cancel_input:{deal['deal_no']}", RED),
            ]
        ])
        await q.message.reply_text(
            f"👤 <b>Edit Seller for Deal #{deal['deal_no']}</b>\n\n"
            "Send seller @username or user ID:",
            reply_markup=kb,
            parse_mode=HTML,
        )
        return

    if action == "f_pay":
        await q.answer()
        kb = InlineKeyboardMarkup([
            [
                _btn("UPI", f"editdeal:set_pay:UPI:{deal['deal_no']}", GREEN),
                _btn("USDT (TRC-20)", f"editdeal:set_pay:TRC-20:{deal['deal_no']}", BLUE),
            ],
            [
                _btn("USDT (BEP-20)", f"editdeal:set_pay:BEP-20:{deal['deal_no']}", BLUE),
                _btn("Bank Transfer", f"editdeal:set_pay:Bank:{deal['deal_no']}", BLUE),
            ],
            [
                _btn("⬅️ Back", f"editdeal:back:{deal['deal_no']}", BLUE),
                _btn("❌ Cancel", f"editdeal:close:{deal['deal_no']}", RED),
            ],
        ])
        await q.edit_message_text("💳 <b>Select Payment Method:</b>", reply_markup=kb, parse_mode=HTML)
        return

    if action == "set_pay":
        new_pay = parts[2]
        deal_no = int(parts[3])
        new_curr = "INR" if new_pay == "UPI" else ("USDT" if "TRC" in new_pay or "BEP" in new_pay else deal.get("currency", "INR"))
        await db.update_deal(deal["_id"], {"payment_network": new_pay, "currency": new_curr})
        await q.answer(f"Payment set to {new_pay}!", show_alert=True)
        fresh = await db.get_deal_by_no(deal_no)
        await _show_editdeal_menu(q.edit_message_text, fresh)
        return

    if action == "f_tat":
        await q.answer()
        kb = InlineKeyboardMarkup([
            [
                _btn("1h", f"editdeal:set_tat:1h:{deal['deal_no']}", GREEN),
                _btn("2h", f"editdeal:set_tat:2h:{deal['deal_no']}", GREEN),
                _btn("6h", f"editdeal:set_tat:6h:{deal['deal_no']}", GREEN),
                _btn("12h", f"editdeal:set_tat:12h:{deal['deal_no']}", GREEN),
            ],
            [
                _btn("24h", f"editdeal:set_tat:24h:{deal['deal_no']}", BLUE),
                _btn("48h", f"editdeal:set_tat:48h:{deal['deal_no']}", BLUE),
                _btn("3d", f"editdeal:set_tat:3d:{deal['deal_no']}", BLUE),
                _btn("7d", f"editdeal:set_tat:7d:{deal['deal_no']}", BLUE),
            ],
            [
                _btn("✏️ Custom TAT", f"editdeal:custom_tat:{deal['deal_no']}", GREEN),
                _btn("⬅️ Back", f"editdeal:back:{deal['deal_no']}", BLUE),
                _btn("❌ Cancel", f"editdeal:close:{deal['deal_no']}", RED),
            ],
        ])
        await q.edit_message_text("⏱ <b>Select TAT Duration:</b>", reply_markup=kb, parse_mode=HTML)
        return

    if action == "set_tat":
        new_tat = parts[2]
        deal_no = int(parts[3])
        secs = parse_tat(new_tat)
        upd = {"tat": new_tat, "tat_seconds": secs}
        if deal.get("status") == "active" and deal.get("tat_started_at"):
            upd["tat_deadline"] = utcnow() + timedelta(seconds=secs)
            upd["expiry_notified"] = False
        await db.update_deal(deal["_id"], upd)
        await q.answer(f"TAT set to {new_tat}!", show_alert=True)
        fresh = await db.get_deal_by_no(deal_no)
        await _show_editdeal_menu(q.edit_message_text, fresh)
        return

    if action == "custom_tat":
        await q.answer()
        _group_edit_sessions[(chat_id, user_id)] = {"action": "edit_tat", "deal_no": deal["deal_no"]}
        kb = InlineKeyboardMarkup([
            [
                _btn("⬅️ Back to Editor", f"editdeal:back:{deal['deal_no']}", BLUE),
                _btn("❌ Cancel", f"editdeal:cancel_input:{deal['deal_no']}", RED),
            ]
        ])
        await q.message.reply_text(
            f"⏱ <b>Custom TAT for Deal #{deal['deal_no']}</b>\n\n"
            "Send duration below (e.g. <code>24h</code>, <code>2d</code>, <code>12h</code>):",
            reply_markup=kb,
            parse_mode=HTML,
        )
        return

    if action == "f_fee":
        await q.answer()
        kb = InlineKeyboardMarkup([
            [
                _btn("4%", f"editdeal:set_fee:4%:{deal['deal_no']}", GREEN),
                _btn("5%", f"editdeal:set_fee:5%:{deal['deal_no']}", GREEN),
                _btn("3%", f"editdeal:set_fee:3%:{deal['deal_no']}", GREEN),
                _btn("2%", f"editdeal:set_fee:2%:{deal['deal_no']}", GREEN),
            ],
            [
                _btn("50 INR", f"editdeal:set_fee:50 INR:{deal['deal_no']}", BLUE),
                _btn("100 INR", f"editdeal:set_fee:100 INR:{deal['deal_no']}", BLUE),
                _btn("✏️ Custom", f"editdeal:custom_fee:{deal['deal_no']}", BLUE),
            ],
            [
                _btn("⬅️ Back", f"editdeal:back:{deal['deal_no']}", BLUE),
                _btn("❌ Cancel", f"editdeal:close:{deal['deal_no']}", RED),
            ],
        ])
        await q.edit_message_text("🏷 <b>Select MM Fee:</b>", reply_markup=kb, parse_mode=HTML)
        return

    if action == "set_fee":
        new_fee = parts[2]
        deal_no = int(parts[3])
        amt = deal.get("amount", "0")
        curr = deal.get("currency", "INR")
        fee_val, fee_disp, tot_val, tot_disp = texts.calculate_fee_details(amt, new_fee, curr)
        await db.update_deal(deal["_id"], {"custom_fee": new_fee, "total_amount": str(tot_val)})
        await q.answer(f"Fee set to {new_fee}!", show_alert=True)
        fresh = await db.get_deal_by_no(deal_no)
        await _show_editdeal_menu(q.edit_message_text, fresh)
        return

    if action == "custom_fee":
        await q.answer()
        _group_edit_sessions[(chat_id, user_id)] = {"action": "edit_fee", "deal_no": deal["deal_no"]}
        kb = InlineKeyboardMarkup([
            [
                _btn("⬅️ Back to Editor", f"editdeal:back:{deal['deal_no']}", BLUE),
                _btn("❌ Cancel", f"editdeal:cancel_input:{deal['deal_no']}", RED),
            ]
        ])
        await q.message.reply_text(
            f"🏷 <b>Custom MM Fee for Deal #{deal['deal_no']}</b>\n\n"
            "Send fee below (e.g. <code>4.5%</code> or <code>75 INR</code>):",
            reply_markup=kb,
            parse_mode=HTML,
        )
        return



# ------------------------------------------------------------------ /editamt Command & Callback
async def editamt_cmd(update, ctx):
    """Middleman /editamt command: edit currency, payment method and amount with automatic fee calculation."""
    msg = update.message
    if not _is_mm(msg.from_user.id, msg.from_user.username):
        await msg.reply_text("⚠️ This command is reserved for the middleman.")
        return

    deal = await db.get_deal_by_group(msg.chat_id)
    if not deal:
        await msg.reply_text("❌ No deal found in this group.")
        return

    if deal.get("status") == "closed":
        await msg.reply_text("❌ This deal is closed.")
        return

    if ctx.args:
        arg_text = " ".join(ctx.args).strip()
        nums = re.findall(r"\d+(?:\.\d+)?", arg_text.replace(",", ""))
        if nums:
            await _apply_new_amount(ctx.bot, msg.chat_id, deal, nums[0], msg.from_user.id)
            return

    await _show_editamt_prompt(ctx.bot, msg.chat_id, deal, msg.from_user.id)


async def _show_editamt_prompt(bot, chat_id: int, deal: dict, user_id: int, edit_msg_id: int = None):
    deal_no = deal["deal_no"]
    curr = deal.get("currency", "INR")
    net = deal.get("payment_network", "UPI")
    fee_setting = deal.get("custom_fee") or settings.get("default_fee", "4%")
    amt = deal.get("amount", "0")

    kb = InlineKeyboardMarkup([
        [
            _btn(f"₹ INR {'✓' if curr == 'INR' else ''}", f"editamt:curr:INR:{deal_no}", BLUE),
            _btn(f"$ USD {'✓' if curr == 'USD' else ''}", f"editamt:curr:USD:{deal_no}", BLUE),
            _btn(f"🪙 Crypto {'✓' if curr in ('USDT', 'CRYPTO') else ''}", f"editamt:curr:USDT:{deal_no}", BLUE),
        ],
        [
            _btn(f"💳 UPI {'✓' if net == 'UPI' else ''}", f"editamt:net:UPI:{deal_no}", GREEN),
            _btn(f"🪙 Crypto (TRC-20) {'✓' if 'TRC' in net else ''}", f"editamt:net:TRC-20:{deal_no}", GREEN),
            _btn(f"🪙 Crypto (BEP-20) {'✓' if 'BEP' in net else ''}", f"editamt:net:BEP-20:{deal_no}", GREEN),
        ],
        [
            _btn(f"🏷 Fee: {fee_setting}", f"editamt:feeset:{deal_no}", GREEN),
            _btn("❌ Cancel", f"editamt:cancel:{deal_no}", RED),
        ],
    ])

    text = (
        f"💰 <b>Edit Deal Amount (MMd {deal_no})</b>\n\n"
        f"• Current Amount: <b>{esc(str(amt))} {esc(str(curr))}</b>\n"
        f"• Payment Method: <b>{esc(str(net))}</b>\n"
        f"• MM Fee Setting: <b>{esc(str(fee_setting))}</b>\n\n"
        "• Tap buttons above to change currency or payment method.\n"
        "• <b>Reply to this message with the deal amount</b> (e.g. <code>5005</code> or <code>5k</code>).\n"
        f"The bot will automatically calculate and add the MM fee (<b>{esc(str(fee_setting))}</b>) to it."
    )

    if edit_msg_id:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=edit_msg_id, text=text, reply_markup=kb, parse_mode=HTML)
            sent_msg_id = edit_msg_id
        except Exception:
            sent = await bot.send_message(chat_id, text, reply_markup=kb, parse_mode=HTML)
            sent_msg_id = sent.message_id
    else:
        sent = await bot.send_message(chat_id, text, reply_markup=kb, parse_mode=HTML)
        sent_msg_id = sent.message_id

    _group_edit_sessions[(chat_id, user_id)] = {
        "action": "edit_amt",
        "deal_no": deal_no,
        "prompt_msg_id": sent_msg_id,
    }


async def _apply_new_amount(bot, chat_id: int, deal: dict, raw_amount: str, actor_id: int):
    cleaned = re.sub(r"\b(\d+(?:\.\d+)?)\s*[kK]\b", lambda m: str(int(float(m.group(1)) * 1000)), raw_amount)
    nums = re.findall(r"\d+(?:\.\d+)?", cleaned.replace(",", ""))
    if not nums:
        await bot.send_message(chat_id, "❌ Please enter a valid number for amount (e.g. 5000).")
        return

    amt_val = nums[0]
    curr = deal.get("currency", "INR")
    fee_setting = deal.get("custom_fee") or settings.get("default_fee", "4%")
    fee_val, fee_disp, tot_val, tot_disp = texts.calculate_fee_details(amt_val, fee_setting, curr)

    upd = {
        "amount": amt_val,
        "custom_fee": fee_disp,
        "total_amount": str(tot_val),
    }
    await db.update_deal(deal["_id"], upd)
    await db.record_deal_audit(deal["_id"], "edit_amount", actor_id, upd)

    deal_no = deal["deal_no"]
    fresh = await db.get_deal_by_no(deal_no)

    sym = get_currency_symbol(curr)
    fee_show = f"{fee_disp} ({sym}{fee_val:g})" if sym in ("₹", "$", "€", "£") and fee_val > 0 and "%" in fee_disp else fee_disp
    disp_amt = f"{sym}{amt_val}" if sym in ("₹", "$", "€", "£") else f"{amt_val} {curr}"

    msg_text = (
        "✅ <b>Amount & MM Fee Updated!</b>\n\n"
        f"📦 Deal: <b>MMd {deal_no}</b>\n"
        f"💰 Deal Amount: <b>{disp_amt}</b>\n"
        f"🏷 MM Fee: <b>{fee_show}</b>\n"
        f"💳 <b>Total Payable: {tot_disp}</b>\n"
        f"💳 Payment: <b>{fresh.get('payment_network', 'UPI')}</b>\n\n"
        f"<i>Bot added {fee_show} MM fee to deal amount.</i>"
    )
    await safe_send(bot, chat_id, msg_text)

    # Re-send updated deal summary card
    b_un = fresh.get("buyer_username")
    s_un = fresh.get("seller_username")
    buyer_tag = f"@{esc(b_un)}" if b_un else "Buyer"
    seller_tag = f"@{esc(s_un)}" if s_un else "Seller"

    summary = texts.instant_deal_summary(fresh, buyer_tag, seller_tag)
    kb = InlineKeyboardMarkup([
        [
            _btn("Confirm Deal", f"inst:confirm:{deal_no}", GREEN, icon=emojis.get_custom_id("SUCCESS")),
            _btn("✏️ Edit Deal", f"editdeal:menu:{deal_no}", BLUE),
        ]
    ])
    await safe_send(bot, chat_id, summary, reply_markup=kb)


async def editamt_callback(update, ctx):
    q = update.callback_query
    if not _is_mm(q.from_user.id, q.from_user.username):
        await q.answer("Only the middleman can edit deal amount.", show_alert=True)
        return

    parts = q.data.split(":")
    action = parts[1]
    chat_id = q.message.chat_id
    user_id = q.from_user.id

    if action == "start":
        deal_no = int(parts[2])
        deal = await db.get_deal_by_no(deal_no)
        if not deal:
            await q.answer("Deal not found.", show_alert=True)
            return
        await q.answer()
        await _show_editamt_prompt(ctx.bot, chat_id, deal, user_id)
        return

    if action == "cancel":
        deal_no = int(parts[2])
        await q.answer("Amount editing cancelled.")
        _group_edit_sessions.pop((chat_id, user_id), None)
        try:
            await q.message.delete()
        except Exception:
            pass
        return

    if action == "curr":
        new_curr = parts[2]
        deal_no = int(parts[3])
        deal = await db.get_deal_by_no(deal_no)
        if not deal:
            await q.answer("Deal not found.", show_alert=True)
            return
        new_net = "UPI" if new_curr == "INR" else ("TRC-20" if new_curr in ("USDT", "CRYPTO") else deal.get("payment_network", "UPI"))
        await db.update_deal(deal["_id"], {"currency": new_curr, "payment_network": new_net})
        await q.answer(f"Currency switched to {new_curr}!")
        fresh = await db.get_deal_by_no(deal_no)
        await _show_editamt_prompt(ctx.bot, chat_id, fresh, user_id, edit_msg_id=q.message.message_id)
        return

    if action == "net":
        new_net = parts[2]
        deal_no = int(parts[3])
        deal = await db.get_deal_by_no(deal_no)
        if not deal:
            await q.answer("Deal not found.", show_alert=True)
            return
        new_curr = "INR" if new_net == "UPI" else "USDT"
        await db.update_deal(deal["_id"], {"payment_network": new_net, "currency": new_curr})
        await q.answer(f"Payment method switched to {new_net}!")
        fresh = await db.get_deal_by_no(deal_no)
        await _show_editamt_prompt(ctx.bot, chat_id, fresh, user_id, edit_msg_id=q.message.message_id)
        return

    if action == "feeset":
        deal_no = int(parts[2])
        deal = await db.get_deal_by_no(deal_no)
        kb = InlineKeyboardMarkup([
            [
                _btn("4%", f"editdeal:set_fee:4%:{deal_no}", GREEN),
                _btn("5%", f"editdeal:set_fee:5%:{deal_no}", GREEN),
                _btn("3%", f"editdeal:set_fee:3%:{deal_no}", GREEN),
                _btn("50 INR", f"editdeal:set_fee:50 INR:{deal_no}", BLUE),
            ],
            [
                _btn("⬅️ Back to Amount Editor", f"editamt:curr:{deal.get('currency', 'INR')}:{deal_no}", RED),
            ],
        ])
        await q.edit_message_text(f"🏷 <b>Select MM Fee for Deal #{deal_no}:</b>", reply_markup=kb, parse_mode=HTML)
        return


# ------------------------------------------------------------------ TAT Edit Callback
async def tat_edit_callback(update, ctx):
    q = update.callback_query
    if not _is_mm(q.from_user.id, q.from_user.username):
        await q.answer("Only the middleman can edit TAT.", show_alert=True)
        return

    parts = q.data.split(":")
    action = parts[1]
    deal_no = int(parts[3]) if action == "add" else int(parts[2])

    deal = await db.get_deal_by_no(deal_no)
    if not deal:
        await q.answer("Deal not found.", show_alert=True)
        return

    if action == "menu":
        await q.answer()
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("+1h", callback_data=f"tatedit:add:1h:{deal_no}"),
                InlineKeyboardButton("+6h", callback_data=f"tatedit:add:6h:{deal_no}"),
                InlineKeyboardButton("+12h", callback_data=f"tatedit:add:12h:{deal_no}"),
                InlineKeyboardButton("+24h", callback_data=f"tatedit:add:24h:{deal_no}"),
            ],
            [
                InlineKeyboardButton("✏️ Custom TAT Duration", callback_data=f"tatedit:custom:{deal_no}"),
                InlineKeyboardButton("⬅️ Done", callback_data=f"tatedit:done:{deal_no}"),
            ],
        ])
        await q.message.reply_text(
            f"⏱ <b>Adjust Turnaround Time for Deal #{deal_no}</b>\n\n"
            f"Current TAT: <b>{fmt_duration(deal.get('tat_seconds', 0))}</b>\n"
            "Choose a quick extension below or set a custom duration:",
            reply_markup=kb,
            parse_mode=HTML,
        )
        return

    elif action == "add":
        dur_str = parts[2]
        secs = parse_tat(dur_str)
        await q.answer(f"Extending TAT by {dur_str}...")
        await service.extend_deal_tat(ctx.bot, deal, secs, dur_str, q.from_user.id)
        return

    elif action == "custom":
        await q.answer()
        _group_edit_sessions[(q.message.chat_id, q.from_user.id)] = {"action": "edit_tat", "deal_no": deal_no}
        await q.message.reply_text(
            f"⏱ <b>Set Custom TAT for Deal #{deal_no}</b>\n\n"
            "Reply to this message with the new TAT duration (e.g. <code>24h</code>, <code>2d</code>, <code>12h</code>):",
            parse_mode=HTML,
        )
        return

    elif action == "done":
        await q.answer("TAT adjustment complete.")
        try:
            await q.message.delete()
        except Exception:
            pass
        return


# ------------------------------------------------------------------ Chat Member Admission Restrictions
async def on_chat_member_update(update, ctx):
    """Enforces max 2 participants (1 buyer, 1 seller). Revokes link and welcomes new joins."""
    cm = update.chat_member
    if not cm:
        return

    chat = cm.chat
    deal = await db.get_deal_by_group(chat.id)
    if not deal:
        return

    new_user = cm.new_chat_member.user if cm.new_chat_member else None
    if not new_user or new_user.is_bot:
        return

    # Only process actual join events (joining group from left/kicked/none)
    new_status = getattr(cm.new_chat_member, "status", None)
    old_status = getattr(cm.old_chat_member, "status", None) if cm.old_chat_member else None
    joined_statuses = ("member", "administrator", "creator")
    if new_status not in joined_statuses:
        return  # user left, was kicked, or restricted - do not send welcome or admit
    if old_status in joined_statuses:
        return  # user was already in the group (e.g. role/title change)

    # Allow Middleman
    if _is_mm(new_user.id, new_user.username):
        return

    # If deal is closed, do NOT kick - mute only
    if deal.get("status") == "closed":
        await service.mute_deal_participants(ctx.bot, chat.id, new_user.id)
        return

    buyer_id = deal.get("buyer_id")
    seller_id = deal.get("seller_id")

    # If both participants are already set
    if buyer_id and seller_id:
        if new_user.id != buyer_id and new_user.id != seller_id:
            # Both roles filled: revoke invite link and mute extra joiner (approval required)
            await service.revoke_deal_invite(ctx.bot, deal)
            await service.mute_deal_participants(ctx.bot, chat.id, new_user.id)
            await safe_send(
                ctx.bot,
                chat.id,
                f"⚠️ {user_mention(new_user.id, new_user.username, new_user.first_name)} joined, but Buyer & Seller roles are already confirmed.\n"
                f"Participant has been muted. Approval from Middleman @{config.MM_USERNAME} required to participate.",
            )
            return
        else:
            # Both participants are in, revoke invitation link
            await service.revoke_deal_invite(ctx.bot, deal)
            return

    # Welcoming message prompting for role
    mention = user_mention(new_user.id, new_user.username, new_user.first_name)
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
    await safe_send(
        ctx.bot,
        chat.id,
        f"🤝 <b>Deal #{config.DEAL_ID_PREFIX}{dno}</b>\n\n"
        f"{mention}, select your role below:",
        reply_markup=kb,
    )


# ------------------------------------------------------------------ Message Routers
async def on_group_message(update, ctx):
    msg = update.message
    if not msg or not msg.text:
        return

    chat_id = msg.chat_id
    deal = await db.get_deal_by_group(chat_id)
    if not deal:
        return

    text = msg.text.strip()
    status = deal.get("status")

    if status == "closed":
        return

    # Check for active group edit session (Middleman replying to an edit prompt)
    edit_sess = _group_edit_sessions.get((chat_id, msg.from_user.id))
    if edit_sess and _is_mm(msg.from_user.id, msg.from_user.username):
        if text.lower() in ("/cancel", "cancel"):
            _group_edit_sessions.pop((chat_id, msg.from_user.id), None)
            await msg.reply_text("❌ Edit operation cancelled.")
            return

        action = edit_sess.get("action")
        deal_no = edit_sess.get("deal_no", deal["deal_no"])
        _group_edit_sessions.pop((chat_id, msg.from_user.id), None)

        if action == "edit_amt":
            await _apply_new_amount(ctx.bot, chat_id, deal, text, msg.from_user.id)
            return

        elif action == "edit_item":
            await db.update_deal(deal["_id"], {"purpose": text, "deal_type": text})
            await db.record_deal_audit(deal["_id"], "edit_item", msg.from_user.id, {"item": text})
            await msg.reply_text(f"✅ <b>Item updated to:</b> {esc(text)}", parse_mode=HTML)
            fresh = await db.get_deal_by_no(deal_no)
            summary = texts.instant_deal_summary(fresh, f"@{esc(fresh.get('buyer_username') or 'Buyer')}", f"@{esc(fresh.get('seller_username') or 'Seller')}")
            kb = InlineKeyboardMarkup([
                [
                    _btn("Confirm Deal", f"inst:confirm:{deal_no}", GREEN, icon=emojis.get_custom_id("SUCCESS")),
                    _btn("✏️ Edit Deal", f"editdeal:menu:{deal_no}", BLUE),
                ]
            ])
            await safe_send(ctx.bot, chat_id, summary, reply_markup=kb)
            return

        elif action == "edit_tat":
            try:
                secs = parse_tat(text)
                upd = {"tat": text, "tat_seconds": secs}
                if deal.get("status") == "active" and deal.get("tat_started_at"):
                    upd["tat_deadline"] = utcnow() + timedelta(seconds=secs)
                    upd["expiry_notified"] = False
                await db.update_deal(deal["_id"], upd)
                await db.record_deal_audit(deal["_id"], "edit_tat", msg.from_user.id, {"tat": text, "seconds": secs})
                await msg.reply_text(f"✅ <b>TAT updated to:</b> {esc(text)} ({fmt_duration(secs)})", parse_mode=HTML)
            except ValueError as e:
                await msg.reply_text(str(e), parse_mode=HTML)
            return

        elif action == "edit_cond":
            await db.update_deal(deal["_id"], {"conditions": text})
            await db.record_deal_audit(deal["_id"], "edit_conditions", msg.from_user.id, {"conditions": text})
            await msg.reply_text(f"✅ <b>Conditions updated to:</b> {esc(text)}", parse_mode=HTML)
            fresh = await db.get_deal_by_no(deal_no)
            summary = texts.instant_deal_summary(fresh, f"@{esc(fresh.get('buyer_username') or 'Buyer')}", f"@{esc(fresh.get('seller_username') or 'Seller')}")
            kb = InlineKeyboardMarkup([
                [
                    _btn("Confirm Deal", f"inst:confirm:{deal_no}", GREEN, icon=emojis.get_custom_id("SUCCESS")),
                    _btn("✏️ Edit Deal", f"editdeal:menu:{deal_no}", BLUE),
                ]
            ])
            await safe_send(ctx.bot, chat_id, summary, reply_markup=kb)
            return

        elif action == "edit_fee":
            amt = deal.get("amount", "0")
            curr = deal.get("currency", "INR")
            fee_val, fee_disp, tot_val, tot_disp = texts.calculate_fee_details(amt, text, curr)
            await db.update_deal(deal["_id"], {"custom_fee": text, "total_amount": str(tot_val)})
            await db.record_deal_audit(deal["_id"], "edit_fee", msg.from_user.id, {"fee": text, "total": str(tot_val)})
            await msg.reply_text(f"✅ <b>MM Fee updated to:</b> {esc(text)} (Total payable: {tot_disp})", parse_mode=HTML)
            fresh = await db.get_deal_by_no(deal_no)
            summary = texts.instant_deal_summary(fresh, f"@{esc(fresh.get('buyer_username') or 'Buyer')}", f"@{esc(fresh.get('seller_username') or 'Seller')}")
            kb = InlineKeyboardMarkup([
                [
                    _btn("Confirm Deal", f"inst:confirm:{deal_no}", GREEN, icon=emojis.get_custom_id("SUCCESS")),
                    _btn("✏️ Edit Deal", f"editdeal:menu:{deal_no}", BLUE),
                ]
            ])
            await safe_send(ctx.bot, chat_id, summary, reply_markup=kb)
            return

        elif action == "edit_buyer":
            clean_b = text.lstrip("@").strip()
            await db.update_deal(deal["_id"], {"buyer_username": clean_b})
            await msg.reply_text(f"✅ <b>Buyer updated to:</b> @{esc(clean_b)}", parse_mode=HTML)
            fresh = await db.get_deal_by_no(deal_no)
            summary = texts.instant_deal_summary(fresh, f"@{esc(fresh.get('buyer_username') or 'Buyer')}", f"@{esc(fresh.get('seller_username') or 'Seller')}")
            kb = InlineKeyboardMarkup([
                [
                    _btn("Confirm Deal", f"inst:confirm:{deal_no}", GREEN, icon=emojis.get_custom_id("SUCCESS")),
                    _btn("✏️ Edit Deal", f"editdeal:menu:{deal_no}", BLUE),
                ]
            ])
            await safe_send(ctx.bot, chat_id, summary, reply_markup=kb)
            return

        elif action == "edit_seller":
            clean_s = text.lstrip("@").strip()
            await db.update_deal(deal["_id"], {"seller_username": clean_s})
            await msg.reply_text(f"✅ <b>Seller updated to:</b> @{esc(clean_s)}", parse_mode=HTML)
            fresh = await db.get_deal_by_no(deal_no)
            summary = texts.instant_deal_summary(fresh, f"@{esc(fresh.get('buyer_username') or 'Buyer')}", f"@{esc(fresh.get('seller_username') or 'Seller')}")
            kb = InlineKeyboardMarkup([
                [
                    _btn("Confirm Deal", f"inst:confirm:{deal_no}", GREEN, icon=emojis.get_custom_id("SUCCESS")),
                    _btn("✏️ Edit Deal", f"editdeal:menu:{deal_no}", BLUE),
                ]
            ])
            await safe_send(ctx.bot, chat_id, summary, reply_markup=kb)
            return

    # Payment sent reply: "Yes" or "Confirm"
    if status in ("payment_pending", "deal_confirmed") and text.lower() in ("yes", "confirm", "sent", "paid"):
        mm_kb = _kb(_btn("💰 Confirm Payment Received", f"inst:mm_paid:{deal['deal_no']}", GREEN))
        await msg.reply_text(texts.payment_pending_acknowledgement(), reply_markup=mm_kb, parse_mode=HTML)
        return

    # Deal details submission in GC
    if status in ("instant_details_pending", "instant_role_selection"):
        parsed = await ai_parser.parse_deal_details(text)
        is_valid, missing = ai_parser.validate_parsed_deal(parsed)
        if parsed and is_valid:
            deal_no = deal["deal_no"]
            await db.update_deal(deal["_id"], {
                "deal_type": parsed["deal_type"],
                "purpose": parsed["deal_type"],
                "tat": parsed["tat"],
                "tat_seconds": parsed["tat_seconds"],
                "conditions": parsed["conditions"],
                "amount": parsed["amount"],
                "currency": parsed["currency"],
                "payment_network": parsed["payment_network"],
                "status": "instant_confirmation_pending",
            })

            fresh = await db.get_deal_by_no(deal_no)
            buyer_tag = f"@{esc(fresh.get('buyer_username') or 'Buyer')}"
            seller_tag = f"@{esc(fresh.get('seller_username') or 'Seller')}"

            summary = texts.instant_deal_summary(fresh, buyer_tag, seller_tag)
            kb = InlineKeyboardMarkup([
                [
                    _btn("Confirm Deal", f"inst:confirm:{deal_no}", GREEN, icon=emojis.get_custom_id("SUCCESS")),
                    _btn("✏️ Edit Deal", f"editdeal:menu:{deal_no}", BLUE),
                ]
            ])
            await msg.reply_text(summary, reply_markup=kb, parse_mode=HTML)
            return
        elif any(k in text.lower() for k in ("item", "amount", "tat", "payment", "price", "upi", "inr", "usdt", "dollar", "₹", "$")):
            miss_str = ", ".join(missing) if missing else "Item or Amount"
            await msg.reply_text(
                f"⚠️ <b>Incomplete Deal Form</b>\nCould not detect: <b>{miss_str}</b>.\n\n"
                "Please reply with all details in format:\n"
                "• Item: (e.g. Instagram account)\n"
                "• Amount: (e.g. 5005)\n"
                "• Payment: UPI | TAT: 24h\n\n"
                "Or Middleman can use <code>/editdeal</code> or <code>/ai</code>.",
                parse_mode=HTML,
            )
            return

    # Strict vouch detection in deal group (only triggers if message resembles configured vouch format)
    if status in ("completed", "active", "closed", "deal_confirmed", "payment_pending", "released"):
        if is_matching_vouch(text, deal):
            vouch_target = parse_channel_target(settings.get("vouch_channel_url", config.VOUCH_CHANNEL_URL))
            vouch_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📢 Forward to Vouches Channel", callback_data=f"vouch:fwd_msg:{deal['deal_no']}:{msg.message_id}"),
                    InlineKeyboardButton("❌ Don't Forward", callback_data=f"vouch:no:{deal['deal_no']}"),
                ]
            ])
            await msg.reply_text(
                f"⭐ <b>Vouch Detected!</b>\n\n"
                f"Forward this vouch message to <code>{esc(str(vouch_target))}</code>?\n"
                f"<i>(Only middleman @{config.MM_USERNAME} can confirm)</i>",
                reply_markup=vouch_kb,
                parse_mode=HTML,
            )
            return


async def on_private_message(update, ctx):
    msg = update.message
    if msg is None:
        return
    user = update.effective_user

    if msg.users_shared:
        return await _users_shared(msg)
    if msg.forward_origin:
        return await _forwarded(msg)

    # 1. Admin session (setting a field, crypto address, template, GC photo, or start number)
    admin_sess = _admin_sessions.get(user.id)
    if admin_sess and _is_mm(user.id, user.username):
        action = admin_sess["action"]

        if msg.text and msg.text.strip().lower() in ("/cancel", "cancel"):
            del _admin_sessions[user.id]
            await msg.reply_text("❌ Action cancelled.")
            return

        # Handle Broadcast Content Input (text, photos, videos, stickers, voice, etc.)
        if action == "broadcast_wait_content":
            target_type = admin_sess.get("target_type", "both")
            admin_sess["message_id"] = msg.message_id
            admin_sess["from_chat_id"] = msg.chat_id
            admin_sess["action"] = "broadcast_confirm"

            user_cnt, gc_cnt = await db.count_broadcast_targets()
            if target_type == "users":
                dest_label = f"All Bot Users ({user_cnt})"
                total_cnt = user_cnt
            elif target_type == "groups":
                dest_label = f"All Deal Groups ({gc_cnt})"
                total_cnt = gc_cnt
            else:
                dest_label = f"Both Users & Deal GCs ({user_cnt + gc_cnt})"
                total_cnt = user_cnt + gc_cnt

            await msg.reply_text("👁️ <b>Broadcast Preview:</b>", parse_mode=HTML)
            try:
                await ctx.bot.copy_message(
                    chat_id=msg.chat_id,
                    from_chat_id=msg.chat_id,
                    message_id=msg.message_id,
                )
            except Exception as e:
                log.warning("Could not copy broadcast preview message: %s", e)

            confirm_text = (
                "📢 <b>Confirm Broadcast</b>\n\n"
                f"• <b>Destination:</b> {dest_label}\n"
                f"• <b>Total Recipients:</b> <code>{total_cnt}</code>\n\n"
                "Do you want to send this broadcast to all recipients?"
            )
            kb = InlineKeyboardMarkup([
                [
                    _btn("🚀 Confirm Broadcast", "bd:confirm", GREEN),
                    _btn("❌ Cancel", "bd:cancel", RED),
                ]
            ])
            await msg.reply_text(confirm_text, reply_markup=kb, parse_mode=HTML)
            return

        # Handle GC Profile Photo Upload (photo message)
        if action == "set_gc_photo":
            if msg.photo:
                photo_file_id = msg.photo[-1].file_id
                await settings.set_setting("gc_photo_file_id", photo_file_id)
                del _admin_sessions[user.id]
                await msg.reply_text(
                    "✅ <b>GC Profile Photo Configured!</b>\n\n"
                    "This photo will now be automatically set on every newly created deal group.",
                    parse_mode=HTML,
                )
                return
            else:
                await msg.reply_text("⚠️ Please send an image photo (or /cancel to abort).")
                return

        # Handle GC starting number input (text message)
        if action == "set_gc_start_num":
            if not msg.text:
                await msg.reply_text("Please enter a positive integer number (e.g. 121), or /cancel to abort.")
                return
            val = msg.text.strip()
            if not val.isdigit() or int(val) <= 0:
                await msg.reply_text("❌ Invalid number. Please enter a positive integer (e.g. <code>121</code>):", parse_mode=HTML)
                return
            start_num = int(val)
            preset_idx = admin_sess["preset_idx"]
            curr_counter = await db.get_current_deal_no()
            baseline = curr_counter + 1
            await settings.set_setting("gc_start_num", start_num)
            await settings.set_setting("gc_baseline_deal_no", baseline)
            await settings.set_setting("gc_name_template", settings.GC_NAME_PRESETS[preset_idx])
            del _admin_sessions[user.id]
            await msg.reply_text(
                f"✅ <b>Sequential GC Naming Configured!</b>\n\n"
                f"• Preset: <code>{esc(settings.GC_NAME_PRESETS[preset_idx])}</code>\n"
                f"• Starting Deal Number: <b>#{start_num}</b> (Baseline counter: {baseline})\n\n"
                f"The next deal group will be named: <code>Deal #{start_num} | @{config.MM_USERNAME}</code>.",
                parse_mode=HTML,
            )
            return

        # Handle custom GC name template input (text message)
        if action == "set_gc_custom_name":
            if not msg.text:
                await msg.reply_text("⚠️ Please send a text template (or /cancel to abort).")
                return
            val = msg.text.strip()
            if len(val) > 120:
                await msg.reply_text(
                    f"❌ Template too long ({len(val)} chars). Max 120 characters. Please send a shorter template:",
                    parse_mode=HTML,
                )
                return
            if not val:
                await msg.reply_text("❌ Template cannot be empty. Please try again or /cancel.")
                return
            # Render a quick preview to show the middleman before saving
            preview = settings.render_gc_title(
                label="#42", buyer="buyer", seller="seller", amount="500", deal_no=42
            )
            # Save the custom template (using the raw user input as template)
            # First validate it renders without crashing
            try:
                safe_format(val, label="#42", mm=config.MM_USERNAME, buyer="buyer",
                            seller="seller", amount="500", startno=42)
            except Exception as e:
                await msg.reply_text(f"❌ Invalid template syntax: {e}\nPlease fix and try again or /cancel.")
                return
            await settings.set_setting("gc_name_template", val)
            del _admin_sessions[user.id]
            rendered_preview = safe_format(
                val, label="#42", mm=config.MM_USERNAME,
                buyer="buyer", seller="seller", amount="500", startno=42,
            )
            await msg.reply_text(
                f"✅ <b>Custom GC Name Template Saved!</b>\n\n"
                f"• Template: <code>{esc(val)}</code>\n"
                f"• Preview: <code>{esc(rendered_preview)}</code>",
                parse_mode=HTML,
            )
            return

        if msg.text:
            val = msg.text.strip()

            if action == "set_template":
                tmpl_key = admin_sess["tmpl_key"]
                # Validate template safely
                try:
                    safe_format(val, deal_id="1", buyer="@b", seller="@s", currency="$", amount="100", tat="24h", deadline="now", mm=config.MM_USERNAME)
                except Exception as e:
                    await msg.reply_text(f"❌ Invalid template format: {e}. Please try again or send /cancel.")
                    return

                await settings.set_template(tmpl_key, val)
                del _admin_sessions[user.id]
                label = settings.TEMPLATE_LABELS.get(tmpl_key, tmpl_key)
                await msg.reply_text(f"✅ Template <b>{label}</b> updated successfully!", parse_mode=HTML)
                return

            elif action == "set_ad":
                ad_content = msg.text_html.strip() if hasattr(msg, "text_html") and msg.text_html else val
                if len(val) > 250:
                    await msg.reply_text("❌ Ad text is too long (maximum 250 characters). Please send a shorter text, or /cancel:")
                    return
                await settings.set_ad(ad_content, enabled=True)
                del _admin_sessions[user.id]
                await msg.reply_text(
                    f"✅ <b>Advertisement Configured & Activated!</b>\n\n"
                    f"Footer will appear on outgoing messages as:\n— \"{ad_content}\"",
                    parse_mode=HTML,
                )
                return

            elif action == "set_crypto":
                coin_key = admin_sess["coin_key"]
                await settings.set_crypto_wallet(coin_key, val)
                del _admin_sessions[user.id]
                info = settings.CRYPTO_NETWORKS.get(coin_key, {})
                await msg.reply_text(f"✅ Wallet address updated for <b>{info.get('name')}</b>!", parse_mode=HTML)
                return

            elif action == "set_field":
                field = admin_sess["field"]
                await settings.set_setting(field, val)
                del _admin_sessions[user.id]
                await msg.reply_text(f"✅ Setting <b>{field}</b> updated!", parse_mode=HTML)
                return

            elif action == "livechat_reply":
                target_uid = admin_sess["target_uid"]
                del _admin_sessions[user.id]
                ok = await _relay_reply_to_user(ctx.bot, target_uid, val)
                if ok:
                    await msg.reply_text(f"✅ Reply delivered to user <code>{target_uid}</code>.", parse_mode=HTML)
                else:
                    await msg.reply_text(f"❌ Failed to deliver reply to user <code>{target_uid}</code>.", parse_mode=HTML)
                return

    # 2. Middleman replying directly to a livechat message
    if _is_mm(user.id, user.username) and msg.reply_to_message:
        relay = await db.get_livechat_relay(msg.reply_to_message.message_id)
        if relay and msg.text:
            target_uid = relay["user_id"]
            ok = await _relay_reply_to_user(ctx.bot, target_uid, msg.text)
            if ok:
                await msg.reply_text(f"✅ Reply delivered to user <code>{target_uid}</code>.", parse_mode=HTML)
            else:
                await msg.reply_text(f"❌ Failed to deliver reply to user <code>{target_uid}</code>.", parse_mode=HTML)
            return

    # 3. Active livechat relay from client to Middleman
    lc = await db.get_active_livechat(user.id)
    if lc:
        mm_id = config.MM_USER_ID or userbot.mm_user_id
        if mm_id:
            text_content = msg.text or msg.caption or "[Media / Attachment]"
            alert = texts.livechat_user_msg_alert(user.first_name or "", user.username or "", user.id, text_content)
            reply_kb = _kb(_btn("↩️ Reply to User", f"lc:reply_to:{user.id}", BLUE))
            try:
                if msg.photo:
                    sent = await ctx.bot.send_photo(
                        mm_id,
                        photo=msg.photo[-1].file_id,
                        caption=alert,
                        parse_mode=HTML,
                        reply_markup=reply_kb,
                    )
                else:
                    sent = await ctx.bot.send_message(mm_id, alert, parse_mode=HTML, reply_markup=reply_kb)

                await db.map_livechat_relay(sent.message_id, user.id, msg.message_id)
                await msg.reply_text(f"✅ Message sent to @{config.MM_USERNAME}. Please wait for a reply.", parse_mode=HTML)
                return
            except TelegramError as e:
                log.warning("Could not relay livechat message to MM: %s", e)

    # 4. Standard /deal session setup
    s = await db.get_session(user.id)
    if s:
        if msg.text:
            await _session_text(update, ctx, s)
        else:
            await msg.reply_text("Please send a text message.")
        return


async def on_error(update, ctx):
    log.error("Unhandled error", exc_info=ctx.error)


# ==================================================================
# Middleman Broadcast Engine (/bd)
# ==================================================================

active_broadcast_task: Optional[asyncio.Task] = None


async def bd_cmd(update, ctx):
    """Middleman /bd command: Initiates broadcast engine."""
    user = update.effective_user
    msg = update.effective_message
    if not _is_mm(user.id, user.username):
        await msg.reply_text("⚠️ This command is reserved for the middleman.")
        return

    user_cnt, gc_cnt = await db.count_broadcast_targets()
    total_cnt = user_cnt + gc_cnt

    text = (
        "📢 <b>Broadcast Engine</b>\n\n"
        "Select broadcast destination:\n\n"
        f"• 👤 <b>All Bot Users:</b> <code>{user_cnt}</code> users\n"
        f"• 👥 <b>All Deal Groups:</b> <code>{gc_cnt}</code> deal GCs\n"
        f"• 🌐 <b>Both Mode:</b> <code>{total_cnt}</code> total recipients\n"
    )
    kb = InlineKeyboardMarkup([
        [
            _btn(f"👤 All Bot Users ({user_cnt})", "bd:dest:users", BLUE),
            _btn(f"👥 All Deal GCs ({gc_cnt})", "bd:dest:groups", GREEN),
        ],
        [
            _btn(f"🌐 Both Mode ({total_cnt})", "bd:dest:both", GREEN),
        ],
        [
            _btn("❌ Cancel", "bd:cancel", RED),
        ],
    ])
    await msg.reply_text(text, reply_markup=kb, parse_mode=HTML)


async def bd_callback(update, ctx):
    """Handles broadcast inline button callbacks."""
    q = update.callback_query
    user = q.from_user
    if not _is_mm(user.id, user.username):
        await q.answer("Unauthorized", show_alert=True)
        return

    parts = q.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "cancel":
        await q.answer()
        _admin_sessions.pop(user.id, None)
        try:
            await q.edit_message_text("❌ Broadcast cancelled.")
        except Exception:
            pass
        return

    if action == "dest":
        await q.answer()
        target_type = parts[2]
        _admin_sessions[user.id] = {
            "action": "broadcast_wait_content",
            "target_type": target_type,
        }
        dest_label = (
            "All Bot Users" if target_type == "users" else
            ("All Deal Groups" if target_type == "groups" else "Both Users & Deal GCs")
        )
        prompt_text = (
            f"📢 <b>Broadcast to {dest_label}</b>\n\n"
            "Now send or forward the message you want to broadcast.\n\n"
            "Supports <b>text formatting, custom emojis, photos, videos, stickers, voice, audio, animations, and documents</b>.\n\n"
            "Send <code>/cancel</code> or click Cancel below to abort."
        )
        kb = InlineKeyboardMarkup([
            [_btn("❌ Cancel", "bd:cancel", RED)]
        ])
        await q.edit_message_text(prompt_text, reply_markup=kb, parse_mode=HTML)
        return

    if action == "confirm":
        await q.answer()
        sess = _admin_sessions.pop(user.id, None)
        if not sess or "message_id" not in sess:
            await q.edit_message_text("❌ Broadcast session expired. Please run /bd again.")
            return

        global active_broadcast_task
        if active_broadcast_task and not active_broadcast_task.done():
            await q.edit_message_text("⚠️ Another broadcast is currently running. Please wait for it to finish.")
            return

        target_type = sess.get("target_type", "both")
        from_chat_id = sess["from_chat_id"]
        message_id = sess["message_id"]

        await q.edit_message_text("⏳ <b>Starting broadcast...</b> You will receive live progress updates below.", parse_mode=HTML)
        active_broadcast_task = asyncio.create_task(
            execute_broadcast(ctx.bot, user.id, target_type, from_chat_id, message_id)
        )
        return


async def execute_broadcast(bot, owner_id: int, target_type: str, from_chat_id: int, message_id: int):
    """Reliable rate-limited broadcast engine supporting text, custom emojis, media, stickers, videos."""
    import io
    started_at = utcnow()
    targets = await db.get_broadcast_targets(target_type)
    total = len(targets)

    if total == 0:
        await safe_send(bot, owner_id, "⚠️ No recipients found for the selected broadcast destination.")
        return

    progress_msg = None
    try:
        progress_msg = await bot.send_message(
            owner_id,
            f"📢 <b>Broadcast in progress:</b> 0/{total}",
            parse_mode=HTML,
        )
    except Exception:
        pass

    success_cnt = 0
    failed_cnt = 0
    blocked_cnt = 0
    results = []
    last_update_time = asyncio.get_event_loop().time()

    for idx, t in enumerate(targets, start=1):
        target_id = t["id"]
        status = "SUCCESS"
        err_str = None

        try:
            await bot.copy_message(chat_id=target_id, from_chat_id=from_chat_id, message_id=message_id)
            success_cnt += 1
        except RetryAfter as exc:
            wait = float(getattr(exc, "retry_after", 3.0))
            await asyncio.sleep(wait)
            try:
                await bot.copy_message(chat_id=target_id, from_chat_id=from_chat_id, message_id=message_id)
                success_cnt += 1
            except Exception as retry_exc:
                failed_cnt += 1
                status = "FAILED"
                err_str = str(retry_exc)[:150]
        except Forbidden as exc:
            blocked_cnt += 1
            status = "BLOCKED"
            err_str = str(exc)[:150]
        except TelegramError as exc:
            failed_cnt += 1
            status = "FAILED"
            err_str = str(exc)[:150]
        except Exception as exc:
            failed_cnt += 1
            status = "FAILED"
            err_str = str(exc)[:150]

        results.append({
            "id": target_id,
            "name": t.get("name", "Unknown"),
            "username": t.get("username"),
            "is_group": t.get("is_group", False),
            "status": status,
            "error": err_str,
        })

        # Periodic live progress update
        now_time = asyncio.get_event_loop().time()
        if progress_msg and (now_time - last_update_time > 3.0 or idx == total):
            try:
                await progress_msg.edit_text(
                    f"📢 <b>Broadcast Progress:</b>\n"
                    f"Processed: <b>{idx}/{total}</b> | Success: <b>{success_cnt}</b> | Failed: <b>{failed_cnt}</b> | Blocked: <b>{blocked_cnt}</b>",
                    parse_mode=HTML,
                )
                last_update_time = now_time
            except Exception:
                pass

        await asyncio.sleep(0.04)

    finished_at = utcnow()
    duration_secs = max(1, int((finished_at - started_at).total_seconds()))
    dur_str = f"{duration_secs}s" if duration_secs < 60 else f"{duration_secs // 60}m {duration_secs % 60}s"

    try:
        await db.record_broadcast({
            "sender_id": owner_id,
            "target_type": target_type,
            "from_chat_id": from_chat_id,
            "message_id": message_id,
            "total": total,
            "success": success_cnt,
            "failed": failed_cnt,
            "blocked": blocked_cnt,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": duration_secs,
        })
    except Exception as e:
        log.warning("Could not record broadcast in db: %s", e)

    dest_name = (
        "ALL BOT USERS" if target_type == "users" else
        ("ALL DEAL GROUPS" if target_type == "groups" else "BOTH (USERS & DEAL GROUPS)")
    )
    report_text = (
        "📢 <b>Broadcast Complete</b>\n\n"
        f"• <b>Destination:</b> {dest_name}\n"
        f"• <b>Total Recipients:</b> {total}\n"
        f"• <b>Successful:</b> {success_cnt}\n"
        f"• <b>Failed:</b> {failed_cnt}\n"
        f"• <b>Blocked/Kicked:</b> {blocked_cnt}\n"
        f"• <b>Duration:</b> {dur_str}\n"
    )

    if total <= 20:
        lines = ["\n<b>Recipient Breakdown:</b>"]
        for r in results:
            uname = f"@{r['username']}" if r.get("username") else "None"
            lines.append(f"• [{r['status']}] {esc(r['name'])} ({uname}, ID <code>{r['id']}</code>)")
        report_text += "\n".join(lines)
        await safe_send(bot, owner_id, report_text)
    else:
        buf = io.StringIO()
        buf.write("=====================================================\n")
        buf.write("MIDDLEMAN BOT — BROADCAST REPORT\n")
        buf.write("=====================================================\n")
        buf.write(f"Destination: {dest_name}\n")
        buf.write(f"Total: {total} | Success: {success_cnt} | Failed: {failed_cnt} | Blocked: {blocked_cnt}\n")
        buf.write(f"Duration: {dur_str}\n")
        buf.write("-----------------------------------------------------\n")
        buf.write("RECIPIENT BREAKDOWN:\n")
        for r in results:
            uname = f"@{r['username']}" if r.get("username") else "None"
            err = f" | Error: {r['error']}" if r.get("error") else ""
            buf.write(f"[{r['status']}] ID: {r['id']} | Name: {r['name']} | Username: {uname}{err}\n")
        buf.seek(0)
        doc = io.BytesIO(buf.getvalue().encode("utf-8"))
        doc.name = f"broadcast_report_{int(started_at.timestamp())}.txt"
        try:
            await bot.send_document(owner_id, document=doc, caption=report_text, parse_mode=HTML)
        except Exception:
            await safe_send(bot, owner_id, report_text)


# ==================================================================
# Bulk Deal Group Cleanup Engine (/dlt)
# ==================================================================

active_cleanup_task: Optional[asyncio.Task] = None


async def dlt_cmd(update, ctx):
    """Middleman /dlt command: Bulk delete & cleanup old closed deal groups."""
    user = update.effective_user
    msg = update.effective_message
    if not _is_mm(user.id, user.username):
        await msg.reply_text("⚠️ This command is reserved for the middleman.")
        return

    # Check if a custom batch argument was passed: /dlt 10, /dlt 20, /dlt 50, /dlt all
    if ctx.args:
        arg = ctx.args[0].lower().strip()
        if arg in ("all", "max"):
            limit = None
        elif arg.isdigit() and int(arg) > 0:
            limit = int(arg)
        else:
            await msg.reply_text("❌ Invalid batch size. Usage: <code>/dlt</code>, <code>/dlt 10</code>, <code>/dlt 20</code>, <code>/dlt 50</code>, or <code>/dlt all</code>.", parse_mode=HTML)
            return

        await _start_cleanup(ctx.bot, user.id, msg, limit)
        return

    count = await db.get_closed_deals_pending_cleanup_count()
    if count == 0:
        await msg.reply_text("✅ <b>All Clean!</b> No closed deal groups are pending cleanup.", parse_mode=HTML)
        return

    text = (
        "🧹 <b>Destroy Closed Groups (/dlt)</b>\n\n"
        f"Found <b>{count}</b> closed deal group(s) waiting for cleanup.\n\n"
        "Destroying a group will:\n"
        "• Permanently delete the group for all participants\n"
        "• Delete from the middleman's account (frees group limits)\n"
        "• Revoke all active invite links\n"
        "• Mark deal record as cleaned in database\n\n"
        "Select batch size to destroy/clean:"
    )
    kb = InlineKeyboardMarkup([
        [
            _btn("💥 Destroy 10", "dlt:batch:10", BLUE),
            _btn("💥 Destroy 20", "dlt:batch:20", BLUE),
        ],
        [
            _btn("💥 Destroy 50", "dlt:batch:50", BLUE),
            _btn(f"🔥 Destroy All ({count})", "dlt:batch:all", GREEN),
        ],
        [
            _btn("❌ Cancel", "dlt:cancel", RED),
        ],
    ])
    await msg.reply_text(text, reply_markup=kb, parse_mode=HTML)


async def dlt_callback(update, ctx):
    """Handles /dlt inline button callbacks."""
    q = update.callback_query
    user = q.from_user
    if not _is_mm(user.id, user.username):
        await q.answer("Unauthorized", show_alert=True)
        return

    parts = q.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "cancel":
        await q.answer()
        try:
            await q.edit_message_text("❌ Deal group cleanup cancelled.")
        except Exception:
            pass
        return

    if action == "menu":
        await q.answer()
        count = await db.get_closed_deals_pending_cleanup_count()
        if count == 0:
            await q.edit_message_text("✅ <b>All Clean!</b> No closed deal groups are pending cleanup.", parse_mode=HTML)
            return

        text = (
            "🧹 <b>Destroy Closed Groups (/dlt)</b>\n\n"
            f"Found <b>{count}</b> closed deal group(s) waiting for cleanup.\n\n"
            "Select batch size to destroy:"
        )
        kb = InlineKeyboardMarkup([
            [
                _btn("💥 Destroy 10", "dlt:batch:10", BLUE),
                _btn("💥 Destroy 20", "dlt:batch:20", BLUE),
            ],
            [
                _btn("💥 Destroy 50", "dlt:batch:50", BLUE),
                _btn(f"🔥 Destroy All ({count})", "dlt:batch:all", GREEN),
            ],
            [
                _btn("❌ Cancel", "dlt:cancel", RED),
            ],
        ])
        await q.edit_message_text(text, reply_markup=kb, parse_mode=HTML)
        return

    if action == "batch":
        await q.answer()
        batch_val = parts[2]
        limit = None if batch_val == "all" else int(batch_val)
        await _start_cleanup(ctx.bot, user.id, q.message, limit, edit_msg=True)
        return


async def _start_cleanup(bot, owner_id: int, msg, limit: Optional[int], edit_msg: bool = False):
    global active_cleanup_task
    if active_cleanup_task and not active_cleanup_task.done():
        if edit_msg:
            await msg.edit_text("⚠️ A cleanup task is already running. Please wait for it to complete.")
        else:
            await msg.reply_text("⚠️ A cleanup task is already running. Please wait for it to complete.")
        return

    status_text = "⏳ <b>Starting deal group destruction & cleanup batch...</b>"
    if edit_msg:
        status_msg = await msg.edit_text(status_text, parse_mode=HTML)
    else:
        status_msg = await msg.reply_text(status_text, parse_mode=HTML)

    active_cleanup_task = asyncio.create_task(
        execute_cleanup(bot, owner_id, limit, status_msg)
    )


async def execute_cleanup(bot, owner_id: int, limit: Optional[int], status_msg=None):
    """Executes bulk destruction of closed deal groups with rate-limiting and live progress."""
    deals_to_clean = await db.get_closed_deals_pending_cleanup(limit=limit)
    total = len(deals_to_clean)

    if total == 0:
        if status_msg:
            try:
                await status_msg.edit_text("✅ No closed deal groups were found for cleanup.", parse_mode=HTML)
            except Exception:
                pass
        return

    success_cnt = 0
    skipped_cnt = 0
    last_update_time = asyncio.get_event_loop().time()

    for idx, deal in enumerate(deals_to_clean, start=1):
        gid = deal.get("group_chat_id")
        deal_no = deal.get("deal_no")

        if gid:
            # 1. Revoke invite links
            try:
                await service.revoke_deal_invite(bot, deal)
            except Exception:
                pass

            # 2. Permanently destroy the group via middleman's userbot account (deletes for all & frees MM group limit)
            destroyed = False
            try:
                destroyed = await userbot.delete_deal_group(gid)
            except Exception as e:
                log.warning("Userbot group deletion attempt failed for deal %s: %s", deal_no, e)

            if destroyed:
                success_cnt += 1
            else:
                # Fallback if userbot could not delete channel (e.g. userbot offline / permission):
                # Remove participants (buyer & seller)
                try:
                    if deal.get("buyer_id"):
                        await bot.ban_chat_member(gid, deal["buyer_id"])
                except Exception:
                    pass
                try:
                    if deal.get("seller_id"):
                        await bot.ban_chat_member(gid, deal["seller_id"])
                except Exception:
                    pass

                # Rename group if possible
                try:
                    await bot.set_chat_title(gid, f"Closed Deal #{deal_no}")
                except Exception:
                    pass

                # Bot leaves the chat
                try:
                    await bot.leave_chat(gid)
                    success_cnt += 1
                except TelegramError:
                    skipped_cnt += 1
                except Exception:
                    skipped_cnt += 1
        else:
            skipped_cnt += 1

        # 3. Mark deal cleaned in database
        try:
            await db.mark_deal_cleaned(deal["_id"])
        except Exception as e:
            log.warning("Could not mark deal cleaned: %s", e)

        # Periodic live progress
        now_time = asyncio.get_event_loop().time()
        if status_msg and (now_time - last_update_time > 2.5 or idx == total):
            try:
                await status_msg.edit_text(
                    f"🧹 <b>Destroying Closed Deal Groups...</b>\n"
                    f"Processed: <b>{idx}/{total}</b> | Destroyed: <b>{success_cnt}</b> | Skipped: <b>{skipped_cnt}</b>",
                    parse_mode=HTML,
                )
                last_update_time = now_time
            except Exception:
                pass

        await asyncio.sleep(0.08)

    remaining_cnt = await db.get_closed_deals_pending_cleanup_count()

    summary_text = (
        "✅ <b>Cleanup Complete</b>\n\n"
        f"• <b>Groups Destroyed & Cleaned:</b> {success_cnt}\n"
        f"• <b>Already Destroyed / Skipped:</b> {skipped_cnt}\n"
        f"• <b>Remaining Closed Groups:</b> {remaining_cnt}\n"
    )

    kb = None
    if remaining_cnt > 0:
        kb = InlineKeyboardMarkup([
            [
                _btn("🧹 Destroy Next Batch", "dlt:menu", BLUE),
                _btn("❌ Close", "set:close", RED),
            ]
        ])

    if status_msg:
        try:
            await status_msg.edit_text(summary_text, reply_markup=kb, parse_mode=HTML)
            return
        except Exception:
            pass

    await safe_send(bot, owner_id, summary_text, reply_markup=kb)


async def cancel_cmd(update, ctx):
    """Universal /cancel command for admin/middleman wizards."""
    user = update.effective_user
    msg = update.effective_message
    if user and user.id in _admin_sessions:
        del _admin_sessions[user.id]
        await msg.reply_text("❌ Current action has been cancelled.")
        return
    await msg.reply_text("ℹ️ No active action to cancel.")

