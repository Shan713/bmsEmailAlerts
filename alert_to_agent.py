#!/usr/bin/env python3
"""
Bridge: Alert System → AI Booking Agent.

Monitors the alert system's cinema-detection events and triggers
the AI-powered booking agent when a matching cinema is detected,
passing the **direct booking URL** so the agent starts from the
showtime page — no homepage → search → movie navigation needed.

Usage::

    bridge = AlertToAgentBridge(config)
    await bridge.on_booking_detected(alert_data)

The bridge:
- Matches detected cinemas against ``config.json`` booking requests
- Extracts the booking URL from the alert system
- Triggers :class:`AIBrowserBookingAgent` with a direct-URL task
- Tracks booked shows to prevent duplicate bookings
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)


class AlertToAgentBridge:
    """
    Connects the alert system's cinema detection to the AI booking agent.

    Parameters
    ----------
    config : dict
        Parsed ``config.json``.
    credentials_path : str
        Path to the encrypted credentials file.
    """

    def __init__(
        self,
        config: dict[str, Any],
        credentials_path: str = "credentials.enc",
    ) -> None:
        self.config = config
        self.credentials_path = credentials_path
        self.booked_shows: Set[str] = set()  # prevent duplicates

    # ------------------------------------------------------------------
    # Main entry point — called by the alert system
    # ------------------------------------------------------------------

    async def on_booking_detected(self, alert_data: dict[str, Any]) -> Optional[dict[str, Any]]:
        """
        Called by the alert system when a movie/cinema is detected.

        Parameters
        ----------
        alert_data : dict
            Must contain:
            - ``movie_name`` (str)
            - ``cinema`` (str) — detected cinema name
            - ``date`` (str) — ``YYYY-MM-DD``
            - ``booking_url`` (str) — full BMS booking URL
            - ``unique_code`` (str) — BMS ``ET...`` code
            - ``time_window`` (tuple[int, int] | None) — (start_hour, end_hour)
            - ``num_tickets`` (int)
            - ``city`` (str) — e.g. "Coimbatore"

        Returns
        -------
        dict or None
            Booking result dict, or ``None`` if no matching request was found
            or the show was already booked.
        """
        movie_name = alert_data.get("movie_name", "")
        alert_cinema = alert_data.get("cinema", "")
        alert_date = alert_data.get("date", "")
        unique_code = alert_data.get("unique_code", "")
        booking_url = alert_data.get("booking_url", "")

        if not booking_url:
            logger.error("No booking_url in alert_data — cannot proceed.")
            return None

        # --- Match against config booking requests --------------------------
        booking_requests: list[dict[str, Any]] = (
            self.config.get("booking_requests", [])
        )

        for req in booking_requests:
            req_id = req.get("id", "?")
            status = req.get("status", "")

            # Only process active / monitoring requests
            if status not in ("active", "monitoring"):
                continue

            # Skip if auto_book is explicitly disabled
            if not req.get("auto_book", False):
                logger.info("[%s] auto_book disabled — skipping.", req_id)
                continue

            # --- Match by movie name (case-insensitive) ---------------------
            req_movie = req.get("movie_name", "").lower()
            if req_movie != movie_name.lower():
                continue

            # --- Match by cinema --------------------------------------------
            target_cinemas = [
                c.lower() for c in req.get("cinemas", [])
            ]
            alert_cinema_lower = alert_cinema.lower()
            if not any(c in alert_cinema_lower for c in target_cinemas):
                continue

            # --- Dedup check ------------------------------------------------
            booking_key = f"{unique_code}_{alert_cinema}_{alert_date}"
            if booking_key in self.booked_shows:
                logger.info("[%s] Already booked: %s — skipping.", req_id, booking_key)
                continue

            logger.info(
                "🎯 [%s] Match found! %s at %s on %s",
                req_id, movie_name, alert_cinema, alert_date,
            )
            logger.info("[%s] Booking URL: %s", req_id, booking_url)

            # --- Trigger the AI agent with direct URL -----------------------
            from ai_booking_agent import AIBrowserBookingAgent

            agent = AIBrowserBookingAgent(
                self.config,
                credentials_file=self.credentials_path,
            )

            # Build a synthetic booking_request for execute_booking
            time_window = alert_data.get("time_window")
            num_tickets = alert_data.get("num_tickets", 2)

            result = await agent.execute_booking(
                request_id=req_id,
                booking_url=booking_url,
                movie_name=movie_name,
                cinema=alert_cinema,
                date=alert_date,
                city=alert_data.get("city", "Coimbatore"),
                time_window=time_window,
                num_tickets=num_tickets,
                dry_run=False,
            )

            if result and result.get("success"):
                self.booked_shows.add(booking_key)
                logger.info("✅ [%s] Booking successful!", req_id)
            else:
                logger.error(
                    "❌ [%s] Booking failed: %s",
                    req_id, result.get("error") if result else "no result",
                )

            return result

        logger.info(
            "No matching booking request found for %s at %s.",
            movie_name, alert_cinema,
        )
        return None


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------

async def _self_test() -> None:
    """Smoke-test the bridge without a real browser."""
    import json
    import logging as _logging

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config_path = Path(__file__).resolve().parent / "config.json"
    if not config_path.exists():
        print(f"❌ config.json not found at {config_path}")
        return

    with open(config_path) as fh:
        config = json.load(fh)

    bridge = AlertToAgentBridge(config)

    # Simulate an alert detection
    sample_alert = {
        "movie_name": "Gatta Kusthi 2",
        "cinema": "Broadway Cinemas",
        "date": "2026-07-07",
        "booking_url": "https://in.bookmyshow.com/movies/coimbatore/gatta-kusthi-2/buytickets/ET00502802/20260707",
        "unique_code": "ET00502802",
        "time_window": (10, 13),
        "num_tickets": 2,
        "city": "Coimbatore",
    }

    print("=" * 60)
    print("AlertToAgentBridge — Self Test")
    print("=" * 60)
    print(f"Sample alert: {json.dumps(sample_alert, indent=2, default=str)}")
    print()
    print("Matching against booking requests...")

    # Don't actually trigger the AI agent — just verify matching logic.
    # We monkey-patch execute_booking to avoid real browser launch.
    original_execute = None
    try:
        from ai_booking_agent import AIBrowserBookingAgent
        original_execute = AIBrowserBookingAgent.execute_booking

        async def mock_execute(self, **kwargs):
            print(f"  → Would trigger AI agent with:")
            print(f"      URL: {kwargs.get('booking_url', 'N/A')}")
            print(f"      Movie: {kwargs.get('movie_name', 'N/A')}")
            print(f"      Cinema: {kwargs.get('cinema', 'N/A')}")
            print(f"      City: {kwargs.get('city', 'N/A')}")
            print(f"      Date: {kwargs.get('date', 'N/A')}")
            print(f"      Time: {kwargs.get('time_window', 'N/A')}")
            print(f"      Tickets: {kwargs.get('num_tickets', 'N/A')}")
            return {
                "request_id": kwargs.get("request_id"),
                "movie_name": kwargs.get("movie_name"),
                "success": True,
                "booking_id": "MOCK-BOOKING-ID",
                "dry_run": False,
            }

        AIBrowserBookingAgent.execute_booking = mock_execute

        result = await bridge.on_booking_detected(sample_alert)
        if result:
            print(f"\n✅ Bridge matched and triggered booking!")
            print(f"   Result: {json.dumps(result, indent=2)}")
        else:
            print("\n⚠️  No match — check config booking_requests.")
    finally:
        if original_execute:
            AIBrowserBookingAgent.execute_booking = original_execute

    print()
    print("=" * 60)
    print("✅ Bridge self-test complete.")
    print("=" * 60)


if __name__ == "__main__":
    from pathlib import Path
    asyncio.run(_self_test())
