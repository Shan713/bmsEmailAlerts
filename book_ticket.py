#!/usr/bin/env python3
"""
Autonomous BookMyShow ticket booking orchestrator.

Ties together ``BMSPlaywrightAutomator``, ``credential_manager``, and
``config.json`` into a one‑command booking flow::

    python book_ticket.py              # process all active requests
    python book_ticket.py --dry-run    # dry‑run (stops before payment)
    python book_ticket.py --request-id req_001   # single request only

For each booking request the script:

1. Finds matching showtimes via :meth:`~BMSPlaywrightAutomator.find_shows`.
2. Picks the best available seats via :meth:`~BMSPlaywrightAutomator.select_best_seats`.
3. Completes payment (or does a dry‑run) via :meth:`~BMSPlaywrightAutomator.complete_payment`.
4. Sends an email notification with the result.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import smtplib
import sys
from datetime import datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Local imports
from bms_playwright import (
    BMSAutomationError,
    BMSPlaywrightAutomator,
    LoginRequired,
    MissingGiftCardCredentials,
    InsufficientGiftCardBalance,
    PaymentFailed,
    PaymentPageNotFound,
    logger as bms_logger,
)
from credential_manager import SecureCredentialManager

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
# Time-range helper
# ---------------------------------------------------------------------------

def _parse_time_range(
    preferred_ranges: List[str],
    showtimes_config: Dict[str, List[str]],
) -> Tuple[int, int]:
    """
    Convert a list of time-range keys (e.g. ``["evening"]``) into a
    ``(start_hour, end_hour)`` tuple covering all matching slots.

    *showtimes_config* is ``user_profile.preferred_showtimes`` from
    ``config.json``.  Returns a wide-enough range that spans the earliest
    start hour and latest end hour of the matching ranges.
    """
    if not preferred_ranges:
        return (0, 24)  # all day

    min_hour = 24
    max_hour = 0

    for key in preferred_ranges:
        slots = showtimes_config.get(key, [])
        if not slots:
            logger.warning("Unknown time-range key: %r", key)
            continue
        for slot in slots:
            try:
                hour = int(slot.strip().split(":")[0])
                min_hour = min(min_hour, hour)
                max_hour = max(max_hour, hour)
            except (ValueError, IndexError):
                continue

    if min_hour == 24 and max_hour == 0:
        return (0, 24)

    # Extend end_hour by 1 so that e.g. 21:xx shows are included.
    max_hour = min(max_hour + 1, 24)
    return (min_hour, max_hour)


# ---------------------------------------------------------------------------
# Show sorting
# ---------------------------------------------------------------------------

def _hour_from_show(show: Dict[str, Any]) -> int:
    """Extract the 24‑hour hour from a show dict's ``show_time`` field."""
    time_str = show.get("show_time", "")
    match = re.search(r"(\d{1,2}):(\d{2})", time_str)
    if not match:
        return 99  # sort unknown times to the end
    hour = int(match.group(1))
    ampm = ""
    # Check if AM/PM is present.
    rest = time_str[match.end():].strip().upper()
    if "PM" in rest:
        ampm = "PM"
    elif "AM" in rest:
        ampm = "AM"
    # Normalise to 24-hour.
    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    return hour


