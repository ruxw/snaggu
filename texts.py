from html import escape as esc
import re
from typing import Any, Optional

import config
import emojis
import settings
from utils import fmt_dt, fmt_duration, get_currency_symbol, user_mention

MM = f"@{config.MM_USERNAME}"


def deal_label(deal) -> str:
    return f"#{config.DEAL_ID_PREFIX}{deal['deal_no']}"


def welcome() -> str:
    return (
        f"🛡️ <b>Official Escrow Service</b>\n\n"
        f"Escrower: <b>{MM}</b>\n\n"
        "• Tap <b>Start Deal</b> or send /deal to begin\n"
        "• Tap <b>Support / Chat</b> to reach the escrower directly"
    )


def _amount(d) -> str:
    cur = str(d.get("currency", "")).strip()
    amt = str(d.get("amount", "")).strip()
    sym = get_currency_symbol(cur)
    if sym in ("₹", "$", "€", "£"):
        return f"{sym}{esc(amt)}"
    if cur:
        return f"{esc(amt)} {esc(cur)}"
    return esc(amt)


def resolve_deal_fee(amt_str: Any, curr_str: str = "INR", custom_fee: Optional[str] = None) -> str:
    """Resolves effective fee string for a deal.
    If deal has a custom_fee set by MM, uses that.
    Otherwise checks tiered fee chart from settings if enabled.
    Falls back to default_fee."""
    if custom_fee and str(custom_fee).strip():
        return str(custom_fee).strip()
    if settings.get("fee_chart_enabled", True):
        try:
            nums = re.findall(r"\d+(?:\.\d+)?", str(amt_str or "0").replace(",", ""))
            if nums:
                val = float(nums[0])
                slabs = settings.get("fee_chart_slabs", [])
                chart_fee = settings.get_chart_fee_for_amount(val, slabs)
                if chart_fee:
                    return chart_fee
        except Exception:
            pass
    return settings.get("default_fee", "4%")


def calculate_fee_details(amt_str: str, fee_str: Optional[str], curr_str: str, custom_fee: Optional[str] = None):
    """Calculates fee amount and total amount supporting flat fees, percentage fees (e.g. '4%'),
    and tiered fee charts.
    Returns (fee_val, fee_disp, tot_val, tot_disp).
    """
    from decimal import Decimal
    sym = get_currency_symbol(curr_str)
    cur = str(curr_str or "").strip()
    try:
        a_nums = re.findall(r"\d+(?:\.\d+)?", str(amt_str).replace(",", ""))
        a_val = Decimal(a_nums[0]) if a_nums else Decimal(0)
    except Exception:
        a_val = Decimal(0)

    # If fee_str is not provided, resolve it
    if not fee_str or fee_str == "None":
        fee_str = resolve_deal_fee(amt_str, curr_str, custom_fee=custom_fee)

    fee_val = Decimal(0)
    fee_disp = str(fee_str or "").strip()
    try:
        f_nums = re.findall(r"\d+(?:\.\d+)?", str(fee_str).replace(",", ""))
        if "%" in str(fee_str) and f_nums:
            pct = Decimal(f_nums[0])
            fee_val = (a_val * pct) / Decimal(100)
            fee_disp = f"{pct:g}%"
        elif f_nums:
            fee_val = Decimal(f_nums[0])
            # If fee was just a number like 35 or 50, format with currency
            if sym in ("₹", "$", "€", "£") and sym not in fee_disp and cur not in fee_disp:
                fee_disp = f"{sym}{fee_val:g}"
            elif cur and cur not in fee_disp and sym not in fee_disp:
                fee_disp = f"{fee_val:g} {cur}".strip()
            else:
                fee_disp = f"{fee_val:g}"
    except Exception:
        pass

    tot_val = a_val + fee_val
    if sym in ("₹", "$", "€", "£"):
        tot_disp = f"{sym}{tot_val:g}"
    elif cur:
        tot_disp = f"{tot_val:g} {cur}".strip()
    else:
        tot_disp = f"{tot_val:g}"

    return fee_val, fee_disp, tot_val, tot_disp


