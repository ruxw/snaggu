"""AI-powered and deterministic NLP deal details parser.
Extracts deal type, turnaround time (TAT), conditions, amount, currency, and payment network.
Supports Google Gemini REST API with robust local fallback.
"""
import json
import logging
import re
from typing import Any, Dict, Optional

import httpx

import config
from utils import parse_tat

log = logging.getLogger(__name__)

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"


async def parse_deal_details(text: str) -> Optional[Dict[str, Any]]:
    """Attempts AI parsing if GEMINI_API_KEY is configured; falls back to deterministic NLP parser."""
    if config.GEMINI_API_KEY:
        try:
            res = await _call_gemini(text)
            if res and res.get("amount") and res.get("deal_type"):
                return res
        except Exception as e:
            log.warning("Gemini AI parse failed or timed out, falling back to local NLP: %s", e)

    return _parse_locally(text)


async def _call_gemini(text: str) -> Optional[Dict[str, Any]]:
    url = f"{GEMINI_ENDPOINT}?key={config.GEMINI_API_KEY}"
    prompt = (
        "You are an escrow middleman assistant parsing deal information. "
        "Extract the deal details from the text below and return ONLY valid JSON matching this schema:\n"
        "{\n"
        '  "deal_type": "<what is being traded>",\n'
        '  "tat": "<turnaround time string, e.g. 24h, 2d, 1h>",\n'
        '  "conditions": "<terms, rules or TOS channel link, or \'None\'>",\n'
        '  "amount": "<numeric amount, e.g. 500, 1500.5>",\n'
        '  "currency": "<INR, USD, USDT, BTC, ETH, SOL, TON, or Crypto>",\n'
        '  "payment_network": "<UPI, or Crypto network like TRC20, BEP20, etc.>"\n'
        "}\n\n"
        f"Input text:\n{text}"
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }

    async with httpx.AsyncClient(timeout=8.0) as client:
        r = await client.post(url, json=payload)
        if r.status_code == 200:
            data = r.json()
            raw_json = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(raw_json)
            _normalize_fields(parsed)
            return parsed
    return None


def _parse_locally(text: str) -> Optional[Dict[str, Any]]:
    """Smart deterministic extraction based on regex & line-by-line keywords."""
    clean = text.strip()
    if not clean:
        return None

    # Expand shorthand numbers like 5k -> 5000, 1.5k -> 1500
    expanded_text = re.sub(
        r"\b(\d+(?:\.\d+)?)\s*[kK]\b",
        lambda m: str(int(float(m.group(1)) * 1000)),
        clean,
    )

    deal_type = None
    tat_str = "24h"
    conditions = "None"
    amount = None
    currency = "INR"
    network = "UPI"

    # Split lines and handle pipe separators (e.g. "Payment: UPI | TAT: 24h")
    raw_segments = []
    for line in expanded_text.splitlines():
        line = line.strip()
        if not line:
            continue
        # If line contains '|' separating key:value pairs, split it
        if "|" in line and ":" in line:
            for part in line.split("|"):
                if part.strip():
                    raw_segments.append(part.strip())
        else:
            raw_segments.append(line)

    for seg in raw_segments:
        lower = seg.lower()

        # Deal Type / Item
        if any(lower.startswith(k) for k in ("deal type:", "type:", "item:", "trading:", "product:", "service:", "account:")):
            deal_type = re.sub(r"^(deal type|type|item|trading|product|service|account):\s*", "", seg, flags=re.I).strip()
        # TAT
        elif any(lower.startswith(k) for k in ("tat:", "time:", "turnaround:", "duration:")):
            tat_str = re.sub(r"^(tat|time|turnaround|duration):\s*", "", seg, flags=re.I).strip()
        # Conditions
        elif any(lower.startswith(k) for k in ("conditions:", "condition:", "terms:", "tos:", "rules:")):
            conditions = re.sub(r"^(conditions|condition|terms|tos|rules):\s*", "", seg, flags=re.I).strip()
        # Amount & Fees
        elif any(lower.startswith(k) for k in ("amount & fees:", "amount:", "price:", "total:", "amt:")):
            raw_amt = re.sub(r"^(amount & fees|amount|price|total|amt):\s*", "", seg, flags=re.I).strip()
            # Extract currency if mentioned in the same line
            if re.search(r"\b(usdt|crypto|btc|eth|sol|ton)\b", raw_amt, re.I):
                currency = "Crypto"
            elif re.search(r"\b(inr|₹|rs)\b", raw_amt, re.I):
                currency = "INR"
            elif re.search(r"\b(usd|\$)\b", raw_amt, re.I):
                currency = "USD"
            nums = re.findall(r"\d+(?:\.\d+)?", raw_amt.replace(",", ""))
            if nums:
                amount = nums[0]
        # Currency
        elif any(lower.startswith(k) for k in ("currency:", "curr:")):
            currency = re.sub(r"^(currency|curr):\s*", "", seg, flags=re.I).strip()
        # Payment Network / Method
        elif any(lower.startswith(k) for k in ("payment network:", "network:", "payment method:", "method:", "payment:")):
            network = re.sub(r"^(payment network|network|payment method|method|payment):\s*", "", seg, flags=re.I).strip()

    # Fallbacks if format wasn't exact key:value
    if not amount:
        # Check for currency symbols with amounts: ₹5005, $100, 5005rs
        cur_amt = re.search(r"(?:₹|\$|rs\.?|inr)\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:rs|inr|usd|usdt)", expanded_text, re.I)
        if cur_amt:
            amount = cur_amt.group(1) or cur_amt.group(2)
        else:
            nums = re.findall(r"\b\d+(?:\.\d+)?\b", expanded_text)
            if nums:
                amount = nums[0]

    if not deal_type:
        # If user just sent text, grab the first non-trivial line
        for l in raw_segments:
            if not any(k in l.lower() for k in ("tat", "amount", "payment", "currency", "conditions", "buyer", "seller", "mm fee", "mmd")):
                deal_type = l
                break

    if not deal_type or not amount:
        return None

    out = {
        "deal_type": deal_type[:200],
        "tat": tat_str,
        "conditions": conditions[:300] if conditions else "None",
        "amount": amount,
        "currency": currency,
        "payment_network": network,
    }
    _normalize_fields(out)
    return out


