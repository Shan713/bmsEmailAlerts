#!/usr/bin/env python3
"""
Core booking engine shared by the CLI (``book_ticket.py``) and the web
dashboard (``web_server.py``).

Provides:

* Config helpers: :func:`load_config`, :func:`save_config`
* Time-range parsing & show sorting
* Email notification
* :func:`_process_request` — the per‑request orchestrator
* :func:`execute_booking` — full lifecycle for a single request (browser,
  process, notify, cleanup)
"""

from __future__ import annotations

import json
import logging
import os
import re
import smtplib
from datetime import datetime
from difflib import SequenceMatcher
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bms_playwright import (
    BMSAutomationError,
    BMSPlaywrightAutomator,
    InsufficientGiftCardBalance,
    MissingGiftCardCredentials,
    MissingUPIID,
    PaymentFailed,
    PaymentMethodNotFound,
    PaymentPageNotFound,
    PaymentTimeout,
)
from credential_manager import SecureCredentialManager

logger = logging.getLogger("booking_engine")


def _normalize_cinema_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(name).lower()).strip()


def _cinema_names_match(requested: str, scraped: str) -> bool:
    requested_norm = _normalize_cinema_name(requested)
    scraped_norm = _normalize_cinema_name(scraped)

    if not requested_norm or not scraped_norm:
        return False

    if requested_norm in scraped_norm or scraped_norm in requested_norm:
        return True

    requested_tokens = [token for token in requested_norm.split() if len(token) > 2]
    scraped_tokens = [token for token in scraped_norm.split() if len(token) > 2]

    if requested_tokens and all(token in scraped_norm for token in requested_tokens):
        return True

    chain_tokens = {"pvr", "inox", "cinepolis", "miraj", "broadway", "kg"}
    if any(token in chain_tokens for token in requested_tokens) and any(token in chain_tokens for token in scraped_tokens):
        requested_tail = " ".join(token for token in requested_tokens if token not in chain_tokens)
        scraped_tail = " ".join(token for token in scraped_tokens if token not in chain_tokens)
        if requested_tail and scraped_tail:
            if SequenceMatcher(None, requested_tail, scraped_tail).ratio() >= 0.55:
                return True

    return SequenceMatcher(None, requested_norm, scraped_norm).ratio() >= 0.72

# ---------------------------------------------------------------------------
# Config paths
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config() -> Dict[str, Any]:
    """Load ``config.json`` and return its contents."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"config.json not found at {CONFIG_PATH}")
    with open(CONFIG_PATH, "r") as fh:
        return json.load(fh)


def save_config(config: Dict[str, Any]) -> None:
    """Atomically write *config* to ``config.json``."""
    tmp = str(CONFIG_PATH) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(config, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, str(CONFIG_PATH))


# ---------------------------------------------------------------------------
# Time-range parsing
# ---------------------------------------------------------------------------

def parse_time_range(
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


def sort_shows_by_time(shows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Sort shows by showtime hour (earliest first).  Unknown showtimes are
    placed at the end.
    """
    return sorted(shows, key=_hour_from_show)


# ---------------------------------------------------------------------------
# Email notification
# ---------------------------------------------------------------------------

