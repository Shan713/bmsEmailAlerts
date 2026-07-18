#!/usr/bin/env python3
"""
Per-request booking monitor that runs as an async task inside the web server.

Each ``RequestWatcher`` polls a single booking request's BMS page at a
configurable interval.  When it detects that booking is open (target cinema
appears on the page), it:

1. Sends a "Booking is OPEN!" email notification to all configured recipients
2. Triggers the AI booking agent to attempt the booking

These two steps are independent — the email always fires even if the AI
booking fails, so the user is never left in the dark about a newly-open
booking.

Usage
-----
    manager = WatcherManager()
    manager.start()
    manager.add_watcher(request_data)   # starts polling in background
    manager.remove_watcher("req_001")   # stops polling
    status = manager.get_all_status()   # list of watcher states
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Load environment variables from .env (API keys, etc.)
from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger("monitor_manager")


# ---------------------------------------------------------------------------
# Helpers (adapted from watch.py)
# ---------------------------------------------------------------------------

def build_booking_url(movie_url: str, date: str) -> Optional[str]:
    """
    Construct a ``/buytickets/...`` URL from a movie-page URL + date.
    """
    if "/buytickets/" in movie_url:
        return movie_url

    match = re.search(r"/movies/([^/]+)/([^/]+)/(ET\d+)", movie_url)
    if not match:
        return None

    city, slug, et_code = match.groups()
    date_fmt = date.replace("-", "")
    return (
        f"https://in.bookmyshow.com/movies/{city}/{slug}"
        f"/buytickets/{et_code}/{date_fmt}"
    )


def check_booking_page_sync(browser_url: str, target_cinemas: List[str]) -> Dict[str, Any]:
    """
    Synchronous version of the booking-page checker.

    Uses JavaScript evaluation to reliably detect whether shows are
    available — checks for showtime buttons/links rather than raw text,
    avoiding false positives from footer/nav mentions of cinema names.
    """
    result: Dict[str, Any] = {
        "available": False,
        "found_cinemas": [],
        "screenshot_path": None,
    }

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()

            try:
                page.goto(browser_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(4000)

                # Screenshot for debugging
                screenshot_dir = Path("screenshots")
                screenshot_dir.mkdir(exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                screenshot_path = str(screenshot_dir / f"mon_{ts}.png")
                page.screenshot(path=screenshot_path)
                result["screenshot_path"] = screenshot_path

                page_text = page.text_content("body") or ""
                page_text_lower = page_text.lower()

                # ── 1. Check "not open" keywords ─────────────────────
                not_open_keywords = [
                    "coming soon", "no shows available", "showtimes not available",
                    "tickets coming soon", "bookings will open soon",
                    "currently no shows", "no showtimes", "uh-oh!",
                    "we couldn't find anything",
                ]
                if any(kw in page_text_lower for kw in not_open_keywords):
                    logger.info("  Bookings not open yet ('coming soon' etc.).")
                    return result

                # ── 2. Use JS to find showtime buttons on the page ───
                # On BMS buytickets page, each available showtime is an
                # <a> or <button> containing a time pattern like "10:00 AM".
                showtime_elements = page.evaluate("""
                    () => {
                        // Find all clickable elements with time-like text
                        const timePattern = /\\d{1,2}:\\d{2}\\s*(?:AM|PM|am|pm)/;
                        const elements = document.querySelectorAll('a, button, span, div');
                        const matches = [];
                        for (const el of elements) {
                            const text = el.textContent.trim();
                            if (timePattern.test(text) && el.offsetParent !== null) {
                                // Get nearby venue name (parent container text)
                                const parent = el.closest('div, section, li') || el;
                                const parentText = parent.textContent.trim();
                                matches.push({
                                    time: text.match(timePattern)[0],
                                    parentText: parentText.slice(0, 200),
                                });
                            }
                        }
                        return matches;
                    }
                """)

                # ── 3. Match showtimes to target cinemas ─────────────
                if showtime_elements and len(showtime_elements) > 0:
                    logger.info(
                        "  Found %d showtime element(s) on page",
                        len(showtime_elements),
                    )
                    for st in showtime_elements[:10]:
                        logger.info("    Time: %s", st.get("time", "?"))

                    # Check if any of our target cinemas appear near showtimes
                    for cinema in target_cinemas:
                        cinema_lower = cinema.lower()
                        for st in showtime_elements:
                            parent = st.get("parentText", "").lower()
                            if cinema_lower in parent:
                                result["found_cinemas"].append(cinema)
                                result["available"] = True
                                break

                    # If we found showtimes but none matched our cinemas,
                    # still mark available so the AI can find any cinema
                    if not result["available"] and target_cinemas:
                        # Check if cinema name exists anywhere near showtimes
                        all_showtime_text = " ".join(
                            s.get("parentText", "") for s in showtime_elements
                        ).lower()
                        for cinema in target_cinemas:
                            if cinema.lower() in all_showtime_text:
                                result["found_cinemas"].append(cinema)
                                result["available"] = True

                    if result["available"]:
                        logger.info(
                            "  Target cinemas FOUND: %s",
                            result["found_cinemas"],
                        )
                    else:
                        # found showtimes but no target cinemas
                        cinema_list = ", ".join(target_cinemas)
                        logger.info(
                            "  Showtimes exist but no cinema matched %s. "
                            "Showing all available venues.",
                            cinema_list,
                        )
                        result["available"] = True  # still trigger for AI
                else:
                    # No showtime elements found = not open yet
                    logger.info("  No showtime elements found on page.")
                    # Log nearby venue text for debugging
                    venue_lines = [
                        l.strip() for l in page_text.split("\n") if l.strip()
                        and any(kw in l.lower() for kw in [
                            "pvr", "inox", "cinema", "broadway", "cinepolis",
                            "imax", "screen", "theatre", "multiplex", "miraj",
                        ])
                    ]
                    if venue_lines:
                        logger.info(
                            "  Venue text on page (no showtimes): %s",
                            venue_lines[:10],
                        )

                return result

            except Exception as exc:
                logger.error("Error checking page: %s", exc)
                return result
            finally:
                context.close()
                browser.close()

    except Exception as exc:
        logger.error("Playwright error in check_booking_page_sync: %s", exc)
        return result


# ---------------------------------------------------------------------------
# Per-request watcher
# ---------------------------------------------------------------------------

class RequestWatcher:
    """
    Watch a single booking request and act when booking opens.

    Uses Playwright's sync API running in a thread executor to avoid
    Windows asyncio subprocess incompatibilities.

    Parameters
    ----------
    request_data : dict
        From ``config.json`` — must have ``id``, ``movie_name``, ``date``,
        ``cinemas``, ``movie_url``.
    interval : int
        Seconds between polls (default 60).
    """

    def __init__(
        self,
        request_data: Dict[str, Any],
        interval: int = 60,
    ) -> None:
        self.request = request_data
        self.interval = interval
        self._task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stopped = False

        # Status tracking
        self.last_check: Optional[str] = None
        self.last_result: Optional[str] = None  # "waiting" | "open" | "error"
        self.error: Optional[str] = None
        self.notified = False  # prevent duplicate emails

        self._booking_url: Optional[str] = None

    # ── Public API ──────────────────────────────────────────────────

    @property
    def request_id(self) -> str:
        return self.request.get("id", "???")

    @property
    def movie_name(self) -> str:
        return self.request.get("movie_name", "???")

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> Dict[str, Any]:
        """Return a snapshot of this watcher's state."""
        return {
            "request_id": self.request_id,
            "movie_name": self.movie_name,
            "is_running": self.is_running,
            "last_check": self.last_check,
            "last_result": self.last_result,
            "notified": self.notified,
            "error": self.error,
            "interval": self.interval,
            "booking_url": self._booking_url,
        }

    def start(self) -> None:
        """Begin the async polling loop."""
        if self.is_running:
            logger.warning("[%s] Watcher already running.", self.request_id)
            return

        # Pre-compute the booking URL
        movie_url = self.request.get("movie_url") or self.request.get("booking_url")
        date = self.request.get("date", "")
        if movie_url:
            self._booking_url = build_booking_url(movie_url, date)
            if not self._booking_url:
                logger.warning(
                    "[%s] Could not build booking URL from: %s",
                    self.request_id, movie_url,
                )

        self._stopped = False
        self._loop = asyncio.get_event_loop()
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "[%s] Watcher started (interval=%ds, url=%s)",
            self.request_id, self.interval, self._booking_url or "N/A",
        )

    async def stop(self) -> None:
        """Cancel the polling loop."""
        self._stopped = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("[%s] Watcher stopped.", self.request_id)

    # ── Internal polling loop ───────────────────────────────────────

    async def _poll_loop(self) -> None:
        """
        Continuously check the booking page at *interval* seconds.

        Runs the actual Playwright call in a thread executor using the
        sync API, which avoids Windows subprocess issues.
        """
        loop = self._loop or asyncio.get_event_loop()

        try:
            while not self._stopped:
                await self._check_once(loop)
                if self._stopped:
                    break
                await asyncio.sleep(self.interval)

        except asyncio.CancelledError:
            logger.info("[%s] Watcher task cancelled.", self.request_id)
        except Exception as exc:
            logger.error("[%s] Watcher crashed: %s", self.request_id, exc)
            self.error = str(exc)
            self.last_result = "error"

    async def _check_once(self, loop: asyncio.AbstractEventLoop) -> None:
        """Single check cycle — run Playwright check in thread, then act."""
        rid = self.request_id
        url = self._booking_url
        if not url:
            self.last_result = "error"
            self.error = "No booking URL available"
            return

        self.last_check = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Run the synchronous Playwright check in a thread executor.
        # This avoids Windows subprocess issues inside asyncio.
        try:
            check = await loop.run_in_executor(
                None,
                check_booking_page_sync,
                url,
                self.request.get("cinemas", []),
            )

            if check["available"]:
                logger.info(
                    "BOOKINGS ARE LIVE! Cinemas: %s",
                    check["found_cinemas"],
                )
                self.last_result = "open"

                if not self.notified:
                    self.notified = True
                    await self._on_booking_open(check)

            else:
                logger.debug("[%s] Not available yet.", rid)
                self.last_result = "waiting"

        except Exception as exc:
            logger.error("[%s] Check error: %s", rid, exc)
            self.last_result = "error"
            self.error = str(exc)

    async def _on_booking_open(self, check: Dict[str, Any]) -> None:
        """
        Called when booking is detected as open.

        Sends email notification FIRST, then attempts AI booking.
        The email always fires regardless of whether booking succeeds.
        """
        rid = self.request_id
        movie = self.movie_name
        logger.info("[%s] Booking detected — sending notification + AI trigger", rid)

        # ── 1. Send email notification ──────────────────────────────
        try:
            from booking_engine import load_config, send_notification
            from credential_manager import SecureCredentialManager

            config = load_config()
            cred_mgr = SecureCredentialManager()
            credentials = cred_mgr.get_credentials() or {}

            subject = "BOOKING IS OPEN: " + movie + "!"
            body_lines = [
                "Movie:     " + movie,
                "Request:   " + rid,
                "Cinemas:   " + ", ".join(check.get("found_cinemas", [])),
                "Date:      " + self.request.get("date", "?"),
                "URL:       " + (self._booking_url or "N/A"),
                "Detected:  " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "",
                "The AI booking agent will now attempt to book tickets.",
                "You'll receive a separate confirmation email on success/failure.",
            ]

            notif_cfg = config.setdefault("notification_settings", {})
            was_enabled = notif_cfg.get("email_notifications", False)
            notif_cfg["email_notifications"] = True

            send_success = send_notification(
                config, credentials, subject, "\n".join(body_lines)
            )
            notif_cfg["email_notifications"] = was_enabled

            if send_success:
                logger.info("[%s] Booking-open email sent.", rid)
            else:
                logger.warning(
                    "[%s] Email notification not sent "
                    "(check notification settings / credentials)",
                    rid,
                )
        except Exception as exc:
            logger.error("[%s] Failed to send notification: %s", rid, exc)

        # ── 2. Trigger AI booking ───────────────────────────────────
        try:
            from ai_booking_agent import AIBrowserBookingAgent
            from booking_engine import load_config, save_config

            fresh_config = load_config()
            fresh_req = next(
                (r for r in fresh_config.get("booking_requests", [])
                 if r.get("id") == rid),
                None,
            )
            if not fresh_req:
                logger.warning("[%s] Request not found in config — can't AI book.", rid)
                return

            # Build time window from preferred time ranges
            time_ranges = fresh_req.get("preferred_time_range", [])
            showtimes = (
                fresh_config.get("user_profile", {})
                .get("preferred_showtimes", {})
            )
            hours = []
            for key in time_ranges:
                for slot in showtimes.get(key, []):
                    try:
                        hours.append(int(slot.strip().split(":")[0]))
                    except (ValueError, IndexError):
                        pass
            time_window = (min(hours), max(hours) + 1) if hours else None

            agent = AIBrowserBookingAgent(fresh_config)
            logger.info("[%s] Launching AI booking agent...", rid)
            result = await agent.execute_booking(
                request_id=rid,
                booking_url=self._booking_url or "",
                movie_name=movie,
                cinema=(
                    fresh_req.get("cinemas", [""])[0]
                    if fresh_req.get("cinemas") else ""
                ),
                date=fresh_req.get("date", ""),
                city=fresh_req.get("city", "Coimbatore"),
                time_window=time_window,
                num_tickets=(
                    fresh_config.get("user_profile", {})
                    .get("max_tickets", 2)
                ),
                dry_run=False,
            )

            if result and result.get("success"):
                logger.info("[%s] AI BOOKING SUCCESSFUL!", rid)
                fresh_req["status"] = "booked"
            else:
                err = result.get("error", "unknown") if result else "no result"
                logger.error("[%s] AI booking failed: %s", rid, err)
                fresh_req["status"] = "booking_failed"

            save_config(fresh_config)

        except ImportError:
            logger.warning(
                "[%s] AI booking agent not available — skipping auto-book.",
                rid,
            )
        except Exception as exc:
            logger.error("[%s] AI booking exception: %s", rid, exc)