def validate_parsed_deal(parsed: Optional[Dict[str, Any]]) -> tuple:
    """Validates if parsed deal has required fields (Item & Amount).
    Returns (is_valid, list_of_missing_fields).
    """
    missing = []
    if not parsed:
        return False, ["Item", "Amount"]
    if not parsed.get("deal_type") or parsed.get("deal_type") in ("N/A", "None", ""):
        missing.append("Item")
    if not parsed.get("amount") or str(parsed.get("amount")) in ("0", "0.0", ""):
        missing.append("Amount")
    return len(missing) == 0, missing


def normalize_currency_and_network(currency_raw: str, network_raw: str):
    """Normalize currency and payment network strings into standard representations."""
    c = (currency_raw or "").strip()
    n = (network_raw or "").strip()

    c_low = c.lower()
    n_low = n.lower()

    # Network detection from both fields
    detected_net = None
    if any(k in n_low or k in c_low for k in ("trc20", "trc-20", "trc")):
        detected_net = "TRC-20"
    elif any(k in n_low or k in c_low for k in ("bep20", "bep-20", "bep", "bsc")):
        detected_net = "BEP-20"
    elif any(k in n_low or k in c_low for k in ("erc20", "erc-20", "erc")):
        detected_net = "ERC-20"
    elif any(k in n_low or k in c_low for k in ("ton mainnet", "ton network", "ton")):
        detected_net = "TON"
    elif any(k in n_low or k in c_low for k in ("solana", "sol mainnet", "spl")):
        detected_net = "SOL"
    elif any(k in n_low or k in c_low for k in ("upi", "gpay", "paytm", "phonepe")):
        detected_net = "UPI"
    elif any(k in n_low for k in ("bitcoin", "btc", "ethereum", "eth", "crypto", "blockchain")):
        detected_net = "Crypto"

    # Currency normalization
    norm_cur = "INR"
    if any(k in c_low for k in ("usdt", "tether", "usd-t")) or ("usdt" in n_low and "inr" not in c_low):
        norm_cur = "USDT"
    elif any(k in c_low for k in ("btc", "bitcoin")):
        norm_cur = "BTC"
    elif any(k in c_low for k in ("eth", "ethereum")):
        norm_cur = "ETH"
    elif any(k in c_low for k in ("sol", "solana")) and "usdt" not in c_low:
        norm_cur = "SOL"
    elif any(k in c_low for k in ("ton", "toncoin")) and "usdt" not in c_low:
        norm_cur = "TON"
    elif any(k in c_low for k in ("ltc", "litecoin")):
        norm_cur = "LTC"
    elif any(k in c_low for k in ("trx", "tron")) and "usdt" not in c_low and "trc" not in c_low:
        norm_cur = "TRX"
    elif any(k in c_low for k in ("bnb", "binance")) and "usdt" not in c_low:
        norm_cur = "BNB"
    elif any(k in c_low for k in ("xrp", "ripple")):
        norm_cur = "XRP"
    elif any(k in c_low for k in ("doge", "dogecoin")):
        norm_cur = "DOGE"
    elif any(k in c_low for k in ("matic", "polygon", "pol")):
        norm_cur = "POL"
    elif any(k in c_low for k in ("usd", "$", "dollar")) and "usdt" not in c_low:
        norm_cur = "USD"
    elif "crypto" in c_low or "crypto" in n_low:
        norm_cur = "Crypto"
    elif any(k in c_low for k in ("inr", "₹", "rs", "rupee", "rupees")):
        norm_cur = "INR"
    elif c:
        norm_cur = c.upper() if len(c) <= 6 else c

    # Finalize network
    if norm_cur == "INR":
        norm_net = "UPI"
    elif detected_net:
        norm_net = detected_net
    elif n:
        norm_net = n
    else:
        norm_net = "Crypto" if norm_cur in ("USDT", "BTC", "ETH", "SOL", "TON", "Crypto") else "UPI"

    return norm_cur, norm_net


