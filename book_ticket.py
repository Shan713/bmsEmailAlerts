#!/usr/bin/env python3
"""
Autonomous BookMyShow ticket booking orchestrator — CLI entry point.

Ties together ``BMSPlaywrightAutomator``, ``credential_manager``, and
``config.json`` into a one‑command booking flow::

    python book_ticket.py              # process all active requests
    python book_ticket.py --dry-run    # dry‑run (stops before payment)
    python book_ticket.py --request-id req_001   # single request only

The heavy lifting is done by :mod:`booking_engine` — this module is a thin
CLI wrapper that selects the right requests and calls
:func:`~booking_engine.execute_booking` for each.

For a web UI, see ``web_server.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from booking_engine import execute_booking, load_config

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FILE = "booking_agent.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("book_ticket")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _test_otp_relay() -> None:
    """Test the OTP email relay pipeline end‑to‑end."""
    print("=" * 60)
    print("🧪 BMS OTP Email Relay — Pipeline Test")
    print("=" * 60)

    # --- Check otp_relay module -------------------------------------------
    try:
        from otp_relay import EmailOTPRelay
        print("✅ otp_relay module imported successfully.")
    except ImportError as exc:
        print(f"❌ Failed to import otp_relay: {exc}")
        return

    # --- Load credentials -------------------------------------------------
    try:
        from credential_manager import SecureCredentialManager
        cred_mgr = SecureCredentialManager()
        creds = cred_mgr.get_credentials()
    except Exception as exc:
        print(f"❌ Failed to load credentials: {exc}")
        print("   Run: python setup_creds.py")
        return

    if not creds:
        print("❌ No credentials found.")
        print("   Run: python setup_creds.py")
        return

    otp_config = creds.get("otp_relay")
    if not otp_config or not otp_config.get("email") or not otp_config.get("app_password"):
        print("❌ OTP relay not configured in credentials.")
        print("   Run: python setup_creds.py")
        print("   Answer 'y' when asked 'Configure OTP email relay?'")
        return

    relay_email = otp_config["email"]
    relay_pw = otp_config["app_password"]
    print(f"✅ OTP relay credentials found for: {relay_email}")

    relay = EmailOTPRelay(email_addr=relay_email, app_password=relay_pw)

    # --- Test 1: SMTP (send test email) -----------------------------------
    print("\n--- Test 1: SMTP (Send Test Email) ---")
    sent = relay.send_test_email()
    if sent:
        print("✅ SMTP test passed — test email sent.")
    else:
        print("❌ SMTP test failed — could not send test email.")
        print("   Check: Is the Gmail App Password correct?")
        print("   Generate at: https://myaccount.google.com/apppasswords")
        return

    # --- Test 2: IMAP (poll for the test email) ---------------------------
    print("\n--- Test 2: IMAP (Poll for Test Email) ---")
    print(f"   Polling {relay_email} for up to 60s…")
    otp = relay.poll_for_otp(timeout=60, interval=5)
    if otp:
        print(f"✅ IMAP test passed — OTP extracted: {otp}")
    else:
        print("⚠️  IMAP test: No OTP found in inbox within 60s.")
        print("   This may be OK if the test email hasn't arrived yet.")
        print("   Check your Gmail inbox manually for the test email.")
        print("   Make sure IMAP is enabled in Gmail settings.")

    # --- Test 3: OTP extraction -------------------------------------------
    print("\n--- Test 3: OTP Extraction Patterns ---")
    test_texts = [
        ("Your OTP is 123456 for transaction", "123456"),
        ("Forwarded SMS: OTP 789012 for INR 450 at BookMyShow", "789012"),
        ("Enter the 6-digit code: 456789", "456789"),
    ]
    all_pass = True
    for text, expected in test_texts:
        result = EmailOTPRelay.extract_otp(text)
        status = "✅" if result == expected else "❌"
        if result != expected:
            all_pass = False
        print(f"   {status} '{text[:50]}…' → {result} (expected {expected})")

    if all_pass:
        print("✅ All OTP extraction patterns pass.")

    print("\n" + "=" * 60)
    print("OTP Relay Pipeline Test Complete")
    print("=" * 60)
    print()
    print("Architecture reminder:")
    print("   Bank SMS → iOS Shortcut (reads SMS) → forwards to Gmail")
    print("   AI Agent polls Gmail → extracts OTP → fills → completes booking")
    print()
    print("iOS Shortcut configuration:")
    print("   1. Trigger: When SMS received from bank shortcode")
    print("   2. Action: Send Email")
    print(f"      To: {relay_email}")
    print("      Subject: BMS OTP")
    print("      Body: [SMS Body]")

async def main() -> None:
    """Entry point — parse args, select requests, execute bookings."""
    parser = argparse.ArgumentParser(
        description="BookMyShow autonomous ticket booking agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python book_ticket.py                     # all active requests\n"
            "  python book_ticket.py --dry-run           # dry‑run only\n"
            "  python book_ticket.py --request-id req_001  # single request\n"
            "  python book_ticket.py --request-id req_001 --dry-run\n"
        ),
    )
    parser.add_argument(
        "--request-id", "-r",
        help="Process only the booking request with this ID.",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Stop before clicking 'Pay Now' (test the full pipeline).",
    )
    parser.add_argument(
        "--ai",
        action="store_true",
        help="Use AI-powered browser agent (browser-use) instead of "
        "hardcoded Playwright selectors.",
    )
    parser.add_argument(
        "--test-otp",
        action="store_true",
        help="Test the OTP email relay pipeline (IMAP + SMTP connection, "
        "test email send, and OTP extraction). No booking is attempted.",
    )
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # --test-otp: run the relay test and exit
    # ------------------------------------------------------------------
    if args.test_otp:
        await _test_otp_relay()
        return

    # ------------------------------------------------------------------
    # Load config
    # ------------------------------------------------------------------
    try:
        config: Dict[str, Any] = load_config()
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        sys.exit(1)
    except Exception as exc:
        logger.error("Failed to load config.json: %s", exc)
        sys.exit(1)

    # ------------------------------------------------------------------
    # Select booking requests
    # ------------------------------------------------------------------
    all_requests: List[Dict[str, Any]] = config.get("booking_requests", [])
    if args.request_id:
        selected = [r for r in all_requests if r.get("id") == args.request_id]
        if not selected:
            logger.error("No booking request found with id: %s", args.request_id)
            sys.exit(1)
    else:
        # All active requests with auto_book enabled.
        selected = [
            r for r in all_requests
            if r.get("auto_book", False)
            and r.get("status") in ("monitoring", "active")
        ]

    if not selected:
        logger.info("No booking requests to process. Exiting.")
        sys.exit(0)

    logger.info("Selected %d booking request(s).", len(selected))
    for r in selected:
        logger.info(
            "  [%s] %s — %s (%s)",
            r.get("id"), r.get("movie_name"), r.get("date"),
            ", ".join(r.get("cinemas", [])) or "any cinema",
        )

    if args.dry_run:
        logger.info("🔍 DRY‑RUN MODE — no real payments will be made.")

    # ------------------------------------------------------------------
    # Pre-flight: check credentials
    # ------------------------------------------------------------------
    try:
        from credential_manager import SecureCredentialManager
        cred_mgr = SecureCredentialManager()
        creds = cred_mgr.get_credentials()
        if not creds:
            logger.warning(
                "⚠️  No credentials found.  Email notifications and "
                "auto‑payment will not work."
            )
            logger.warning(
                "    Run once:  python setup_creds.py"
            )
    except Exception:
        logger.warning(
            "⚠️  Cannot load credentials.  Run:  python setup_creds.py"
        )

    # ------------------------------------------------------------------
    # Execute each request
    # ------------------------------------------------------------------
    results: List[Dict[str, Any]] = []
    for request in selected:
        req_id = request.get("id", "?")
        logger.info("🚀 Triggering booking for request %s …", req_id)

        if args.ai:
            # --- AI-powered agent ----------------------------------------
            from ai_booking_agent import AIBrowserBookingAgent

            try:
                ai_agent = AIBrowserBookingAgent(config)
            except RuntimeError as exc:
                logger.error("❌ Cannot initialize AI agent: %s", exc)
                logger.error(
                    "   Set ANTHROPIC_API_KEY or DEEPSEEK_API_KEY in "
                    "your environment."
                )
                sys.exit(1)
            result = await ai_agent.run(request, dry_run=args.dry_run)
        else:
            # --- Classic Playwright agent --------------------------------
            result = await execute_booking(req_id, dry_run=args.dry_run)

        results.append(result)

    # ------------------------------------------------------------------
    # Final summary
    # ------------------------------------------------------------------
    logger.info("=" * 60)
    logger.info("BOOKING SUMMARY")
    logger.info("=" * 60)
    for r in results:
        status = (
            "✅ CONFIRMED" if r["success"]
            else "🔍 DRY‑RUN" if r.get("dry_run")
            else "❌ FAILED"
        )
        logger.info(
            "  [%s] %s — %s — %s",
            r["request_id"], r["movie_name"], status,
            r.get("error") or r.get("booking_id") or "",
        )
    logger.info("=" * 60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(main())