# ---------------------------------------------------------------------------
# Watcher manager
# ---------------------------------------------------------------------------

class WatcherManager:
    """
    Manages a collection of ``RequestWatcher`` instances.

    Provides start/stop lifecycle for per-request watchers.
    """

    def __init__(self, default_interval: int = 60) -> None:
        self._watchers: Dict[str, RequestWatcher] = {}
        self._interval = default_interval

    # ── Lifecycle ───────────────────────────────────────────────────

    def add_watcher(self, request_data: Dict[str, Any]) -> Optional[RequestWatcher]:
        """
        Start watching a booking request.

        Parameters
        ----------
        request_data : dict
            Must have ``id``, ``movie_name``, ``date``, ``cinemas``,
            ``movie_url`` (or ``booking_url``).

        Returns
        -------
        RequestWatcher or None
        """
        rid = request_data.get("id")
        if not rid:
            logger.error("Cannot add watcher: request has no 'id' field.")
            return None

        # Don't double-add
        if rid in self._watchers and self._watchers[rid].is_running:
            logger.info("[%s] Watcher already exists — skipping.", rid)
            return self._watchers[rid]

        # Don't watch already-booked or failed requests
        status = request_data.get("status", "")
        if status in ("booked", "booking_failed", "expired"):
            logger.info("[%s] Status is '%s' — no watcher needed.", rid, status)
            return None

        # Remove stale watcher first
        if rid in self._watchers:
            old = self._watchers.pop(rid)
            asyncio.create_task(old.stop())

        watcher = RequestWatcher(request_data, interval=self._interval)
        self._watchers[rid] = watcher
        watcher.start()
        return watcher

    async def remove_watcher(self, request_id: str) -> bool:
        """
        Stop and remove a watcher.

        Returns ``True`` if a watcher was removed.
        """
        watcher = self._watchers.pop(request_id, None)
        if watcher:
            await watcher.stop()
            return True
        return False

    async def restart_all(self, request_datas: List[Dict[str, Any]]) -> None:
        """
        Stop all current watchers and start fresh ones from a list.
        Used on server startup to restore watchers.
        """
        for watcher in list(self._watchers.values()):
            await watcher.stop()
        self._watchers.clear()

        for rd in request_datas:
            self.add_watcher(rd)

    # ── Status ──────────────────────────────────────────────────────

    def get_all_status(self) -> List[Dict[str, Any]]:
        """Return status of every watcher."""
        return [w.status() for w in self._watchers.values()]

    def get_status(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Return status of a single watcher."""
        watcher = self._watchers.get(request_id)
        return watcher.status() if watcher else None

    def is_watching(self, request_id: str) -> bool:
        """Check if a request is currently being watched."""
        watcher = self._watchers.get(request_id)
        return watcher is not None and watcher.is_running

    def watcher_count(self) -> int:
        """Number of active watchers."""
        return sum(1 for w in self._watchers.values() if w.is_running)

    def reapply_from_config(self, requests: List[Dict[str, Any]]) -> None:
        """
        Diff the current watcher set against a fresh list of requests
        and add/remove watchers as needed.
        """
        fresh_ids = {
            r["id"] for r in requests
            if r.get("status") in ("monitoring", "active")
            and r.get("auto_book")
        }

        for rid in list(self._watchers.keys()):
            if rid not in fresh_ids:
                asyncio.create_task(self.remove_watcher(rid))

        for r in requests:
            rid = r.get("id")
            if rid in fresh_ids and rid not in self._watchers:
                self.add_watcher(r)