def _normalize_fields(d: Dict[str, Any]):
    tat_str = str(d.get("tat", "24h"))
    try:
        secs = parse_tat(tat_str)
    except Exception:
        secs = 24 * 3600
    d["tat_seconds"] = secs
    d["amount"] = str(d.get("amount", "0")).replace(",", "").strip()
    if not d.get("conditions"):
        d["conditions"] = "None"
    cur, net = normalize_currency_and_network(d.get("currency", "INR"), d.get("payment_network", "UPI"))
    d["currency"] = cur
    d["payment_network"] = net


async def generate_deal_reasoning(deal: dict, chat_messages: list = None, extra_context: str = "") -> str:
    """Generates a concise 5 to 6 line AI reasoning/conclusion of the deal strictly in Hinglish.
    Uses Gemini AI if configured; otherwise uses intelligent local generator."""
    if config.GEMINI_API_KEY:
        try:
            res = await _call_gemini_reasoning(deal, chat_messages, extra_context)
            if res:
                return res
        except Exception as e:
            log.warning("Gemini deal reasoning failed, using local generator: %s", e)

    return _generate_local_reasoning(deal, chat_messages)


async def _call_gemini_reasoning(deal: dict, chat_messages: list = None, extra_context: str = "") -> Optional[str]:
    deal_no = deal.get("deal_no", "")
    item = deal.get("purpose") or deal.get("deal_type") or "Product/Service"
    amt = deal.get("amount", "0")
    curr = deal.get("currency", "INR")
    b_un = deal.get("buyer_username") or "Buyer"
    s_un = deal.get("seller_username") or "Seller"
    status = deal.get("status", "active")
    pay = deal.get("payment_network", "UPI")

    deal_summary = (
        f"Deal #{deal_no}\n"
        f"Item: {item}\n"
        f"Amount: {amt} {curr}\n"
        f"Payment: {pay}\n"
        f"Buyer: @{b_un}\n"
        f"Seller: @{s_un}\n"
        f"Status: {status}\n"
    )

    chat_text = "\n".join(chat_messages) if chat_messages else "No chat history available."
    if extra_context:
        chat_text += f"\nContext message: {extra_context}"

    prompt = (
        "You are an expert escrow middleman assistant analyzing a Telegram deal chat.\n"
        "Analyze the buyer and seller messages and deal details below.\n\n"
        "Write a concise, professional, and clear 5 to 6 lines summary/conclusion strictly in HINGLISH "
        "(conversational Hindi written in English/Latin script, as commonly spoken in Indian trading communities).\n"
        "Cover:\n"
        "1. What the deal was about (item and amount).\n"
        "2. How the buyer and seller communicated/agreed.\n"
        "3. Middleman safely holding funds in escrow.\n"
        "4. How handover and verification took place.\n"
        "5. Final conclusion on how the deal went smooth without disputes.\n\n"
        "Rules:\n"
        "- Strictly 5 to 6 bullet lines.\n"
        "- Start each line with an emoji number: 1️⃣, 2️⃣, 3️⃣, 4️⃣, 5️⃣ (and 6️⃣ if 6 lines).\n"
        "- Language MUST be pure natural Hinglish (e.g. 'Buyer @buyer aur Seller @seller ne deal terms finalize ki...', 'Payment securely MM ke paas hold hui...', 'Deal smoothly conclude ho gayi bina kisi issue ke...').\n"
        "- DO NOT use markdown bold with ** because it breaks HTML formatting. Use <b>bold text</b> tags instead.\n"
        "- Return ONLY the 5-6 bullet lines, nothing else.\n"
        "- NEVER mention AI, artificial intelligence, Gemini, or language model. Act as MiddleShield Bot's direct escrow verification.\n\n"
        f"Deal Info:\n{deal_summary}\n\n"
        f"Chat Messages:\n{chat_text}"
    )

    url = f"{GEMINI_ENDPOINT}?key={config.GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3},
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(url, json=payload)
        if r.status_code == 200:
            data = r.json()
            text_out = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            # Clean up markdown bold if any
            text_out = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", text_out)
            return text_out
    return None