def _calc_total(amt_str: str, fee_str: str, curr_str: str) -> str:
    _, _, _, tot_disp = calculate_fee_details(amt_str, fee_str, curr_str)
    return tot_disp


def summary_text(d) -> str:
    fee_raw = resolve_deal_fee(d.get("amount", "0"), d.get("currency", "INR"), custom_fee=d.get("fee"))
    _, fee_info, _, _ = calculate_fee_details(d.get("amount", "0"), fee_raw, d.get("currency", "INR"))
    fee_info = esc(fee_info)
    return (
        "🤝 <b>DEAL SUMMARY</b>\n\n"
        f"👤 <b>Buyer:</b> @{esc(d['buyer_username'])}\n"
        f"👤 <b>Seller:</b> @{esc(d['seller_username'])}\n"
        f"🛡️ <b>Escrower:</b> {MM}\n\n"
        f"📦 <b>Item:</b> {esc(d['purpose'])}\n"
        f"💰 <b>Amount:</b> {_amount(d)}\n"
        f"⏱ <b>TAT:</b> {fmt_duration(d['tat_seconds'])}\n"
        f"📜 <b>Conditions:</b> {esc(d['conditions'])}\n\n"
        f"<b>MM Fee:</b> {fee_info}\n\n"
        "Both parties must start the bot to proceed."
    )


def request_text(d) -> str:
    role = "Buyer" if d.get("initiator_role") == "buyer" else "Seller"
    fee_raw = resolve_deal_fee(d.get("amount", "0"), d.get("currency", "INR"), custom_fee=d.get("fee"))
    _, fee_info, _, _ = calculate_fee_details(d.get("amount", "0"), fee_raw, d.get("currency", "INR"))
    fee_info = esc(fee_info)
    return (
        "🤝 <b>NEW DEAL REQUEST</b>\n\n"
        f"👤 <b>From:</b> @{esc(d['initiator_username'])} ({role})\n"
        f"🛡️ <b>Escrower:</b> {MM}\n\n"
        f"📦 <b>Item:</b> {esc(d['purpose'])}\n"
        f"💰 <b>Amount:</b> {_amount(d)}\n"
        f"⏱ <b>TAT:</b> {fmt_duration(d['tat_seconds'])}\n"
        f"📜 <b>Conditions:</b> {esc(d['conditions'])}\n\n"
        f"<b>MM Fee:</b> {fee_info}\n\n"
        "Do you accept this deal?"
    )


def pinned_text(d) -> str:
    b_un = d.get("buyer_username")
    s_un = d.get("seller_username")
    buyer_tag = f"@{esc(b_un)}" if b_un else user_mention(d.get("buyer_id", 0), "Buyer")
    seller_tag = f"@{esc(s_un)}" if s_un else user_mention(d.get("seller_id", 0), "Seller")
    return instant_deal_summary(d, buyer_tag, seller_tag)


def mm_dm_text(d) -> str:
    return (
        f"🤝 <b>Deal Initiated {deal_label(d)}</b>\n\n"
        f"Buyer: @{esc(d['buyer_username'])}\n"
        f"Seller: @{esc(d['seller_username'])}\n"
        f"Item: {esc(d['purpose'])}\n"
        f"Amount: {_amount(d)}\n"
        f"GC Link: {esc(d['invite_link'])}"
    )


def gc_created_text(link: str) -> str:
    return (
        "🤝 <b>Deal Group Created</b>\n\n"
        f"Invite Link: {esc(link)}\n\n"
        "Share this link with both parties to join."
    )


def expiry_text(d) -> str:
    tat_dur = fmt_duration(d.get("tat_seconds", 0))
    b_un = d.get("buyer_username")
    s_un = d.get("seller_username")
    buyer_m = f"@{esc(b_un)}" if b_un else user_mention(d.get("buyer_id", 0), "Buyer")
    seller_m = f"@{esc(s_un)}" if s_un else user_mention(d.get("seller_id", 0), "Seller")
    mm_m = f"@{esc(config.MM_USERNAME.lstrip('@'))}"
    dno = d.get("deal_no", "")

    return (
        "⚠️ <b>TAT EXPIRED</b>\n\n"
        f"The turnaround time ({esc(tat_dur)}) for <b>Deal #{config.DEAL_ID_PREFIX}{dno}</b> has expired.\n\n"
        f"Buyer: {buyer_m}\n"
        f"Seller: {seller_m}\n"
        f"Escrower: {mm_m}\n\n"
        f"{buyer_m}, confirm delivery status. {mm_m}, please review."
    )


