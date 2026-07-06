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
    args = parser.parse_args()

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