def _generate_local_reasoning(deal: dict, chat_messages: list = None) -> str:
    deal_no = deal.get("deal_no", "")
    item = deal.get("purpose") or deal.get("deal_type") or "Product/Service"
    amt = deal.get("amount", "0")
    curr = deal.get("currency", "INR")
    b_un = deal.get("buyer_username") or "Buyer"
    s_un = deal.get("seller_username") or "Seller"
    pay = deal.get("payment_network", "UPI")

    lines = [
        f"1️⃣ <b>Deal Start:</b> Buyer @{b_un} aur Seller @{s_un} ke beech <b>{item}</b> ke liye deal confirm hui.",
        f"2️⃣ <b>Escrow Secure:</b> Middleman ne <b>{amt} {curr}</b> ({pay}) payment securely hold kiya tha.",
        f"3️⃣ <b>Handover:</b> Seller ne deal credentials/item smoothly Buyer ko provide kiye.",
        f"4️⃣ <b>Verification:</b> Buyer ne check karke verify kiya ki sabkuch bilkul deal terms ke mutabiq hai.",
        f"5️⃣ <b>Smooth Conclusion:</b> Dono parties ka coordination best raha aur deal completely safe & smooth conclude hui.",
    ]
    return "\n".join(lines)


async def parse_deal_edits(text: str, current_deal: dict = None) -> Dict[str, Any]:
    """Extracts modified deal fields from command arguments or natural language text."""
    if config.GEMINI_API_KEY:
        try:
            res = await _call_gemini_edits(text)
            if res:
                return res
        except Exception as e:
            log.warning("Gemini deal edit parse failed: %s", e)

    return _parse_edits_locally(text)