# ------------------------------------------------------------- Live Chat
def livechat_welcome() -> str:
    return (
        "💬 <b>Support & Live Chat</b>\n\n"
        f"Send your message directly to Escrower {MM}.\n"
        "All replies will be delivered directly here."
    )


def livechat_user_msg_alert(sender_name: str, username: str, user_id: int, text: str) -> str:
    u = f"@{esc(username)}" if username else "No username"
    return (
        "💬 <b>Support Message</b>\n\n"
        f"From: <b>{esc(sender_name)}</b> ({u}) [<code>{user_id}</code>]\n\n"
        f"{esc(text)}"
    )


# ------------------------------------------------------------- Instant /mm Deal GC
def instant_deal_details_prompt(tos_url: str = None) -> str:
    return (
        "📋 <b>Deal Details Required</b>\n\n"
        "Send your deal details or chat naturally:\n"
        "• <b>Item:</b> (e.g. Instagram account)\n"
        "• <b>Amount:</b> (e.g. 1500 UPI)\n"
        "• <b>TAT:</b> (e.g. 2h, 24h)\n\n"
        "🤖 <i>AI is active — details will be auto-traced from your chat.</i>\n"
        "<i>Middleman can also use /edeal or /editdeal</i>"
    )


def instant_deal_summary(d: dict, buyer_tag: str, seller_tag: str) -> str:
    amt = str(d.get("amount", "0")).strip()
    curr = str(d.get("currency", "INR")).strip()
    sym = get_currency_symbol(curr)
    if sym in ("₹", "$", "€", "£"):
        amt_str = f"{sym}{amt}"
    elif curr:
        amt_str = f"{amt} {curr}"
    else:
        amt_str = amt

    dno = d.get("deal_no", "")
    item = esc(str(d.get("purpose") or d.get("deal_type") or "N/A"))
    tat = esc(str(d.get("tat") or (fmt_duration(d.get("tat_seconds", 0)) if d.get("tat_seconds") else "24h")))
    payment = esc(str(d.get("payment_network") or d.get("payment_method") or "UPI"))
    conditions = esc(str(d.get("conditions") or "None"))
    fee_raw = resolve_deal_fee(amt, curr, custom_fee=d.get("custom_fee"))
    _, fee_disp, _, _ = calculate_fee_details(amt, fee_raw, curr)
    fee = esc(fee_disp)

    return (
        f"<b>MMd {dno}</b>\n\n"
        f"Buyer: {buyer_tag}\n"
        f"Seller: {seller_tag}\n\n"
        f"Item: {item}\n"
        f"Amount: {amt_str}\n"
        f"Payment: {payment} | TAT: {tat}\n"
        f"Conditions: {conditions}\n\n"
        f"MM Fee: {fee}"
    )


def buyer_confirmed_wait_mm_text(buyer_tag: str) -> str:
    mm_tag = f"@{config.MM_USERNAME.lstrip('@')}"
    return (
        f"✅ {buyer_tag} confirmed the deal.\n\n"
        f"{mm_tag}, review and confirm to proceed to payment."
    )


def proceeding_to_payment_text() -> str:
    return "⏳ Proceeding to payment..."


def buyer_confirmed_text(buyer_tag: str) -> str:
    return f"✅ {buyer_tag} confirmed the deal.\n\n⏳ Proceeding to payment..."


def payment_details_upi(amount: str, currency: str, fee: str, upi_id: str) -> str:
    fee_val, fee_disp, _, total_str = calculate_fee_details(amount, fee, currency)
    sym = get_currency_symbol(currency)
    disp_amt = f"{sym}{amount}" if sym in ("₹", "$", "€", "£") else f"{amount} {currency}"
    fee_text = f"{fee_disp} ({sym}{fee_val:g})" if ("%" in fee_disp and sym in ("₹", "$", "€", "£") and fee_val > 0) else fee_disp
    return (
        "💳 <b>PAYMENT DETAILS</b>\n\n"
        f"Amount: {esc(disp_amt)}\n"
        f"MM Fee: {esc(fee_text)}\n"
        f"<b>Total: {esc(total_str)}</b>\n\n"
        f"UPI: <code>{esc(upi_id)}</code>\n\n"
        "After paying, tap below and submit your payment screenshot."
    )