def _sort_shows_by_time(shows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Sort shows by showtime hour (earliest first).  Unknown showtimes are
    placed at the end.
    """
    return sorted(shows, key=_hour_from_show)


# ---------------------------------------------------------------------------
# Email notification
# ---------------------------------------------------------------------------

def _send_notification(
    config: Dict[str, Any],
    credentials: Dict[str, Any],
    subject: str,
    body: str,
    screenshot_path: Optional[str] = None,
) -> bool:
    """
    Send an email notification using the SMTP settings in *credentials*.

    Returns ``True`` if the email was sent successfully to at least one
    recipient.
    """
    notification_cfg = config.get("notification_settings", {})
    if not notification_cfg.get("email_notifications", False):
        logger.info("Email notifications are disabled — skipping.")
        return False

    recipients: List[str] = notification_cfg.get("notification_recipients", [])
    if not recipients:
        logger.warning("No notification recipients configured.")
        return False

    sender_email = config.get("user_profile", {}).get("email", "")
    app_password = credentials.get("email_app_password", "")

    if not sender_email or not app_password:
        logger.error("Missing sender email or email_app_password — cannot send email.")
        return False

    sent_any = False
    for recipient in recipients:
        try:
            msg = MIMEMultipart()
            msg["Subject"] = subject
            msg["From"] = sender_email
            msg["To"] = recipient
            msg.attach(MIMEText(body, "plain"))

            # Attach screenshot if provided and exists.
            if screenshot_path:
                try:
                    path = Path(screenshot_path)
                    if path.exists():
                        with open(path, "rb") as fh:
                            img = MIMEImage(fh.read(), _subtype="png")
                            img.add_header(
                                "Content-Disposition",
                                "attachment",
                                filename=path.name,
                            )
                            msg.attach(img)
                        logger.info("Attached screenshot: %s", path.name)
                except Exception as exc:
                    logger.warning("Could not attach screenshot: %s", exc)

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
                smtp.login(sender_email, app_password)
                smtp.send_message(msg)
                logger.info("✅ Email sent to %s", recipient)
                sent_any = True
        except Exception as exc:
            logger.error("Failed to send email to %s: %s", recipient, exc)

    return sent_any


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

async def _process_request(
    automator: BMSPlaywrightAutomator,
    page: Any,
    request: Dict[str, Any],
    config: Dict[str, Any],
    dry_run: bool,
) -> Dict[str, Any]:
    """
    Process a single booking request: find shows → pick seats → pay.

    Returns a result dict::

        {
            "request_id": str,
            "movie_name": str,
            "success": bool,
            "booking_id": str | None,
            "cinema": str | None,
            "show_time": str | None,
            "seats": str | None,
            "total_paid": str | None,
            "screenshot": str | None,
            "error": str | None,
            "dry_run": bool,
        }
    """
    req_id: str = request.get("id", "?")
    movie_name: str = request.get("movie_name", "")
    date: str = request.get("date", "")
    preferred_ranges: List[str] = request.get("preferred_time_range", [])
    cinemas: List[str] = request.get("cinemas", [])
    booking_url: Optional[str] = request.get("booking_url") or None
    num_tickets: int = config.get("user_profile", {}).get("max_tickets", 2)

    logger.info("=" * 60)
    logger.info("[req:%s] Processing: %s on %s", req_id, movie_name, date)
    logger.info("[req:%s] Cinemas: %s  Tickets: %d  Dry‑run: %s",
                req_id, cinemas or "any", num_tickets, dry_run)
    logger.info("=" * 60)

    # --- Parse time range -------------------------------------------------
    showtimes_config = config.get("user_profile", {}).get("preferred_showtimes", {})
    time_range = _parse_time_range(preferred_ranges, showtimes_config)
    logger.info("[req:%s] Time window: %02d:00–%02d:00", req_id, *time_range)

    # --- Find shows -------------------------------------------------------
    try:
        shows = await automator.find_shows(
            page,
            movie_name,
            date,
            time_range,
            booking_url=booking_url,
            cinema_filter=cinemas if cinemas else None,
        )
    except Exception as exc:
        logger.error("[req:%s] find_shows failed: %s", req_id, exc)
        return {
            "request_id": req_id,
            "movie_name": movie_name,
            "success": False,
            "booking_id": None,
            "cinema": None,
            "show_time": None,
            "seats": None,
            "total_paid": None,
            "screenshot": None,
            "error": str(exc),
            "dry_run": dry_run,
        }

    if not shows:
        error_msg = f"No shows found for {movie_name} on {date}"
        logger.warning("[req:%s] %s", req_id, error_msg)
        return {
            "request_id": req_id,
            "movie_name": movie_name,
            "success": False,
            "booking_id": None,
            "cinema": None,
            "show_time": None,
            "seats": None,
            "total_paid": None,
            "screenshot": None,
            "error": error_msg,
            "dry_run": dry_run,
        }

    logger.info("[req:%s] Found %d show(s).", req_id, len(shows))
    for s in shows:
        logger.info(
            "[req:%s]   %s | %s | ₹%s",
            req_id, s.get("venue", "?"), s.get("show_time", "?"),
            s.get("price") or "?",
        )

    # --- Sort shows by time -----------------------------------------------
    shows = _sort_shows_by_time(shows)

    # --- Try each show ----------------------------------------------------
    results: List[Dict[str, Any]] = []
    for idx, show in enumerate(shows):
        venue = show.get("venue", "?")
        show_time = show.get("show_time", "?")
        logger.info(
            "[req:%s] Attempt %d/%d: %s at %s",
            req_id, idx + 1, len(shows), venue, show_time,
        )

        # -- Select seats --
        try:
            seats_result = await automator.select_best_seats(
                page, show, num_tickets=num_tickets,
            )
        except Exception as exc:
            logger.warning(
                "[req:%s] select_best_seats exception: %s", req_id, exc,
            )
            continue

        if not seats_result.get("attempted"):
            err = seats_result.get("error", "Unknown seat selection error")
            logger.warning("[req:%s] Seat selection failed: %s", req_id, err)
            continue

        selected = seats_result.get("selected_seats", [])
        total_price = seats_result.get("total_price")
        logger.info(
            "[req:%s] ✅ Seats selected: %s  (₹%s)",
            req_id, selected, total_price,
        )

        # -- Complete payment --
        try:
            payment_result = await automator.complete_payment(
                page,
                expected_amount=total_price,
                dry_run=dry_run,
            )
        except InsufficientGiftCardBalance as exc:
            logger.error(
                "[req:%s] ❌ Insufficient balance: ₹%.2f < ₹%.2f",
                req_id, exc.balance, exc.required,
            )
            return {
                "request_id": req_id,
                "movie_name": movie_name,
                "success": False,
                "booking_id": None,
                "cinema": venue,
                "show_time": show_time,
                "seats": ", ".join(selected) if selected else None,
                "total_paid": f"₹{exc.required:.2f}",
                "screenshot": None,
                "error": str(exc),
                "dry_run": dry_run,
            }
        except (PaymentFailed, PaymentPageNotFound, MissingGiftCardCredentials) as exc:
            logger.error("[req:%s] Payment error: %s", req_id, exc)
            continue  # try next show
        except Exception as exc:
            logger.error("[req:%s] Payment exception: %s", req_id, exc)
            continue

        if dry_run:
            logger.info("[req:%s] 🔍 Dry‑run complete — would have paid ₹%s for %s",
                        req_id, total_price or "?", selected)
            return {
                "request_id": req_id,
                "movie_name": movie_name,
                "success": False,
                "booking_id": None,
                "cinema": venue,
                "show_time": show_time,
                "seats": ", ".join(selected) if selected else None,
                "total_paid": (
                    f"₹{total_price:.2f}" if total_price else None
                ),
                "screenshot": payment_result.get("screenshot"),
                "error": None,
                "dry_run": True,
            }

        if payment_result.get("success"):
            logger.info(
                "[req:%s] 🎉 BOOKING CONFIRMED — ID: %s",
                req_id, payment_result.get("booking_id"),
            )
            return {
                "request_id": req_id,
                "movie_name": movie_name,
                "success": True,
                "booking_id": payment_result.get("booking_id"),
                "cinema": payment_result.get("cinema") or venue,
                "show_time": payment_result.get("show_time") or show_time,
                "seats": payment_result.get("seats") or ", ".join(selected),
                "total_paid": payment_result.get("total_paid"),
                "screenshot": payment_result.get("screenshot"),
                "error": None,
                "dry_run": False,
            }
        else:
            logger.warning("[req:%s] Payment returned success=False — trying next show.", req_id)
            continue

    # ---- All shows exhausted -----------------------------------------------
    error_msg = f"All {len(shows)} shows exhausted — booking failed."
    logger.error("[req:%s] %s", req_id, error_msg)
    return {
        "request_id": req_id,
        "movie_name": movie_name,
        "success": False,
        "booking_id": None,
        "cinema": None,
        "show_time": None,
        "seats": None,
        "total_paid": None,
        "screenshot": None,
        "error": error_msg,
        "dry_run": dry_run,
    }


async def main() -> None:
    """Entry point — parse args, init automator, process requests."""
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
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # Load config
    # ------------------------------------------------------------------
    config_path = Path(__file__).resolve().parent / "config.json"
    if not config_path.exists():
        logger.error("config.json not found at %s", config_path)
        sys.exit(1)

    config: Dict[str, Any] = {}
    try:
        with open(config_path, "r") as fh:
            config = json.load(fh)
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
    # Load credentials (for email notifications)
    # ------------------------------------------------------------------
    try:
        cred_mgr = SecureCredentialManager()
        credentials = cred_mgr.get_credentials()
    except Exception as exc:
        logger.warning("Could not load credentials: %s — notifications disabled.", exc)
        credentials = {}

    # ------------------------------------------------------------------
    # Initialize automator
    # ------------------------------------------------------------------
    automator = BMSPlaywrightAutomator(config)
    browser = None
    context = None
    results: List[Dict[str, Any]] = []

    try:
        logger.info("Launching browser …")
        browser, context, page = await automator.start()
        logger.info("✅ Browser ready.")

        # Login.
        try:
            logged_in = await automator.login(page)
            if not logged_in:
                logger.warning("Login did not confirm — continuing anyway.")
        except LoginRequired as exc:
            logger.error("🔐 %s", exc)
            logger.error(
                "Run once in headed mode to complete login, then re‑run this script."
            )
            sys.exit(1)

        # ------------------------------------------------------------------
        # Process each request
        # ------------------------------------------------------------------
        for request in selected:
            result = await _process_request(
                automator, page, request, config, dry_run=args.dry_run,
            )
            results.append(result)

            # --- Send notification -----------------------------------------
            req_id = result["request_id"]
            movie = result["movie_name"]

            if result["success"]:
                subject = f"🎉 Booking Confirmed: {movie}"
                body_lines = [
                    f"Movie:     {movie}",
                    f"Cinema:    {result.get('cinema') or 'N/A'}",
                    f"Showtime:  {result.get('show_time') or 'N/A'}",
                    f"Seats:     {result.get('seats') or 'N/A'}",
                    f"Amount:    {result.get('total_paid') or 'N/A'}",
                    f"Booking:   {result.get('booking_id') or 'N/A'}",
                    f"Time:      {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                ]
                body = "\n".join(body_lines)
                _send_notification(
                    config, credentials, subject, body,
                    screenshot_path=result.get("screenshot"),
                )

            elif result.get("dry_run"):
                subject = f"🔍 Dry‑Run Complete: {movie}"
                body_lines = [
                    f"Movie:     {movie}",
                    f"Cinema:    {result.get('cinema') or 'N/A'}",
                    f"Showtime:  {result.get('show_time') or 'N/A'}",
                    f"Seats:     {result.get('seats') or 'N/A'}",
                    f"Would pay: {result.get('total_paid') or 'N/A'}",
                    f"Time:      {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    "",
                    "No real payment was made — dry‑run mode.",
                ]
                body = "\n".join(body_lines)
                _send_notification(
                    config, credentials, subject, body,
                    screenshot_path=result.get("screenshot"),
                )

            else:
                subject = f"❌ Booking Failed: {movie}"
                body_lines = [
                    f"Movie:     {movie}",
                    f"Date:      {request.get('date', '?')}",
                    f"Error:     {result.get('error') or 'Unknown'}",
                    f"Time:      {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                ]
                body = "\n".join(body_lines)
                _send_notification(config, credentials, subject, body)

    except BMSAutomationError as exc:
        logger.error("Automation error: %s", exc)
        sys.exit(1)
    except Exception as exc:
        logger.exception("Unexpected error during booking.")
        sys.exit(1)
    finally:
        # ------------------------------------------------------------------
        # Cleanup
        # ------------------------------------------------------------------
        if browser:
            try:
                await browser.close()
                logger.info("🛑 Browser closed.")
            except Exception as exc:
                logger.warning("Error closing browser: %s", exc)

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
