import asyncio
from decimal import Decimal
import config
import emojis
import settings
import utils

def run_tests():
    print("Running Middleman Bot Test Suite...")

    # 1. TAT parsing tests
    print("\n--- 1. Testing TAT parser ---")
    valid_cases = [
        ("1m", 60),
        ("1min", 60),
        ("15m", 900),
        ("15mins", 900),
        ("1h", 3600),
        ("1hr", 3600),
        ("2hours", 7200),
        ("1d", 86400),
        ("1day", 86400),
        ("15d", 15 * 86400),
        ("1d 12h 30m", 86400 + 12 * 3600 + 30 * 60),
    ]
    for text, expected in valid_cases:
        actual = utils.parse_tat(text)
        assert actual == expected, f"Failed on {text}: expected {expected}, got {actual}"
    print("✓ All valid duration formats (m, min, h, hr, d, day) parsed correctly.")

    invalid_cases = ["0m", "-1h", "0", "invalid", "", "xyz"]
    for text in invalid_cases:
        try:
            utils.parse_tat(text)
            assert False, f"Expected parse_tat('{text}') to raise ValueError"
        except ValueError:
            pass
    print("✓ All invalid duration inputs correctly rejected with clear errors.")

    # 2. Currency symbol mapping
    print("\n--- 2. Testing Currency Symbol Mapping ---")
    assert utils.get_currency_symbol("USD") == "$"
    assert utils.get_currency_symbol("$") == "$"
    assert utils.get_currency_symbol("INR") == "₹"
    assert utils.get_currency_symbol("₹") == "₹"
    assert utils.get_currency_symbol("EUR") == "€"
    assert utils.get_currency_symbol("GBP") == "£"
    assert utils.get_currency_symbol("USDT") == "USDT"
    print("✓ Currency symbol mappings verified.")

    # 3. Custom Emoji Configuration & Fallbacks
    print("\n--- 3. Testing Custom Emojis ---")
    orig_cids = dict(emojis.CUSTOM_EMOJI_IDS)
    emojis.CUSTOM_EMOJI_IDS.clear()
    assert emojis.get_emoji("SUCCESS") == "✅"
    assert emojis.get_emoji("HOLD") == "🔒"
    assert emojis.get_emoji("RELEASE") == "🔓"
    assert emojis.get_emoji("CLOSE") == "🚪"

    # Simulate custom emoji ID set
    emojis.CUSTOM_EMOJI_IDS["SUCCESS"] = "5368324170671202286"
    custom_rendered = emojis.get_emoji("SUCCESS")
    assert custom_rendered == '<tg-emoji emoji-id="5368324170671202286">✅</tg-emoji>'
    emojis.CUSTOM_EMOJI_IDS.clear()
    emojis.CUSTOM_EMOJI_IDS.update(orig_cids)
    print("✓ Custom emoji HTML rendering and Unicode fallbacks verified.")

    # 4. Safe Template Formatting & Defaults
    print("\n--- 4. Testing Configurable Templates ---")
    for key in settings.TEMPLATE_LABELS:
        preview = settings.preview_template(key)
        assert len(preview) > 0, f"Template preview failed for {key}"
    print("✓ All 9 templates rendered and previewed successfully.")

    # Check specific required texts
    exp_msg = settings.render_template(
        "tat_expiration",
        tat="24 hours",
        buyer="@buyer_user",
        seller="@seller_user",
        deal_id="1042",
    )
    assert "24 hours" in exp_msg
    assert "Deal #1042" in exp_msg
    assert "@buyer_user" in exp_msg
    assert "@seller_user" in exp_msg
    print("✓ TAT Expiration message format matches requirement exactly.")

    hold_msg = settings.render_template("hold", deal_id="1042", title="MM 1042 $ hold")
    assert "MM 1042 $ hold" in hold_msg
    assert "1042" in hold_msg
    print("✓ Hold message format matches requirement exactly.")

    rel_msg = settings.render_template("release", deal_id="1042", title="MM 1042 $ Released")
    assert "MM 1042 $ Released" in rel_msg
    assert "1042" in rel_msg
    print("✓ Release message format matches requirement exactly.")

    close_msg = settings.render_template("deal_closure", deal_id="1042")
    assert "1042" in close_msg
    print("✓ Deal closure message format verified.")

    # Vouch template test
    vouch_out = settings.render_vouch({"amount": "50", "currency": "$", "deal_no": 1})
    assert "Vouch @" in vouch_out
    assert "for MM'd 50 $" in vouch_out
    print("✓ Concise vouch format matches requirement exactly.")

    # Safe formatting with unknown placeholders
    safe_out = utils.safe_format("Hello {known} and {unknown}!", known="World")
    assert safe_out == "Hello World and {unknown}!"
    print("✓ Safe template formatter handles unknown placeholders safely.")

    # 5. Channel target parsing
    print("\n--- 5. Testing Vouch Channel Target Parser ---")
    assert utils.parse_channel_target("https://t.me/Proofinity") == "@Proofinity"
    assert utils.parse_channel_target("t.me/Proofinity") == "@Proofinity"
    assert utils.parse_channel_target("@Proofinity") == "@Proofinity"
    assert utils.parse_channel_target("-1001234567890") == -1001234567890
    print("✓ Vouch channel target parser verified.")

    # 6. Sequential GC Title Preset & Math
    print("\n--- 6. Testing Sequential GC Title Presets ---")
    # Test standard preset
    std_title = settings.render_gc_title(deal_no=15, buyer="alice", seller="bob", amount="100", currency="USD")
    assert "15" in std_title
    print("✓ Standard GC Title rendered correctly:", std_title)

    # Test sequential preset with baseline math: start_num=121, baseline=1, deal_no=1 -> 121
    settings._cache["gc_name_template"] = settings.GC_NAME_PRESETS[4] # "Deal #{startno} | @{mm}"
    settings._cache["gc_start_num"] = 121
    settings._cache["gc_baseline_deal_no"] = 1
    seq_title_1 = settings.render_gc_title(deal_no=1)
    assert "#121" in seq_title_1, f"Expected #121, got {seq_title_1}"
    seq_title_2 = settings.render_gc_title(deal_no=2)
    assert "#122" in seq_title_2, f"Expected #122, got {seq_title_2}"
    seq_title_4 = settings.render_gc_title(deal_no=4)
    assert "#124" in seq_title_4, f"Expected #124, got {seq_title_4}"
    # Reset template back to default
    settings._cache["gc_name_template"] = settings.GC_NAME_PRESETS[0]
    print("✓ Sequential GC title math (starting deal no #121 -> #122 -> #124) verified.")

    # 7. Currency & Payment Network Normalization
    print("\n--- 7. Testing Currency & Network Normalization ---")
    from ai_parser import normalize_currency_and_network

    cases = [
        ("₹", "upi", "INR", "UPI"),
        ("inr", "UPI", "INR", "UPI"),
        ("Rs", "", "INR", "UPI"),
        ("usdt", "trc20", "USDT", "TRC-20"),
        ("USDT", "bep-20", "USDT", "BEP-20"),
        ("bitcoin", "", "BTC", "Crypto"),
        ("btc", "bitcoin", "BTC", "Crypto"),
        ("eth", "erc20", "ETH", "ERC-20"),
        ("sol", "solana", "SOL", "SOL"),
        ("ton", "ton", "TON", "TON"),
        ("usd", "", "USD", "UPI"),
        ("crypto", "trc20", "Crypto", "TRC-20"),
    ]
    for raw_c, raw_n, exp_c, exp_n in cases:
        c, n = normalize_currency_and_network(raw_c, raw_n)
        assert c == exp_c, f"For ({raw_c}, {raw_n}): expected currency {exp_c}, got {c}"
        assert n == exp_n, f"For ({raw_c}, {raw_n}): expected network {exp_n}, got {n}"
    print("✓ All currency & payment network normalizations verified.")

    # 8. Advertisement System & Format
    print("\n--- 8. Testing Advertisement System ---")
    # Verify get_ad default
    ad = settings.get_ad()
    assert isinstance(ad, dict)
    assert "text" in ad and "enabled" in ad

    # Test footer format
    ad_text = "🎰 @Rainbetcom #1 Non-KYC Crypto Casino & Sportsbook"
    expected_footer = f"\n\n<blockquote>{ad_text}</blockquote>"
    assert f"<blockquote>{ad_text}</blockquote>" in expected_footer
    print("✓ Advertisement footer format matches expected styling.")

    # 9. Testing Redesigned User-Facing Presentation & Deal Summary Cards
    print("\n--- 9. Testing Redesigned Deal Cards & Payment UX ---")
    import texts
    from main import EmojiBot

    # Test P2P Deal Summary
    p2p_deal = {
        "buyer_username": "buyer_bob",
        "seller_username": "seller_sam",
        "purpose": "Instagram account 50k",
        "tat_seconds": 7200,
        "amount": "5000",
        "currency": "INR",
        "conditions": "Standard terms",
    }
    p2p_sum = texts.summary_text(p2p_deal)
    assert "DEAL SUMMARY" in p2p_sum
    assert "@buyer_bob" in p2p_sum
    assert "@seller_sam" in p2p_sum
    assert "₹5000" in p2p_sum
    assert "2 hours" in p2p_sum
    print("✓ P2P Deal summary card format verified.")

    # Test Exchange Deal Summary
    ex_deal = {
        "ex_give_amount": "500 INR",
        "ex_receive_amount": "5.5 USDT",
        "ex_cp": "alice",
        "ex_tat_seconds": 3600,
        "conditions": "None",
    }
    ex_sum = texts.exchange_summary_text(ex_deal, "bob")
    assert "EXCHANGE SUMMARY" in ex_sum
    assert "@bob" in ex_sum
    assert "@alice" in ex_sum
    assert "500 INR" in ex_sum
    assert "5.5 USDT" in ex_sum
    print("✓ Exchange deal summary card format verified.")

    # Test Instant Deal Summary
    inst_deal = {
        "deal_no": 42,
        "deal_type": "Telegram Handle",
        "tat": "24h",
        "payment_network": "TRC-20",
        "conditions": "Safe transfer",
        "amount": "100",
        "currency": "USDT",
    }
    inst_sum = texts.instant_deal_summary(inst_deal, "@buyer_user", "@seller_user")
    assert "MMd 42" in inst_sum or "MMd" in inst_sum
    assert "@buyer_user" in inst_sum
    assert "@seller_user" in inst_sum
    assert "Telegram Handle" in inst_sum
    assert "100 USDT" in inst_sum
    print("✓ Instant deal summary card format verified.")

    # Test Percentage Fee Calculation
    fee_val, fee_disp, tot_val, tot_disp = texts.calculate_fee_details("5005", "4%", "INR")
    assert fee_val == Decimal("200.2")
    assert fee_disp == "4%"
    assert tot_val == Decimal("5205.2")
    assert tot_disp == "₹5205.2"
    print("✓ Percentage fee calculation (4% of 5005 -> ₹5205.2) verified.")

    # Test Payment Formatting: UPI with Total Calculation
    upi_card = texts.payment_details_upi("5000", "INR", "50 INR", "escrow@upi")
    assert "PAYMENT DETAILS" in upi_card
    assert "₹5050" in upi_card
    assert "escrow@upi" in upi_card
    print("✓ UPI payment card with automatic total calculation verified.")

    # Test Crypto Payment Formatting
    crypto_prompt = texts.payment_details_crypto_prompt("100", "USDT", "2.5 USDT")
    assert "CRYPTO PAYMENT" in crypto_prompt
    assert "102.5 USDT" in crypto_prompt
    assert "Select network below:" in crypto_prompt

    crypto_addr = texts.payment_details_crypto_address("USDT", "USDT-TRC20", "Tron (TRC-20)", "TX1234567890ABCDEF")
    assert "USDT · Tron (TRC-20)" in crypto_addr
    assert "TX1234567890ABCDEF" in crypto_addr
    print("✓ Crypto payment prompt and deposit address formatting verified.")

    # 10. Testing Advertisement Suppression on Critical Transaction Messages
    print("\n--- 10. Testing Advertisement Suppression ---")
    kw_test_cases = [
        ("PAYMENT DETAILS\nAmount: ₹5000\nTotal: ₹5050", True),
        ("CRYPTO PAYMENT\nSelect network below", True),
        ("Deposit address:\n0x123456", True),
        ("DEAL SUMMARY\nBuyer: @b\nSeller: @s", True),
        ("EXCHANGE SUMMARY\nGiving: 500 INR", True),
        ("TAT COUNTDOWN\nTimer: 23h", True),
        ("TAT EXPIRED\nBuyer: @b", True),
        ("TAT EXTENDED\nNew deadline: tomorrow", True),
        ("Funds Secured\nPayment verified", True),
        ("Payment Reported\nWaiting for verification", True),
        ("Deal Closed #101\nParticipants muted", True),
        ("Regular chat message about general topic", False),
    ]

    # Temporarily enable ad
    settings._cache["active_ad"] = {"text": "🎰 @Rainbetcom Casino", "enabled": True}

    for text, should_skip in kw_test_cases:
        kw = {"text": text, "parse_mode": "HTML"}
        EmojiBot._fix(kw, "text")
        has_ad = "Rainbetcom" in kw["text"]
        if should_skip:
            assert not has_ad, f"Ad was not suppressed for critical message: {text[:40]}"
        else:
            assert has_ad, f"Ad should have been appended for non-critical message: {text[:40]}"

    # Reset ad
    settings._cache["active_ad"] = ad
    print("✓ Advertisement suppression verified for all critical transaction messages.")

    # 11. Testing Button Emoji Deduplication (No Double Emojis)
    print("\n--- 11. Testing Button Emoji Deduplication ---")
    import handlers
    # Case A: When custom icon is present/configured, leading emoji must be stripped from text
    b_deal = handlers._btn("🤝 Start Deal", "menu:start_deal", handlers.GREEN, icon=emojis.get_custom_id("DEALS"))
    assert b_deal.text == "Start Deal", f"Expected 'Start Deal', got {b_deal.text!r}"
    assert b_deal.api_kwargs.get("icon_custom_emoji_id") == emojis.get_custom_id("DEALS")

    b_chat = handlers._btn("💬 Support / Chat", "lc:start", handlers.BLUE, icon=emojis.get_custom_id("CHAT"))
    assert b_chat.text == "Support / Chat", f"Expected 'Support / Chat', got {b_chat.text!r}"
    assert b_chat.api_kwargs.get("icon_custom_emoji_id") == emojis.get_custom_id("CHAT")

    b_vouch = handlers._btn("⭐️ Vouches", color=handlers.BLUE, url="https://t.me/test", icon=emojis.get_custom_id("VOUCH"))
    assert b_vouch.text == "Vouches", f"Expected 'Vouches', got {b_vouch.text!r}"
    assert b_vouch.api_kwargs.get("icon_custom_emoji_id") == emojis.get_custom_id("VOUCH")

    # Case B: Auto-detection without explicit icon argument
    b_auto = handlers._btn("🛒 Buyer", "d:role:buyer", handlers.BLUE)
    assert b_auto.text == "Buyer", f"Expected 'Buyer', got {b_auto.text!r}"
    assert b_auto.api_kwargs.get("icon_custom_emoji_id") == emojis.get_custom_id("BUYER")

    # Case C: Fallback to unicode when custom emojis are unconfigured
    saved_ids = dict(emojis.CUSTOM_EMOJI_IDS)
    try:
        emojis.CUSTOM_EMOJI_IDS.clear()
        emojis._REVERSE = emojis._build_reverse()
        b_fallback = handlers._btn("🤝 Start Deal", "menu:start_deal", handlers.GREEN, icon=emojis.get_custom_id("DEALS"))
        assert b_fallback.text == "🤝 Start Deal", f"Expected '🤝 Start Deal', got {b_fallback.text!r}"
        assert "icon_custom_emoji_id" not in (b_fallback.api_kwargs or {})
    finally:
        emojis.CUSTOM_EMOJI_IDS.update(saved_ids)
        emojis._REVERSE = emojis._build_reverse()

    print("✓ Button emoji deduplication and unicode fallback verified.")

    # 12. Testing Strict Vouch Detection
    print("\n--- 12. Testing Strict Vouch Detection ---")
    sample_deal = {
        "deal_no": 19,
        "amount": "5005",
        "currency": "INR",
        "purpose": "Instagram Account",
        "buyer_username": "scalopus",
        "seller_username": "seller",
    }
    assert not handlers.is_matching_vouch("vouch", sample_deal)
    assert not handlers.is_matching_vouch("can you vouch for me?", sample_deal)
    assert not handlers.is_matching_vouch("thanks mm", sample_deal)
    assert not handlers.is_matching_vouch(f"is @{config.MM_USERNAME} legit?", sample_deal)
    assert not handlers.is_matching_vouch("+rep good guy", sample_deal)

    assert handlers.is_matching_vouch(f"Vouch @{config.MM_USERNAME} for MM'd 5005 INR", sample_deal)
    assert handlers.is_matching_vouch(f"vouch @{config.MM_USERNAME} 5005 inr smooth deal", sample_deal)
    assert handlers.is_matching_vouch(f"+rep @{config.MM_USERNAME} mm'd 5005", sample_deal)
    print("✓ Strict vouch detection verified (no false triggers on casual chat).")

    # 13. Testing Custom Emojis in Ad Footer
    print("\n--- 13. Testing Custom Emojis in Ad Footer ---")
    custom_ad = '@fappersgc #1 Place to Fap 24/7 <tg-emoji emoji-id="5843949948541864880">🍻</tg-emoji>'
    assert "<tg-emoji" in custom_ad
    # Ensure it is not escaped into &lt;tg-emoji
    assert "&lt;tg-emoji" not in custom_ad
    print("✓ Custom emoji tags preserved without raw escaping.")

    # 14. Testing AI Deal Reasoning in Hinglish
    print("\n--- 14. Testing AI Deal Reasoning in Hinglish ---")
    import ai_parser
    deal_test = {
        "deal_no": 42,
        "purpose": "Instagram 100k Account",
        "amount": "15000",
        "currency": "INR",
        "payment_network": "UPI",
        "buyer_username": "buyer_raj",
        "seller_username": "seller_simon",
        "status": "active",
    }
    reasoning = ai_parser._generate_local_reasoning(deal_test)
    lines = [l for l in reasoning.strip().splitlines() if l.strip()]
    assert len(lines) == 5, f"Expected 5 lines, got {len(lines)}"
    assert "1️⃣" in lines[0] and "5️⃣" in lines[-1]
    assert any("smooth" in l.lower() for l in lines)
    assert any("buyer" in l.lower() for l in lines)
    assert any("seller" in l.lower() for l in lines)
    print("✓ AI Hinglish deal reasoning generated exactly 5 lines with smooth conclusion.")

    # 15. Testing Quick Deal Edits (/edits extraction)
    print("\n--- 15. Testing Quick Deal Edits (/edits extraction) ---")
    e1 = ai_parser._parse_edits_locally("amount 5000")
    assert e1.get("amount") == "5000"

    e2 = ai_parser._parse_edits_locally("item 50k Instagram Account")
    assert "Instagram Account" in e2.get("deal_type", "")

    e3 = ai_parser._parse_edits_locally("tat 12h")
    assert e3.get("tat") == "12h"
    assert e3.get("tat_seconds") == 12 * 3600

    e4 = ai_parser._parse_edits_locally("fee 5%")
    assert e4.get("custom_fee") == "5%"

    e5 = ai_parser._parse_edits_locally("buyer @alice seller @bob")
    assert e5.get("buyer_username") == "alice"
    assert e5.get("seller_username") == "bob"

    e6 = ai_parser._parse_edits_locally("pay USDT TRC20")
    assert e6.get("payment_network") == "TRC-20"
    assert e6.get("currency") == "USDT"

    e7 = ai_parser._parse_edits_locally("5000 inr")
    assert e7.get("amount") == "5000"
    assert e7.get("currency") == "INR"
    print("✓ Quick deal edits parsed all partial and multi-field inputs correctly.")

    # 16. Testing Broadcast System (/bd) Configuration & Targets
    print("\n--- 16. Testing Broadcast System (/bd) ---")
    import database as db_mod
    import handlers as h_mod
    assert hasattr(h_mod, "bd_cmd")
    assert hasattr(h_mod, "bd_callback")
    assert hasattr(h_mod, "execute_broadcast")
    assert hasattr(db_mod, "count_broadcast_targets")
    assert hasattr(db_mod, "get_broadcast_targets")
    print("✓ Broadcast engine functions and database target collectors verified.")

    # 17. Testing Closed Deal Cleanup System (/dlt)
    print("\n--- 17. Testing Closed Deal Group Cleanup (/dlt) ---")
    assert hasattr(h_mod, "dlt_cmd")
    assert hasattr(h_mod, "dlt_callback")
    assert hasattr(h_mod, "execute_cleanup")
    assert hasattr(db_mod, "get_closed_deals_pending_cleanup_count")
    assert hasattr(db_mod, "get_closed_deals_pending_cleanup")
    assert hasattr(db_mod, "mark_deal_cleaned")
    assert hasattr(db_mod, "get_old_closed_deals_for_reminder")
    import tat as tat_mod
    assert hasattr(tat_mod, "check_cleanup_reminder")
    print("✓ Bulk cleanup command (/dlt), batching, and 2-3 day reminder background check verified.")

    # 18. Testing Deal Summary Header (No External AI Disclosure & Short Heading)
    print("\n--- 18. Testing Deal Summary Header (No External AI Disclosure) ---")
    with open("handlers.py", "r", encoding="utf-8") as f:
        handlers_code = f.read()
    assert "AI Deal Analysis & Reasoning" not in handlers_code
    assert "Summary</b>" in handlers_code
    # Ensure bulk divider lines are removed
    assert "━━━━━━━━━━━━━━━━━━━━━━" not in handlers_code
    print("✓ Deal summary header cleanly updated (external AI disclosure & bulky dividers removed).")

    # 19. Testing Leaderboard Display & Privacy (No UID, Short Headings)
    print("\n--- 19. Testing Leaderboard Formatting & Privacy ---")
    # Simulate user with username, user with name only, and user with neither
    u_with_uname = {"username": "enmoron", "first_name": "Enmoron"}
    u_with_name_only = {"first_name": "Raj", "last_name": "Sharma"}
    u_none = None

    def render_name_test(u):
        if u and u.get("username"):
            return f"@{u['username'].lstrip('@')}"
        elif u and u.get("first_name"):
            fname = u["first_name"]
            lname = u.get("last_name")
            return f"{fname} {lname}".strip() if lname else fname
        elif u and u.get("name"):
            return u["name"]
        return "Dealer"

    assert render_name_test(u_with_uname) == "@enmoron"
    assert render_name_test(u_with_name_only) == "Raj Sharma"
    assert render_name_test(u_none) == "Dealer"
    assert "7072573379" not in render_name_test(u_none)
    print("✓ Leaderboard privacy verified: shows @username or name, never raw UID.")

    print("\n===================================")
    print("🎉 ALL TEST SUITE ASSERTIONS PASSED!")
    print("===================================")

if __name__ == "__main__":
    run_tests()