def send_notification(
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
        except smtplib.SMTPAuthenticationError:
            logger.error(
                "❌ Gmail authentication failed for %s. "
                "This usually means the stored password is your regular "
                "Gmail password, not a Gmail App Password.\n"
                "Fix:\n"
                "  1. Go to https://myaccount.google.com/apppasswords\n"
                "  2. Generate an App Password for 'Mail'\n"
                "  3. Run:  python setup_creds.py\n"
                "  4. Enter the 16-character App Password (not your regular "
                "Gmail password) when prompted for the 'Email App Password'.",
                sender_email,
            )
        except smtplib.SMTPException as exc:
            logger.error(
                "Failed to send email to %s: SMTP error — %s\n"
                "If the error is about authentication, ensure you are using "
                "a Gmail App Password, not your regular Gmail password.\n"
                "Generate one at: https://myaccount.google.com/apppasswords",
                recipient, exc,
            )
        except Exception as exc:
            logger.error("Failed to send email to %s: %s", recipient, exc)

    return sent_any


# ---------------------------------------------------------------------------
# Per-request orchestrator
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
    payment_method: str = request.get("payment_method", "upi") or "upi"

    logger.info("=" * 60)
    logger.info("[req:%s] Processing: %s on %s", req_id, movie_name, date)
    logger.info("[req:%s] Cinemas: %s  Tickets: %d  Dry‑run: %s  Payment: %s",
                req_id, cinemas or "any", num_tickets, dry_run, payment_method)
    logger.info("=" * 60)

    # --- Parse time range -------------------------------------------------
    showtimes_config = config.get("user_profile", {}).get("preferred_showtimes", {})
    time_range = parse_time_range(preferred_ranges, showtimes_config)
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
    shows = sort_shows_by_time(shows)

    # --- Filter by requested cinemas before attempting seats --------------
    if cinemas:
        requested = [c.strip().lower() for c in cinemas if c and c.strip()]

        def _matches_requested_cinema(show: Dict[str, Any]) -> bool:
            venue = str(show.get("venue") or show.get("cinema") or "").strip()
            return any(_cinema_names_match(name, venue) for name in requested)

        # Log which shows match and which are skipped.
        matching_shows = []
        skipped_cinemas: set[str] = set()
        for show in shows:
            venue = str(show.get("venue") or show.get("cinema") or "")
            if _matches_requested_cinema(show):
                matching_shows.append(show)
            else:
                skipped_cinemas.add(venue)

        if skipped_cinemas:
            for cinema in sorted(skipped_cinemas):
                logger.info(
                    '[req:%s] Skipping "%s" — not in preferred cinemas %s',
                    req_id, cinema, cinemas,
                )

        if matching_shows:
            logger.info(
                "[req:%s] After cinema filter: %d show(s) matching %s (from %d total)",
                req_id, len(matching_shows), cinemas, len(shows),
            )
            shows = matching_shows
        else:
            # No fallback — if no shows match the configured cinemas, we fail early.
            error_msg = (
                f"No shows matched requested cinemas {cinemas}. "
                f"Found {len(shows)} show(s) at: "
                + ", ".join(sorted(skipped_cinemas)[:5])
            )
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

    # --- Try each show ----------------------------------------------------
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
                page,
                show,
                num_tickets=num_tickets,
                time_range=time_range,
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

        # -- Complete payment (route by payment method) --
        try:
            if payment_method == "upi":
                payment_result = await automator.complete_payment_upi(
                    page,
                    expected_amount=total_price,
                    dry_run=dry_run,
                )
            else:  # gift_card (dormant but preserved)
                payment_result = await automator.complete_payment(
                    page,
                    expected_amount=total_price,
                    dry_run=dry_run,
                )
        except MissingUPIID as exc:
            logger.error("[req:%s] ❌ UPI ID not set: %s", req_id, exc)
            return {
                "request_id": req_id,
                "movie_name": movie_name,
                "success": False,
                "booking_id": None,
                "cinema": venue,
                "show_time": show_time,
                "seats": ", ".join(selected) if selected else None,
                "total_paid": None,
                "screenshot": None,
                "error": str(exc),
                "dry_run": dry_run,
            }
        except PaymentTimeout as exc:
            logger.error("[req:%s] ❌ UPI payment timed out: %s", req_id, exc)
            continue  # try next show
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
        except (PaymentFailed, PaymentPageNotFound,
                MissingGiftCardCredentials, PaymentMethodNotFound) as exc:
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


# ---------------------------------------------------------------------------
# Full booking lifecycle (single request)
# ---------------------------------------------------------------------------

async def execute_booking(
    request_id: str,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Execute a full booking lifecycle for a single request.

    Opens a browser, logs in, finds shows, selects seats, completes payment
    (or dry‑runs), sends email notification, and returns the result dict.

    This function is self‑contained — it opens and closes its own browser,
    making it safe for both CLI and web‑server use.
    """
    config = load_config()

    # Find the request
    all_requests: List[Dict[str, Any]] = config.get("booking_requests", [])
    request = next((r for r in all_requests if r.get("id") == request_id), None)
    if not request:
        return {
            "request_id": request_id,
            "movie_name": "?",
            "success": False,
            "booking_id": None,
            "cinema": None,
            "show_time": None,
            "seats": None,
            "total_paid": None,
            "screenshot": None,
            "error": f"Request '{request_id}' not found in config.",
            "dry_run": dry_run,
        }

    # Load credentials for notifications & payment
    credentials: Dict[str, Any] = {}
    try:
        cred_mgr = SecureCredentialManager()
        credentials = cred_mgr.get_credentials() or {}
    except Exception as exc:
        logger.warning("Could not load credentials: %s", exc)

    if not credentials:
        logger.warning(
            "⚠️  No credentials found — email notifications and auto‑payment "
            "will not work. Run:  python setup_creds.py"
        )

    # Initialize automator
    automator = BMSPlaywrightAutomator(config)
    browser = None

    try:
        logger.info("Launching browser for request %s …", request_id)
        browser, context, page = await automator.start()
        logger.info("✅ Browser ready for request %s.", request_id)

        # No login needed — BMS asks for email/phone at the payment stage.
        # Email/phone are filled automatically in complete_payment() from
        # stored credentials (credentials['user_details']).

        # Process the request
        result = await _process_request(
            automator, page, request, config, dry_run=dry_run,
        )

        # --- Send notification -----------------------------------------
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
            if credentials:
                send_notification(
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
            if credentials:
                send_notification(
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
            if credentials:
                send_notification(config, credentials, subject, body)

        return result

    except BMSAutomationError as exc:
        logger.error("Automation error for request %s: %s", request_id, exc)
        return {
            "request_id": request_id,
            "movie_name": request.get("movie_name", "?"),
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
    except Exception as exc:
        logger.exception("Unexpected error during booking for request %s.", request_id)
        return {
            "request_id": request_id,
            "movie_name": request.get("movie_name", "?"),
            "success": False,
            "booking_id": None,
            "cinema": None,
            "show_time": None,
            "seats": None,
            "total_paid": None,
            "screenshot": None,
            "error": f"Unexpected error: {exc}",
            "dry_run": dry_run,
        }
    finally:
        if browser:
            try:
                await browser.close()
                logger.info("🛑 Browser closed for request %s.", request_id)
            except Exception as exc:
                logger.warning("Error closing browser: %s", exc)