def payment_details_crypto_prompt(amount: str, currency: str, fee: str) -> str:
    fee_val, fee_disp, _, total_str = calculate_fee_details(amount, fee, currency)
    fee_text = f"{fee_disp} ({fee_val:g} {currency})" if ("%" in fee_disp and fee_val > 0) else fee_disp
    return (
        "🪙 <b>CRYPTO PAYMENT</b>\n\n"
        f"Amount: {esc(amount)} {esc(currency)}\n"
        f"MM Fee: {esc(fee_text)}\n"
        f"<b>Total: {esc(total_str)}</b>\n\n"
        "Select network below:"
    )


def payment_details_crypto_address(coin_name: str, symbol: str, network: str, address: str) -> str:
    return (
        f"🪙 <b>{esc(coin_name)} · {esc(network)}</b>\n\n"
        "Deposit address:\n"
        f"<code>{esc(address)}</code>\n\n"
        f"Send only via {esc(network)}.\n"
        "After paying, tap below and submit your transaction hash."
    )


def payment_verification_text(buyer_tag: str) -> str:
    return f"💳 {buyer_tag}, tap below once you have sent the payment."


def payment_pending_acknowledgement() -> str:
    return f"⏳ Waiting for escrower {MM} to verify payment receipt."


# ------------------------------------------------------------- Stats
def stats_display(s: dict) -> str:
    return (
        "📊 <b>Escrow Statistics</b>\n\n"
        f"• Total Deals: <b>{s['total_deals']}</b>\n"
        f"• Active Deals: <b>{s['active_deals']}</b>\n"
        f"• Completed Deals: <b>{s['completed_deals']}</b>\n"
        f"• Total Users: <b>{s['total_users']}</b>\n\n"
        f"🛡️ <b>Escrower:</b> {MM}"
    )


def exchange_summary_text(d: dict, initiator_username: str) -> str:
    fee_info = esc(str(d.get("fee") or settings.get("default_fee", "Charged separately")))
    return (
        "🔄 <b>EXCHANGE SUMMARY</b>\n\n"
        f"👤 <b>Initiator:</b> @{esc(initiator_username)}\n"
        f"👤 <b>Counterparty:</b> @{esc(d.get('ex_cp', ''))}\n"
        f"🛡️ <b>Escrower:</b> {MM}\n\n"
        f"📤 <b>Giving:</b> {esc(d.get('ex_give_amount', ''))}\n"
        f"📥 <b>Receiving:</b> {esc(d.get('ex_receive_amount', ''))}\n"
        f"⏱ <b>TAT:</b> {fmt_duration(d.get('ex_tat_seconds', 3600))}\n"
        f"📜 <b>Conditions:</b> {esc(str(d.get('conditions', 'None')))}\n\n"
        f"<b>MM Fee:</b> {fee_info}\n\n"
        "Confirm below to send exchange request."
    )


def exchange_request_text(d: dict) -> str:
    fee_info = esc(str(d.get("fee") or settings.get("default_fee", "Charged separately")))
    return (
        "🔄 <b>NEW EXCHANGE REQUEST</b>\n\n"
        f"👤 <b>From:</b> @{esc(d.get('initiator_username', ''))}\n"
        f"🛡️ <b>Escrower:</b> {MM}\n\n"
        f"📤 <b>They Give:</b> {esc(d.get('ex_give_amount', ''))}\n"
        f"📥 <b>They Receive:</b> {esc(d.get('ex_receive_amount', ''))}\n"
        f"⏱ <b>TAT:</b> {fmt_duration(d.get('tat_seconds', 3600))}\n"
        f"📜 <b>Conditions:</b> {esc(str(d.get('conditions', 'None')))}\n\n"
        f"<b>MM Fee:</b> {fee_info}\n\n"
        "Do you accept this exchange deal?"
    )