async def _call_gemini_edits(text: str) -> Optional[Dict[str, Any]]:
    url = f"{GEMINI_ENDPOINT}?key={config.GEMINI_API_KEY}"
    prompt = (
        "You are an assistant for an escrow bot. Extract deal update fields from the input text.\n"
        "Only extract fields that the user explicitly wants to update or specifies.\n"
        "Supported JSON keys (only include keys that are present):\n"
        "- deal_type: string\n"
        "- amount: numeric string\n"
        "- currency: INR, USD, USDT, etc.\n"
        "- payment_network: UPI, TRC-20, BEP-20, Bank, etc.\n"
        "- tat: duration string like 12h, 24h, 2d\n"
        "- custom_fee: fee string like 5% or 50 INR\n"
        "- conditions: string\n"
        "- buyer_username: string without @\n"
        "- seller_username: string without @\n\n"
        "Return ONLY valid JSON matching this schema.\n\n"
        f"Input:\n{text}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }
    async with httpx.AsyncClient(timeout=8.0) as client:
        r = await client.post(url, json=payload)
        if r.status_code == 200:
            data = r.json()
            raw_json = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(raw_json)
            if parsed and isinstance(parsed, dict):
                if "tat" in parsed:
                    try:
                        parsed["tat_seconds"] = parse_tat(parsed["tat"])
                    except Exception:
                        parsed["tat_seconds"] = 24 * 3600
                if "currency" in parsed or "payment_network" in parsed:
                    c, n = normalize_currency_and_network(parsed.get("currency", ""), parsed.get("payment_network", ""))
                    if "currency" in parsed:
                        parsed["currency"] = c
                    if "payment_network" in parsed:
                        parsed["payment_network"] = n
                return parsed
    return None


def _parse_edits_locally(text: str) -> Dict[str, Any]:
    clean = text.strip()
    edits = {}

    # Expand shorthand numbers like 5k -> 5000 in amount context
    expanded = re.sub(r"(?<=\bamount\s)(\d+(?:\.\d+)?)\s*[kK]\b", lambda m: str(int(float(m.group(1)) * 1000)), clean, flags=re.I)
    expanded = re.sub(r"(?<=\bamt\s)(\d+(?:\.\d+)?)\s*[kK]\b", lambda m: str(int(float(m.group(1)) * 1000)), expanded, flags=re.I)
    expanded = re.sub(r"(?<=\bprice\s)(\d+(?:\.\d+)?)\s*[kK]\b", lambda m: str(int(float(m.group(1)) * 1000)), expanded, flags=re.I)

    # 1. Buyer
    b_match = re.search(r"\b(?:buyer)\s*[:=]?\s*@?([a-zA-Z0-9_]{3,32})", expanded, re.I)
    if b_match:
        edits["buyer_username"] = b_match.group(1).lstrip("@")

    # 2. Seller
    s_match = re.search(r"\b(?:seller)\s*[:=]?\s*@?([a-zA-Z0-9_]{3,32})", expanded, re.I)
    if s_match:
        edits["seller_username"] = s_match.group(1).lstrip("@")

    # 3. Fee
    fee_match = re.search(r"\b(?:fee|mm\s*fee)\s*[:=]?\s*(\d+(?:\.\d+)?\s*%|\d+\s*(?:inr|rs|usd))", expanded, re.I)
    if fee_match:
        edits["custom_fee"] = fee_match.group(1).strip()

    # 4. TAT
    tat_match = re.search(r"\b(?:tat|time|duration)\s*[:=]?\s*(\d+\s*(?:m|min|mins|h|hr|hrs|d|day|days))\b", expanded, re.I)
    if tat_match:
        raw_tat = tat_match.group(1).strip()
        try:
            secs = parse_tat(raw_tat)
            edits["tat"] = raw_tat
            edits["tat_seconds"] = secs
        except Exception:
            pass

    # 5. Amount
    amt_match = re.search(r"\b(?:amount|amt|price|total)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]+)?\b", expanded, re.I)
    if amt_match:
        edits["amount"] = amt_match.group(1)
        if amt_match.group(2):
            raw_c = amt_match.group(2).upper()
            if raw_c in ("INR", "USD", "USDT", "BTC", "ETH", "SOL", "TON"):
                edits["currency"] = raw_c
    elif not any(k in edits for k in ("buyer_username", "seller_username", "custom_fee", "tat")):
        # Bare amount? e.g. '5000' or '5000 inr' or '5k'
        clean_bare = re.sub(r"\b(\d+(?:\.\d+)?)\s*[kK]\b", lambda m: str(int(float(m.group(1)) * 1000)), clean)
        bare_amt = re.match(r"^(?:₹|\$)?\s*(\d+(?:\.\d+)?)\s*(?:(inr|rs|usd|usdt))?$", clean_bare, re.I)
        if bare_amt:
            edits["amount"] = bare_amt.group(1)
            if bare_amt.group(2):
                edits["currency"] = "INR" if bare_amt.group(2).lower() in ("inr", "rs") else bare_amt.group(2).upper()

    # 6. Payment
    pay_match = re.search(r"\b(?:pay|payment|network|payment\s*method)\s*[:=]?\s*([a-zA-Z0-9_\-\s]{2,20})", expanded, re.I)
    if pay_match:
        raw_p = pay_match.group(1).strip()
        if "trc" in raw_p.lower():
            edits["payment_network"] = "TRC-20"
            edits.setdefault("currency", "USDT")
        elif "bep" in raw_p.lower():
            edits["payment_network"] = "BEP-20"
            edits.setdefault("currency", "USDT")
        elif "bank" in raw_p.lower():
            edits["payment_network"] = "Bank"
        elif "upi" in raw_p.lower():
            edits["payment_network"] = "UPI"
            edits.setdefault("currency", "INR")
        else:
            edits["payment_network"] = raw_p

    # 7. Item
    item_match = re.search(r"\b(?:item|product|account|deal\s*type|type)\s*[:=]?\s*([^|\n]+)", expanded, re.I)
    if item_match:
        item_val = item_match.group(1).strip()
        for kw in ("amount", "tat", "fee", "buyer", "seller", "payment", "pay", "conditions", "terms"):
            item_val = re.split(rf"\b{kw}\b", item_val, flags=re.I)[0].strip()
        if item_val:
            edits["deal_type"] = item_val

    # 8. Conditions
    cond_match = re.search(r"\b(?:cond|conditions|terms|rules)\s*[:=]?\s*([^|\n]+)", expanded, re.I)
    if cond_match:
        edits["conditions"] = cond_match.group(1).strip()

    return edits

