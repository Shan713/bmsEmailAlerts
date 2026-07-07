#!/usr/bin/env python3
"""
Watch for BookMyShow bookings to open and auto-book via AI agent.

Continuously monitors booking requests from ``config.json``.  When a
target cinema appears on the booking page, it triggers the AI booking
agent automatically — no manual intervention needed.

Usage::

    python watch.py                     # watch ALL monitoring requests
    python watch.py --request-id req_001  # watch a single request
    python watch.py --once              # check once and exit
    python watch.py --interval 120      # check every 2 minutes
    python watch.py --headless          # run browser in background
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("watch.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("watch")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_config(path: str = "config.json") -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)


def save_config(config: dict[str, Any], path: str = "config.json") -> None:
    with open(path, "w") as f:
        json.dump(config, f, indent=2)


def build_booking_url(movie_url: str, date: str) -> Optional[str]:
    """
    Construct a ``/buytickets/...`` URL from a movie-page URL + date.

    >>> build_booking_url(
    ...   "https://in.bookmyshow.com/movies/coimbatore/gatta-kusthi-2/ET00502802",
    ...   "2026-07-07"
    ... )
    'https://in.bookmyshow.com/movies/coimbatore/gatta-kusthi-2/buytickets/ET00502802/20260707'
    """
    # Already a buytickets URL?
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


# ---------------------------------------------------------------------------
# Page checker
# ---------------------------------------------------------------------------

async def check_booking_page(
    page,
    booking_url: str,
    target_cinemas: List[str],
) -> Dict[str, Any]:
    """
    Navigate to the booking page and check whether target cinemas are listed.

    Returns
    -------
    dict
        ``available`` (bool), ``found_cinemas`` (list[str]),
        ``screenshot_path`` (str | None).
    """
    result: dict[str, Any] = {
        "available": False,
        "found_cinemas": [],
        "screenshot_path": None,
    }

    try:
        logger.info("  🌐 Navigating to booking page...")
        await page.goto(booking_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(4000)  # let JS render showtimes

        # Screenshot for debugging
        screenshot_dir = Path("screenshots")
        screenshot_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_path = str(screenshot_dir / f"watch_check_{ts}.png")
        await page.screenshot(path=screenshot_path)
        result["screenshot_path"] = screenshot_path

        # Grab page text
        page_text = await page.text_content("body") or ""
        page_text_lower = page_text.lower()

        # --- Blocked / not-yet-open indicators --------------------------
        not_open_keywords = [
            "coming soon",
            "no shows available",
            "showtimes not available",
            "tickets coming soon",
            "bookings will open soon",
            "currently no shows",
            "no showtimes",
            "uh-oh!",
            "we couldn't find anything",
        ]

        if any(kw in page_text_lower for kw in not_open_keywords):
            logger.info("  ⏳ Bookings not open yet ('coming soon' etc.).")
            return result

        # --- Check for target cinemas -----------------------------------
        for cinema in target_cinemas:
            if cinema.lower() in page_text_lower:
                result["found_cinemas"].append(cinema)
                result["available"] = True

        if result["available"]:
            logger.info("  ✅ Target cinemas FOUND: %s", result["found_cinemas"])
        else:
            # Log any cinema-like text for debugging
            lines = [l.strip() for l in page_text.split("\n") if l.strip()]
            cinema_lines = [
                l for l in lines
                if any(
                    kw in l.lower()
                    for kw in [
                        "pvr", "inox", "cinema", "broadway", "cinepolis",
                        "imax", "screen", "theatre", "multiplex", "miraj",
                    ]
                )
            ]
            if cinema_lines:
                logger.info("  📋 Cinema-like text on page: %s", cinema_lines[:10])
            else:
                logger.info("  ❌ No cinema text found on page.")

        return result

    except Exception as exc:
        logger.error("  ⚠️  Error checking page: %s", exc)
        return result


# ---------------------------------------------------------------------------
# Main watch loop
# ---------------------------------------------------------------------------

def _build_watch_list(
    config: dict[str, Any],
    request_ids: Optional[List[str]] = None,
) -> list[dict[str, Any]]:
    """
    Build a list of watchable requests from *config*.

    Parameters
    ----------
    config : dict
        Parsed ``config.json``.
    request_ids : list[str] or None
        If provided, only return requests whose ID is in this list.
        ``None`` = all eligible requests.

    Returns
    -------
    list[dict]
        Each dict has the booking-request fields plus a ``booking_url`` key.
    """
    all_requests: list[dict[str, Any]] = config.get("booking_requests", [])
    to_watch: list[dict[str, Any]] = []

    for req in all_requests:
        rid = req.get("id", "")

        if request_ids and rid not in request_ids:
            continue
        if req.get("status") not in ("monitoring", "active"):
            continue
        if not req.get("auto_book"):
            continue

        movie_url = req.get("movie_url")
        if not movie_url:
            logger.warning(
                "[%s] No movie_url in config — add it to enable watching.", rid
            )
            continue

        booking_url = build_booking_url(movie_url, req.get("date", ""))
        if not booking_url:
            logger.warning(
                "[%s] Could not build booking URL from: %s", rid, movie_url
            )
            continue

        to_watch.append({**req, "booking_url": booking_url})

    return to_watch


async def _trigger_ai_booking(
    config: dict[str, Any],
    req: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """
    Launch the AI booking agent for a detected cinema match.

    Parameters
    ----------
    config : dict
        Freshly-loaded ``config.json`` (so user_profile is current).
    req : dict
        A watch-list entry with ``booking_url``, ``movie_name``, etc.

    Returns
    -------
    dict or None
        The agent's result dict, or ``None`` on fatal error.
    """
    rid = req["id"]
    logger.info("[%s] 🤖 Launching AI booking agent…", rid)

    from ai_booking_agent import AIBrowserBookingAgent

    # Build time window
    time_ranges = req.get("preferred_time_range", [])
    showtimes = (
        config.get("user_profile", {})
        .get("preferred_showtimes", {})
    )
    hours: list[int] = []
    for key in time_ranges:
        for slot in showtimes.get(key, []):
            try:
                hours.append(int(slot.strip().split(":")[0]))
            except (ValueError, IndexError):
                pass
    time_window = (min(hours), max(hours) + 1) if hours else None

    agent = AIBrowserBookingAgent(config)
    result = await agent.execute_booking(
        request_id=rid,
        booking_url=req["booking_url"],
        movie_name=req["movie_name"],
        cinema=(
            req.get("cinemas", [""])[0]
            if req.get("cinemas") else ""
        ),
        date=req["date"],
        city=req.get("city", "Coimbatore"),
        time_window=time_window,
        num_tickets=(
            config.get("user_profile", {})
            .get("max_tickets", 2)
        ),
        dry_run=False,
    )

    if result and result.get("success"):
        logger.info("✅ [%s] BOOKING SUCCESSFUL!", rid)
        # Persist status to config
        try:
            fresh = load_config()
            for r in fresh.get("booking_requests", []):
                if r.get("id") == rid:
                    r["status"] = "booked"
                    break
            save_config(fresh)
        except Exception as exc:
            logger.warning("Could not update config status: %s", exc)
    else:
        error = result.get("error") if result else "no result"
        logger.error("❌ [%s] AI booking failed: %s", rid, error)

    return result


# ---------------------------------------------------------------------------
# Main watch loop
# ---------------------------------------------------------------------------

async def watch_requests(
    request_ids: Optional[List[str]] = None,
    interval: int = 60,
    once: bool = False,
    headless: bool = False,
) -> None:
    """
    Watch all (or specific) booking requests and auto-book when live.

    Re-reads ``config.json`` on every polling cycle so that newly-added
    requests are picked up without restarting the watcher.

    Parameters
    ----------
    request_ids : list[str] | None
        Specific request IDs to watch.  ``None`` = all monitoring requests.
    interval : int
        Seconds between checks (default 60).
    once : bool
        Check once and exit.
    headless : bool
        Run the checker browser in headless mode.
    """
    config = load_config()
    to_watch: list[dict[str, Any]] = _build_watch_list(config, request_ids)

    if not to_watch:
        logger.info(
            "No requests to watch.  Add a request with 'auto_book: true' "
            "and 'movie_url' set in config.json."
        )
        if once:
            return
        logger.info(
            "👀 Will keep polling config.json every %ds for new requests…",
            interval,
        )

    # Track request IDs we already know about, plus completed bookings.
    known_ids: set[str] = {r["id"] for r in to_watch}
    booked_ids: set[str] = set()

    def _log_watch_list() -> None:
        if not to_watch:
            logger.info("   (no requests currently being watched)")
            return
        logger.info("👀 Watching %d request(s):", len(to_watch))
        for w in to_watch:
            tag = " ✅" if w["id"] in booked_ids else ""
            logger.info(
                "    [%s] %s — %s (%s)%s",
                w["id"], w["movie_name"], w["date"],
                ", ".join(w.get("cinemas", [])),
                tag,
            )

    _log_watch_list()

    # --- Import Playwright ----------------------------------------------
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)

        try:
            while True:
                # ── Reload config to pick up new / updated requests ────
                fresh_config = load_config()
                fresh_watch = _build_watch_list(fresh_config, request_ids)

                # Check for requests that changed status to "booked"
                # externally (e.g. via web dashboard).
                for r in fresh_config.get("booking_requests", []):
                    rid = r.get("id", "")
                    if rid in known_ids and r.get("status") == "booked":
                        booked_ids.add(rid)

                # Merge in NEW requests we haven't seen before.
                for nw in fresh_watch:
                    if nw["id"] not in known_ids:
                        known_ids.add(nw["id"])
                        to_watch.append(nw)
                        logger.info(
                            "🆕 [%s] New request picked up: %s at %s on %s",
                            nw["id"], nw["movie_name"],
                            ", ".join(nw.get("cinemas", [])), nw["date"],
                        )

                # Remove requests that are no longer in monitoring/active
                # status (e.g. user deleted them or marked them inactive).
                fresh_ids = {r["id"] for r in fresh_watch}
                removed = [r for r in to_watch if r["id"] not in fresh_ids]
                for rem in removed:
                    logger.info(
                        "🗑️  [%s] Request removed from watch (status changed).",
                        rem["id"],
                    )
                    to_watch.remove(rem)
                    known_ids.discard(rem["id"])

                # Also mark as booked any that appear as booked in fresh config.
                for r in fresh_config.get("booking_requests", []):
                    rid = r.get("id", "")
                    if rid in known_ids and r.get("status") == "booked":
                        booked_ids.add(rid)

                # ── Check each watched request ─────────────────────────
                for req in to_watch:
                    rid = req["id"]

                    if rid in booked_ids:
                        continue

                    logger.info("-" * 50)
                    logger.info(
                        "[%s] 🔍 Checking %s (%s)…",
                        rid, req["movie_name"], req["date"],
                    )

                    context = await browser.new_context(
                        viewport={"width": 1920, "height": 1080},
                        user_agent=(
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"
                        ),
                    )
                    page = await context.new_page()

                    try:
                        check = await check_booking_page(
                            page,
                            req["booking_url"],
                            req.get("cinemas", []),
                        )

                        if check["available"]:
                            logger.info(
                                "🎯 [%s] BOOKINGS ARE LIVE! Cinemas: %s",
                                rid, check["found_cinemas"],
                            )
                            result = await _trigger_ai_booking(
                                fresh_config, req,
                            )
                            if result and result.get("success"):
                                booked_ids.add(rid)
                        else:
                            logger.info("[%s] Not available yet.", rid)

                    finally:
                        await context.close()

                # --- Loop control ----------------------------------------
                if once:
                    logger.info("--once mode — exiting.")
                    break

                pending = [
                    r for r in to_watch if r["id"] not in booked_ids
                ]
                if pending:
                    logger.info(
                        "⏳ Waiting %ds before next check "
                        "(%d request(s) remaining)…",
                        interval, len(pending),
                    )
                else:
                    # Everything we know about is booked — but keep polling
                    # config in case new requests are added.
                    logger.info(
                        "⏳ All known requests booked. "
                        "Polling config every %ds for new requests…",
                        interval,
                    )
                await asyncio.sleep(interval)

        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Watch BMS for bookings to open, then auto-book via AI.",
    )
    parser.add_argument(
        "--request-id", "-r",
        help="Watch only this request ID.",
    )
    parser.add_argument(
        "--interval", "-i",
        type=int,
        default=60,
        help="Seconds between checks (default: 60).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Check once and exit.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run the checker browser headless.",
    )
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("🚀 BMS Watch & Auto-Book")
    logger.info("   %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("=" * 60)

    await watch_requests(
        request_ids=[args.request_id] if args.request_id else None,
        interval=args.interval,
        once=args.once,
        headless=args.headless,
    )


if __name__ == "__main__":
    asyncio.run(main())
