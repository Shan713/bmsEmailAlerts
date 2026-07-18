#!/usr/bin/env python3
"""
AI-powered BookMyShow booking agent using the browser-use library.

Replaces hardcoded Playwright CSS selectors with an LLM-powered agent that
sees screenshots and decides what to do — eliminating fragile DOM selectors
that break when BMS changes their UI.

Usage::

    from ai_booking_agent import AIBrowserBookingAgent

    config = load_config()
    agent = AIBrowserBookingAgent(config)
    result = await agent.run(booking_request, dry_run=True)

The credential manager, config.json, and CLI entry point are kept unchanged.
The ``--ai`` flag on ``book_ticket.py`` switches from the old Playwright
automator to this agent.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

# Load environment variables from .env (API keys, etc.)
from dotenv import load_dotenv
load_dotenv()

from browser_use import Agent, BrowserProfile
from browser_use.llm.anthropic.chat import ChatAnthropic
from browser_use.llm.deepseek.chat import ChatDeepSeek

from credential_manager import SecureCredentialManager

try:
    from otp_relay import EmailOTPRelay
    _OTP_AVAILABLE = True
except ImportError:
    _OTP_AVAILABLE = False

logger = logging.getLogger(__name__)

# CDP URL for reconnecting to the browser-use managed Chrome instance.
# browser-use opens Chrome with this debugging port so that the OTP relay
# can connect via Playwright's connect_over_cdp and fill the OTP field
# after the main agent task has completed.
_BMS_CDP_URL = "http://127.0.0.1:9222"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BMS_BASE_URL = "https://in.bookmyshow.com"

# Default LLM models — can be overridden via environment variables.
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-20250514"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_time_range_keys(
    preferred_ranges: list[str],
    showtimes_config: dict[str, list[str]],
) -> str:
    """Convert a list of time-range keys into a human-readable time window."""
    if not preferred_ranges:
        return "any time"

    hours: list[int] = []
    for key in preferred_ranges:
        slots = showtimes_config.get(key, [])
        for slot in slots:
            try:
                hour = int(slot.strip().split(":")[0])
                hours.append(hour)
            except (ValueError, IndexError):
                continue

    if not hours:
        return "any time"

    min_hour = min(hours)
    max_hour = max(hours) + 1  # inclusive
    return f"{min_hour:02d}:00 to {max_hour:02d}:00"


# ---------------------------------------------------------------------------
# AIBrowserBookingAgent
# ---------------------------------------------------------------------------

class AIBrowserBookingAgent:
    """
    AI-powered booking agent that uses browser-use to automate BookMyShow.

    Parameters
    ----------
    config : dict
        The parsed contents of ``config.json``.
    credentials_file : str
        Path to the encrypted credentials file (default ``credentials.enc``).

    LLM Selection
    -------------
    The agent picks the LLM in this order:

    1. If ``DEEPSEEK_API_KEY`` is set → ``ChatDeepSeek`` (native browser-use
       integration using tool-calling for structured output).
    2. Else if ``ANTHROPIC_API_KEY`` is set → ``ChatAnthropic`` with Claude.
    3. Otherwise raises ``RuntimeError``.

    You can override the model via ``BMS_LLM_MODEL`` (e.g.
    ``deepseek-reasoner`` or ``claude-sonnet-4-20250514``).
    """

    def __init__(
        self,
        config: dict[str, Any],
        credentials_file: str = "credentials.enc",
    ) -> None:
        self.config = config
        self.credentials_file = credentials_file
        self._credentials: Optional[dict[str, Any]] = None
        self._browser_session: Any = None

        # --- Resolve LLM -------------------------------------------------
        self.llm = self._build_llm()

    # ------------------------------------------------------------------
    # LLM factory
    # ------------------------------------------------------------------

    def _build_llm(self):
        """Build the LLM client based on available API keys.

        Priority: DeepSeek first, then Anthropic Claude as fallback.
        """
        deepseek_key = os.getenv("DEEPSEEK_API_KEY")
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        model_override = os.getenv("BMS_LLM_MODEL")

        if deepseek_key:
            model = model_override or DEFAULT_DEEPSEEK_MODEL
            logger.info("🤖 Using DeepSeek (native): %s", model)
            return ChatDeepSeek(
                model=model,
                api_key=deepseek_key,
                temperature=0.0,
            )

        if anthropic_key:
            model = model_override or DEFAULT_ANTHROPIC_MODEL
            logger.info("🤖 Using Anthropic Claude: %s", model)
            return ChatAnthropic(
                model=model,
                api_key=anthropic_key,
                temperature=0.0,
                max_tokens=8192,
            )

        raise RuntimeError(
            "No LLM API key found. Set either DEEPSEEK_API_KEY or "
            "ANTHROPIC_API_KEY in your environment.\n"
            "Example:  export DEEPSEEK_API_KEY=sk-..."
        )

    # ------------------------------------------------------------------
    # Credential loading (reuses SecureCredentialManager)
    # ------------------------------------------------------------------

    def _load_credentials(self) -> dict[str, Any]:
        """Load and decrypt credentials via SecureCredentialManager."""
        if self._credentials is not None:
            return self._credentials

        mgr = SecureCredentialManager(credentials_file=self.credentials_file)
        creds = mgr.get_credentials()
        if creds is None:
            raise RuntimeError(
                "No credentials found. Run 'python setup_creds.py' first."
            )
        self._credentials = creds
        return creds

    # ------------------------------------------------------------------
    # Task builder
    # ------------------------------------------------------------------

    def build_task(
        self,
        booking_request: dict[str, Any],
        dry_run: bool = False,
    ) -> str:
        """
        Build a detailed step-by-step task for the AI agent.

        The BMS accessibility modal uses **shadow DOM** for its dropdowns,
        which blocks normal ``document.querySelector``.  We therefore use
        browser-use's built-in ``dropdown_options(index)`` +
        ``select_dropdown_option(index, text)`` for selects (these tools
        pierce shadow DOM at the browser-protocol level).

        Seats are still selected via JavaScript ``evaluate`` because they
        are regular buttons, not shadow-DOM selects.

        All credentials are injected directly — the LLM never sees
        placeholders.
        """
        creds = self._load_credentials()
        user_details = creds.get("user_details", {})
        card_details = creds.get("card", {})

        # --- Extract fields with safe defaults ---------------------------
        movie_name: str = booking_request.get("movie_name", "")
        date: str = booking_request.get("date", "")
        city: str = booking_request.get("city", "Coimbatore")
        cinemas: list[str] = booking_request.get("cinemas", [])
        preferred_ranges: list[str] = booking_request.get(
            "preferred_time_range", []
        )
        num_tickets: int = (
            self.config.get("user_profile", {}).get("max_tickets", 2)
        )

        # --- Time range --------------------------------------------------
        showtimes_config = (
            self.config.get("user_profile", {})
            .get("preferred_showtimes", {})
        )
        time_window = _parse_time_range_keys(
            preferred_ranges, showtimes_config
        )
        time_start = time_window.split(" to ")[0] if " to " in time_window else "10:00"
        time_end = time_window.split(" to ")[1] if " to " in time_window else "13:00"

        # --- Cinema ------------------------------------------------------
        cinema_name = cinemas[0] if cinemas else "any available cinema"

        # --- Injected credentials ----------------------------------------
        email = user_details.get("email", "")
        phone = user_details.get("phone", "")
        card_number = card_details.get("number", "")
        card_expiry = card_details.get("expiry", "")
        card_cvv = card_details.get("cvv", "")
        card_name = card_details.get("name", "")

        # --- Date formatting --------------------------------------------
        try:
            dt = datetime.strptime(date, "%Y-%m-%d")
            date_readable = dt.strftime("%A, %d %B %Y")
        except (ValueError, TypeError):
            date_readable = date

        # --- Dry-run logic -----------------------------------------------
        if dry_run:
            pay_step_instruction = (
                '⛔ DRY-RUN: SKIP STEP 19 (card payment). '
                'After STEP 18 (contact details), STOP. '
                'Report: "DRY-RUN COMPLETE: Reached payment page safely."'
            )
            final_step = (
                'DRY-RUN COMPLETE. Report: "Reached payment page safely. '
                'No payment was attempted."'
            )
        else:
            pay_step_instruction = (
                '- After filling all card fields, click "Pay Now" or '
                '"Pay ₹XXX".'
            )
            final_step = (
                'If a bank OTP popup appears after Pay Now, '
                'DO NOT enter OTP. Report: "Card details submitted. '
                'Waiting for OTP page."'
            )

        # --- Build the task ----------------------------------------------
        task = f"""CRITICAL RULES — READ FIRST:
⛔ The BMS accessibility modal uses SHADOW DOM for its dropdowns. JavaScript evaluate() with document.querySelector CANNOT reach them. Use dropdown_options(index=X) + select_dropdown_option for ALL selects in the modal.
⛔ Maximum 3 attempts per step. If you find yourself repeating the same action 3 times with no result, STOP and report exactly where you are. Do NOT loop.
⛔ Do NOT improvise. Follow the steps exactly.
⛔ Be FAST. Don't overthink.

TASK: Book a movie ticket on BookMyShow.

STEP 1: Go to https://in.bookmyshow.com

STEP 2: If a city/region popup appears, type "{city}" and click the matching city.

STEP 3: Click the search icon, type "{movie_name}", click the movie from suggestions.

STEP 4: Click "Book tickets" on the movie page.

STEP 5: Ensure the date "{date}" ({date_readable}) is selected. Click it if not.

STEP 6: Scroll to find "{cinema_name}" showtimes.

STEP 7: Click the earliest showtime between {time_start} and {time_end} for "{cinema_name}". If none in that window, pick the closest.

STEP 8: If a confirmation popup appears, click "Continue" or "Yes".

STEP 9: Set ticket quantity to {num_tickets} on the "How many seats?" page.

STEP 10: Click "Select Seats".

STEP 11: Click the ACCESSIBILITY MODE button (♿ wheelchair icon near top of seat area). If not found after 2 attempts, use the visual seat map instead.

STEP 12: Inside the accessibility modal, set the THREE dropdowns using browser-use tools (NOT evaluate — the selects are inside shadow DOM):

STEP 12a: First, set the QUANTITY dropdown. Find the quantity <select> element index (it will have options like "1 Ticket", "2 Tickets", etc.). Use dropdown_options(index=X) to list its options, then use select_dropdown_option to pick the one containing "{num_tickets} Ticket" (ignore the ₹ price).

STEP 12b: Set the CATEGORY dropdown. Use dropdown_options(index=X) to list options, then use select_dropdown_option to pick ELITE if available, otherwise GOLD.

STEP 12c: Set the ROW dropdown. Use dropdown_options(index=X) to list the rows, count them, then use select_dropdown_option to pick the MIDDLE row (e.g. if rows A-H, pick D or E).

STEP 13: Now select {num_tickets} contiguous seats as close to CENTRE of the row as possible. Seats are regular buttons (NOT shadow DOM), so use evaluate with JavaScript:

```
(function() {{
    var seats = Array.from(document.querySelectorAll('button[aria-label*="Seat"], button[aria-label*="seat"], [role="button"][aria-label*="Seat"]'));
    var available = seats.filter(function(s) {{
        var label = s.getAttribute('aria-label') || '';
        return label.toLowerCase().includes('available') || (!label.toLowerCase().includes('unavailable') && !label.toLowerCase().includes('sold') && !label.toLowerCase().includes('booked'));
    }});
    console.log('TOTAL SEAT BUTTONS: ' + seats.length + ', AVAILABLE: ' + available.length);

    var byRow = {{}};
    available.forEach(function(s) {{
        var label = s.getAttribute('aria-label') || '';
        var match = label.match(/[Rr]ow\\s*([A-Za-z])/);
        var row = match ? match[1].toUpperCase() : 'Z';
        if (!byRow[row]) byRow[row] = [];
        byRow[row].push(s);
    }});

    var rowKeys = Object.keys(byRow).sort();
    if (rowKeys.length === 0) {{ console.log('NO SEATS FOUND'); return; }}
    var midRow = rowKeys[Math.floor(rowKeys.length / 2)];
    console.log('ROWS: ' + rowKeys.join(',') + ', MIDDLE: ' + midRow);

    var rowSeats = byRow[midRow];
    var midIdx = Math.floor(rowSeats.length / 2);
    var start = Math.max(0, midIdx - Math.floor({num_tickets} / 2));
    var end = Math.min(rowSeats.length, start + {num_tickets});
    if (end - start < {num_tickets}) {{ start = Math.max(0, rowSeats.length - {num_tickets}); end = rowSeats.length; }}
    var picked = rowSeats.slice(start, end);
    console.log('CLICKING ' + picked.length + ' SEATS from row ' + midRow);
    picked.forEach(function(s) {{ s.click(); }});
}})();
```

STEP 14: Click "Pay ₹XXX" or "Proceed to Pay" INSIDE the accessibility modal. Do NOT close the modal first.

STEP 15: Accept any Terms & Conditions popup (check box + "Accept"/"Continue").

STEP 16: If a loading/confirmation overlay appears, WAIT — do not interact.

STEP 17: If a Food & Beverages upsell page appears, click "Skip" or "No thanks".

STEP 18: On the Contact Details page, fill Email: {email} and Phone: {phone}, then click "Submit"/"Continue".

STEP 19: On the Payments page:
- Click "Debit/Credit Card" or "Card" payment option. If not visible, try "More payment options" or scroll down.
- Once the card form is visible, you will see 4 input fields in the DOM.  They are likely inside SHADOW DOM — JavaScript evaluate CANNOT reach them.  Use browser-use's input_text() tool with the element INDEX instead:

STEP 19a: Find the card-number input (look for an input with placeholder "Card Number" or similar).  Use input_text(index=THE_INDEX, text="{card_number}") to fill it.

STEP 19b: Find the expiry input (placeholder "MM/YY").  Use input_text(index=THE_INDEX, text="{card_expiry}").

STEP 19c: Find the CVV input (placeholder "CVV").  Use input_text(index=THE_INDEX, text="{card_cvv}").

STEP 19d: Find the cardholder-name input (placeholder "Cardholder Name" or "Name on Card").  Use input_text(index=THE_INDEX, text="{card_name}").

{pay_step_instruction}

STEP 20: {final_step}

REMINDERS:
- BMS accessibility modal selects are in SHADOW DOM — use dropdown_options() + select_dropdown_option(), NOT evaluate JS.
- BMS card-payment inputs are ALSO in shadow DOM — use input_text(index=X, text="..."), NOT evaluate JS.
- Seats are regular DOM buttons — use evaluate JS for those.
- If any step reports "NOT FOUND", try clicking the element visually instead.
- Skip non-applicable steps (e.g. no F&B page) and move on.
- Maximum 35 steps total. Exceeding this → stop and report.
- 3 repeats of the same action with no result → STOP and report."""

        return task

    # ------------------------------------------------------------------
    # Direct-task builder (when booking URL is already known)
    # ------------------------------------------------------------------

    def _build_direct_task(
        self,
        booking_url: str,
        movie_name: str,
        cinema: str,
        date: str,
        city: str,
        time_window: tuple[int, int] | None,
        num_tickets: int,
        dry_run: bool,
    ) -> str:
        """
        Build a SHORT task for when the booking URL is already known.

        Skips homepage → search → movie navigation entirely.  The agent
        starts at the showtime page and only does: showtime, accessibility,
        seats, contact, payment.

        Parameters
        ----------
        booking_url : str
            Full BMS booking URL (e.g.
            ``https://in.bookmyshow.com/movies/coimbatore/.../buytickets/ET00502802/20260707``).
        movie_name : str
        cinema : str
        date : str
            ``YYYY-MM-DD``.
        city : str
        time_window : tuple[int, int] or None
            (start_hour, end_hour), e.g. (10, 13).  ``None`` = any time.
        num_tickets : int
        dry_run : bool
        """
        creds = self._load_credentials()
        user_details = creds.get("user_details", {})
        card_details = creds.get("card", {})

        email = user_details.get("email", "")
        phone = user_details.get("phone", "")
        card_number = card_details.get("number", "")
        card_expiry = card_details.get("expiry", "")
        card_cvv = card_details.get("cvv", "")
        card_name = card_details.get("name", "")

        # --- Time window ---------------------------------------------------
        if time_window:
            time_start = f"{time_window[0]:02d}:00"
            time_end = f"{time_window[1]:02d}:00"
            time_instruction = (
                f"Click the earliest showtime between {time_start} and "
                f"{time_end} for \"{cinema}\". If none in that window, "
                f"pick the CLOSEST available showtime."
            )
        else:
            time_instruction = (
                f"Click the earliest available showtime for \"{cinema}\"."
            )

        # --- Date formatting -----------------------------------------------
        try:
            dt = datetime.strptime(date, "%Y-%m-%d")
            date_readable = dt.strftime("%A, %d %B %Y")
        except (ValueError, TypeError):
            date_readable = date

        # --- Dry-run -------------------------------------------------------
        if dry_run:
            pay_step = (
                '⛔ DRY-RUN: SKIP card payment. After STEP 12 (contact '
                'details), STOP. Report: "DRY-RUN COMPLETE."'
            )
            final_step = (
                'DRY-RUN COMPLETE. Report: "Reached payment page safely."'
            )
        else:
            pay_step = (
                '- Fill all card fields with the JavaScript above, then '
                'click "Pay Now" or "Pay ₹XXX".'
            )
            final_step = (
                'If a bank OTP popup appears after Pay Now, DO NOT enter '
                'OTP. Report: "Card details submitted. Waiting for OTP."'
            )

        # --- Build ---------------------------------------------------------
        task = f"""CRITICAL RULES — READ FIRST:
⛔ You are STARTING FROM the booking URL — do NOT go to the homepage, do NOT search.
⛔ The BMS accessibility modal uses SHADOW DOM for dropdowns. Use dropdown_options(index=X) + select_dropdown_option, NOT evaluate JS for selects.
⛔ BMS card-payment inputs are ALSO in shadow DOM. Use input_text(index=X, text="..."), NOT evaluate JS for those.
⛔ Seats are regular DOM buttons — use evaluate JS for those.
⛔ Max 3 attempts per step.  Max 25 steps total.  If stuck 3 times → STOP and report.
⛔ Be FAST. Don't overthink.

TASK: Book "{movie_name}" at "{cinema}" on {date_readable}.  Start from the booking page directly.

STEP 1: Navigate to {booking_url}

STEP 2: If a city popup appears, type "{city}" and click the matching city.

STEP 3: The page should show dates. Ensure "{date}" ({date_readable}) is selected. Click it if not.

STEP 4: {time_instruction}

STEP 5: If a "Show Has Already Started" popup appears, click "Continue" or "Yes".

STEP 6: Set ticket quantity to {num_tickets} on the "How many seats?" page.

STEP 7: Click "Select Seats".

STEP 8: Click the ACCESSIBILITY MODE button (♿ wheelchair icon near top). If not found after 2 attempts, use the visual seat map.

STEP 9: Inside the accessibility modal, set THREE dropdowns (use dropdown_options + select_dropdown_option — NOT evaluate):

STEP 9a: QUANTITY — use dropdown_options(index=X) to list options, then select_dropdown_option to pick the one with "{num_tickets} Ticket".

STEP 9b: CATEGORY — use dropdown_options, pick ELITE if available, otherwise GOLD.

STEP 9c: ROW — use dropdown_options, count rows, pick the MIDDLE row.

STEP 10: Select {num_tickets} contiguous centre seats using evaluate JS:

```
(function() {{
    var seats = Array.from(document.querySelectorAll('button[aria-label*="Seat"], button[aria-label*="seat"], [role="button"][aria-label*="Seat"]'));
    var available = seats.filter(function(s) {{
        var label = s.getAttribute('aria-label') || '';
        return label.toLowerCase().includes('available') || (!label.toLowerCase().includes('unavailable') && !label.toLowerCase().includes('sold') && !label.toLowerCase().includes('booked'));
    }});
    var byRow = {{}};
    available.forEach(function(s) {{
        var label = s.getAttribute('aria-label') || '';
        var match = label.match(/[Rr]ow\\s*([A-Za-z])/);
        var row = match ? match[1].toUpperCase() : 'Z';
        if (!byRow[row]) byRow[row] = [];
        byRow[row].push(s);
    }});
    var rowKeys = Object.keys(byRow).sort();
    if (rowKeys.length === 0) {{ console.log('NO SEATS'); return; }}
    var midRow = rowKeys[Math.floor(rowKeys.length / 2)];
    var rowSeats = byRow[midRow];
    var midIdx = Math.floor(rowSeats.length / 2);
    var start = Math.max(0, midIdx - Math.floor({num_tickets} / 2));
    var end = Math.min(rowSeats.length, start + {num_tickets});
    if (end - start < {num_tickets}) {{ start = Math.max(0, rowSeats.length - {num_tickets}); end = rowSeats.length; }}
    rowSeats.slice(start, end).forEach(function(s) {{ s.click(); }});
    console.log('CLICKED ' + (end - start) + ' seats from row ' + midRow);
}})();
```

STEP 11: Click "Pay ₹XXX" or "Proceed" INSIDE the modal. Accept T&C if shown. Wait out any loading screens. Skip F&B upsell.

STEP 12: On Contact Details page, fill Email: {email}, Phone: {phone}, click "Submit"/"Continue".

STEP 13: On Payments page, click "Debit/Credit Card". The card input fields are in SHADOW DOM — use input_text() with element INDEX, NOT evaluate JS:

STEP 13a: Find the card-number input element (look for an input with index near the card form).  Use input_text(index=THAT_INDEX, text="{card_number}").

STEP 13b: Find the expiry input.  Use input_text(index=THAT_INDEX, text="{card_expiry}").

STEP 13c: Find the CVV input.  Use input_text(index=THAT_INDEX, text="{card_cvv}").

STEP 13d: Find the cardholder-name input.  Use input_text(index=THAT_INDEX, text="{card_name}").

{pay_step}

STEP 14: {final_step}

REMINDERS: Shadow DOM dropdowns = dropdown_options(). Shadow DOM inputs = input_text(index=X). Regular buttons = evaluate JS. Max 25 steps. Skip non-applicable steps."""

        return task

    # ------------------------------------------------------------------
    # Execute booking (supports direct URL from alert system)
    # ------------------------------------------------------------------

    async def execute_booking(
        self,
        request_id: str,
        booking_url: str | None = None,
        movie_name: str | None = None,
        cinema: str | None = None,
        date: str | None = None,
        city: str | None = None,
        time_window: tuple[int, int] | None = None,
        num_tickets: int = 2,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """
        Execute a booking — either from a direct URL or from a config request.

        This is the single entry point used by both the CLI (via ``run()``)
        and the alert-system bridge (``alert_to_agent.py``).

        Parameters
        ----------
        request_id : str
            Booking request ID (e.g. "req_001").
        booking_url : str or None
            If provided, the agent starts from this URL (skips search).
            If ``None``, falls back to ``run()`` which reads from config.
        movie_name, cinema, date, city, time_window, num_tickets :
            Used only when ``booking_url`` is provided.
        dry_run : bool
            If ``True``, stops before submitting payment.

        Returns
        -------
        dict
            Result dict with ``request_id``, ``movie_name``, ``success``,
            ``booking_id``, ``cinema``, ``show_time``, ``seats``,
            ``total_paid``, ``screenshot``, ``error``, ``dry_run``.
        """
        if not booking_url:
            # Fall back to the original full-flow run()
            logger.info("[%s] No direct URL — using full-flow run().", request_id)
            booking_requests = self.config.get("booking_requests", [])
            match = next(
                (r for r in booking_requests if r.get("id") == request_id),
                None,
            )
            if match is None:
                return self._build_error_result(
                    request_id, movie_name or "",
                    f"No booking request found with id: {request_id}",
                    dry_run,
                )
            return await self.run(match, dry_run=dry_run)

        # --- Build and run the direct task ---------------------------------
        logger.info("=" * 60)
        logger.info(
            "[ai:%s] 🎯 DIRECT booking: %s at %s on %s",
            request_id, movie_name, cinema, date,
        )
        logger.info("[ai:%s] URL: %s", request_id, booking_url)
        logger.info("[ai:%s] Dry‑run: %s", request_id, dry_run)
        logger.info("=" * 60)

        task = self._build_direct_task(
            booking_url=booking_url,
            movie_name=movie_name or "",
            cinema=cinema or "",
            date=date or "",
            city=city or "Coimbatore",
            time_window=time_window,
            num_tickets=num_tickets,
            dry_run=dry_run,
        )
        logger.info("[ai:%s] Direct task (first 200 chars):\n%s\n---",
                    request_id, task[:200])

        return await self._run_agent(task, request_id, movie_name or "", dry_run)

    # ------------------------------------------------------------------
    # Run (full-flow from config — kept for backward compat / --ai flag)
    # ------------------------------------------------------------------

    async def run(
        self,
        booking_request: dict[str, Any],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """
        Execute the AI-powered booking flow for a single request.

        Parameters
        ----------
        booking_request : dict
            A single booking request from ``config.json``.
        dry_run : bool
            If ``True``, stops before submitting payment.

        Returns
        -------
        dict
            A result dict compatible with ``booking_engine``:
            ``request_id``, ``movie_name``, ``success``, ``booking_id``,
            ``cinema``, ``show_time``, ``seats``, ``total_paid``,
            ``screenshot``, ``error``, ``dry_run``.
        """
        req_id: str = booking_request.get("id", "?")
        movie_name: str = booking_request.get("movie_name", "")

        logger.info("=" * 60)
        logger.info("[ai:%s] 🤖 AI-powered booking for: %s", req_id, movie_name)
        logger.info("[ai:%s] Dry‑run: %s", req_id, dry_run)
        logger.info("=" * 60)

        task = self.build_task(booking_request, dry_run=dry_run)
        logger.info("[ai:%s] Task (first 200 chars):\n%s\n---",
                    req_id, task[:200])

        return await self._run_agent(task, req_id, movie_name, dry_run)

    # ------------------------------------------------------------------
    # Shared agent runner
    # ------------------------------------------------------------------

    async def _run_agent(
        self,
        task: str,
        request_id: str,
        movie_name: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Launch a browser-use Agent, run the task, and return a result dict."""
        browser_profile = BrowserProfile(
            headless=False,
            disable_security=True,
            minimum_wait_page_load_time=1.0,
            wait_for_network_idle_page_load_time=3.0,
            wait_between_actions=0.5,
        )

        agent = Agent(
            task=task,
            llm=self.llm,
            browser_profile=browser_profile,
            use_vision=False,
            max_steps=35 if "STEP 1: Go to https://in.bookmyshow.com" in task else 25,
            max_failures=3,
            step_timeout=60_000,
        )

        # --- Run ---------------------------------------------------------
        history = None
        error_msg: Optional[str] = None
        try:
            logger.info("[ai:%s] 🚀 Starting browser-use agent …", request_id)
            history = await agent.run()
            # Keep the browser session alive — the OTP page is still open
            self._browser_session = agent.browser_session
            final = history.final_result()
            logger.info("[ai:%s] ✅ Agent finished. Final result: %s",
                        request_id, final[:500] if final else "(none)")
        except Exception as exc:
            logger.error("[ai:%s] ❌ Agent error: %s", request_id, exc)
            error_msg = str(exc)

        screenshot_path = await self._save_final_screenshot(request_id)

        if error_msg:
            await self._close_browser_session(request_id)
            return self._build_error_result(
                request_id, movie_name, error_msg, dry_run,
            )

        final_text = history.final_result() if history else ""

        if dry_run:
            logger.info("[ai:%s] 🔍 Dry‑run complete.", request_id)
            await self._close_browser_session(request_id)
            return {
                "request_id": request_id,
                "movie_name": movie_name,
                "success": False,
                "booking_id": None,
                "cinema": None,
                "show_time": None,
                "seats": None,
                "total_paid": None,
                "screenshot": screenshot_path,
                "error": None,
                "dry_run": True,
            }

        # --- OTP detection & auto-fill ------------------------------------
        otp_filled = False
        if self._needs_otp(final_text):
            logger.info("[ai:%s] 🔐 OTP page detected — attempting auto-fill…", request_id)
            otp_filled = await self._auto_fill_otp(request_id)

        # --- Close browser session -----------------------------------------
        await self._close_browser_session(request_id)

        # --- Determine success --------------------------------------------
        success = any(
            kw in (final_text or "").lower()
            for kw in ("confirmed", "successful", "booking", "thank you")
        )
        return {
            "request_id": request_id,
            "movie_name": movie_name,
            "success": success,
            "booking_id": final_text[:100] if final_text else None,
            "cinema": None,
            "show_time": None,
            "seats": None,
            "total_paid": None,
            "screenshot": screenshot_path,
            "error": (
                None if success
                else ("OTP auto-filled — check result" if otp_filled
                      else "Could not confirm booking success.")
            ),
            "dry_run": False,
        }

    # ------------------------------------------------------------------
    # OTP detection & auto-fill
    # ------------------------------------------------------------------

    @staticmethod
    def _needs_otp(final_text: str) -> bool:
        """
        Check whether the agent's final result indicates an OTP is needed.

        The main agent is instructed to report::

            "Card details submitted. Waiting for OTP page."

        or similar when it reaches the bank verification step.
        """
        if not final_text:
            return False
        text_lower = final_text.lower()
        otp_keywords = ["otp", "verify", "verification", "one time password",
                        "enter the code", "authentication"]
        return any(kw in text_lower for kw in otp_keywords)

    async def _auto_fill_otp(self, request_id: str) -> bool:
        """
        Poll email for bank OTP and run a short follow-up agent to fill it.

        Returns ``True`` if the OTP was found and filling was attempted,
        ``False`` otherwise.
        """
        # --- Check OTP relay configuration --------------------------------
        try:
            creds = self._load_credentials()
        except RuntimeError:
            logger.warning("[ai:%s] No credentials — cannot auto-fill OTP.", request_id)
            return False

        otp_config = creds.get("otp_relay")
        if not otp_config or not otp_config.get("email") or not otp_config.get("app_password"):
            logger.warning(
                "[ai:%s] ⚠️  OTP relay not configured. "
                "Run 'python setup_creds.py' to set it up. "
                "Waiting 120s for manual OTP entry…",
                request_id,
            )
            # Give the user 120 seconds to manually enter OTP
            await asyncio.sleep(120)
            return False

        if not _OTP_AVAILABLE:
            logger.error("[ai:%s] ❌ otp_relay module not available.", request_id)
            return False

        # --- Poll email for OTP ------------------------------------------
        logger.info(
            "[ai:%s] 📬 Starting email OTP relay: %s",
            request_id, otp_config["email"],
        )
        relay = EmailOTPRelay(
            email_addr=otp_config["email"],
            app_password=otp_config["app_password"],
        )
        otp = relay.poll_for_otp(timeout=120, interval=5)

        if not otp:
            logger.warning(
                "[ai:%s] ⏰ No OTP received via email within 120s. "
                "The user may need to enter it manually.",
                request_id,
            )
            # Give 60 more seconds for manual entry
            await asyncio.sleep(60)
            return False

        logger.info("[ai:%s] 📩 OTP received: %s", request_id, otp)

        # --- Get the EXISTING page — try multiple approaches -----------
        # browser-use closes its CDP session after agent.run(), so
        # get_current_page() may return None.  We use fallbacks to reach
        # the still-open Chrome tab.
        page = None

        # Method 1: Get from browser_session's CDP session --------------
        try:
            cdp_session = getattr(self._browser_session, 'cdp_client', None)
            if cdp_session is not None and hasattr(cdp_session, '_page'):
                page = cdp_session._page
                logger.info(
                    "[ai:%s] 📱 Got page via CDP session", request_id,
                )
        except Exception as exc:
            logger.debug("[ai:%s] Method 1 failed: %s", request_id, exc)

        # Method 2: Get from the Playwright browser context -------------
        if page is None:
            try:
                browser = getattr(self._browser_session, '_browser', None)
                if browser is not None:
                    contexts = getattr(browser, 'contexts', [])
                    if contexts:
                        pages = getattr(contexts[0], 'pages', [])
                        if pages:
                            page = pages[-1]  # last active page
                            logger.info(
                                "[ai:%s] 📱 Got page via browser context: %s",
                                request_id,
                                getattr(page, 'url', 'unknown'),
                            )
            except Exception as exc:
                logger.debug("[ai:%s] Method 2 failed: %s", request_id, exc)

        # Method 3: Connect via CDP URL ---------------------------------
        if page is None:
            try:
                cdp_url = getattr(self._browser_session, '_cdp_url', None)
                if cdp_url:
                    from playwright.async_api import async_playwright as _async_pw
                    pw_instance = await _async_pw().start()
                    cdp_browser = await pw_instance.chromium.connect_over_cdp(cdp_url)
                    cdp_contexts = cdp_browser.contexts
                    if cdp_contexts:
                        cdp_pages = cdp_contexts[0].pages
                        if cdp_pages:
                            page = cdp_pages[-1]
                            logger.info(
                                "[ai:%s] 📱 Got page via CDP connection: %s",
                                request_id,
                                getattr(page, 'url', 'unknown'),
                            )
            except Exception as exc:
                logger.debug("[ai:%s] Method 3 failed: %s", request_id, exc)

        # Method 4: Start a mini agent to resurrect the session ----------
        if page is None:
            try:
                from browser_use import Agent as MiniAgent
                mini_task = (
                    "Report the current page URL. Do NOT navigate anywhere."
                )
                mini_agent = MiniAgent(task=mini_task, llm=self.llm)
                await mini_agent.browser_session.start()
                page = await mini_agent.browser_session.get_current_page()
                if page is not None:
                    logger.info(
                        "[ai:%s] 📱 Got page via mini-agent: %s",
                        request_id,
                        getattr(page, 'url', 'None'),
                    )
            except Exception as exc:
                logger.debug("[ai:%s] Method 4 failed: %s", request_id, exc)

        # --- Bail if no page -----------------------------------------------
        if page is None:
            logger.error(
                "[ai:%s] ❌ Could not access browser page. OTP fill failed.",
                request_id,
            )
            return False

        # --- Fill OTP directly via JavaScript --------------------------
        try:
            logger.info(
                "[ai:%s] 📱 Filling OTP directly on current page: %s",
                request_id, otp,
            )

            # Check current URL (useful for debugging bank redirects)
            try:
                current_url = await page.evaluate("window.location.href")
                logger.info(
                    "[ai:%s] 📱 Current page URL: %s", request_id, current_url,
                )
                if any(kw in current_url.lower()
                       for kw in ('bank', 'secure', 'acs', '3ds')):
                    logger.info(
                        "[ai:%s] 📱 Detected bank 3D Secure page — "
                        "looking for OTP input...", request_id,
                    )
            except Exception as exc:
                logger.debug("[ai:%s] URL check failed: %s", request_id, exc)

            # Let the page stabilise
            await page.wait_for_timeout(3000)

            # Step 1: Find and fill the OTP input via JavaScript ---------
            filled = await page.evaluate(f"""
                (function() {{
                    // Check main document inputs
                    var inputs = document.querySelectorAll('input');
                    for (var i = 0; i < inputs.length; i++) {{
                        var inp = inputs[i];
                        if (inp.type === 'password' || inp.type === 'text' ||
                            inp.type === 'number' || inp.inputMode === 'numeric') {{
                            if (inp.offsetParent !== null) {{
                                inp.focus();
                                inp.value = '{otp}';
                                inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                                inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                                return 'filled: ' + inp.type;
                            }}
                        }}
                    }}
                    // Check iframes (common for 3D Secure bank pages)
                    var iframes = document.querySelectorAll('iframe');
                    for (var j = 0; j < iframes.length; j++) {{
                        try {{
                            var doc = iframes[j].contentDocument ||
                                      iframes[j].contentWindow.document;
                            var frameInputs = doc.querySelectorAll('input');
                            for (var k = 0; k < frameInputs.length; k++) {{
                                var fInp = frameInputs[k];
                                if (fInp.offsetParent !== null &&
                                    (fInp.type === 'password' ||
                                     fInp.type === 'text' ||
                                     fInp.type === 'number')) {{
                                    fInp.focus();
                                    fInp.value = '{otp}';
                                    fInp.dispatchEvent(new Event('input', {{bubbles: true}}));
                                    return 'filled in iframe';
                                }}
                            }}
                        }} catch(e) {{}}
                    }}
                    return 'no input found';
                }})()
            """)
            logger.info(
                "[ai:%s] 📱 JS fill result: %s", request_id, filled,
            )

            # Step 2: Click Submit/Verify via JavaScript ------------------
            await page.wait_for_timeout(500)
            clicked = await page.evaluate("""
                (function() {
                    var buttons = document.querySelectorAll(
                        'button, input[type="submit"]'
                    );
                    for (var i = 0; i < buttons.length; i++) {
                        var btn = buttons[i];
                        var text = (btn.textContent || btn.value || '')
                                   .toLowerCase();
                        if (text.indexOf('submit') >= 0 ||
                            text.indexOf('verify') >= 0 ||
                            text.indexOf('confirm') >= 0 ||
                            text.indexOf('pay') >= 0 ||
                            text.indexOf('continue') >= 0) {
                            if (btn.offsetParent !== null) {
                                btn.click();
                                return 'clicked: ' + text.substring(0, 30);
                            }
                        }
                    }
                    return 'no button found';
                })()
            """)
            logger.info(
                "[ai:%s] 📱 Submit click result: %s", request_id, clicked,
            )

            # Step 3: Wait for confirmation --------------------------------
            await page.wait_for_timeout(5000)

            # Step 4: Save confirmation screenshot --------------------------
            os.makedirs("screenshots", exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = (
                f"screenshots/booking_confirm_{request_id}_{ts}.png"
            )
            await page.screenshot(path=screenshot_path)
            logger.info(
                "[ai:%s] 📱 Confirmation screenshot saved: %s",
                request_id, screenshot_path,
            )

            # Step 5: Check for success ------------------------------------
            page_content = await page.content()
            success_keywords = [
                "booking confirmed", "successful", "ticket",
                "booking id", "thank you", "payment successful",
            ]
            if any(kw in page_content.lower() for kw in success_keywords):
                logger.info(
                    "[ai:%s] ✅ OTP submitted — booking appears confirmed!",
                    request_id,
                )
            else:
                logger.warning(
                    "[ai:%s] ⚠️  OTP submitted but could not confirm status.",
                    request_id,
                )

            return True

        except Exception as exc:
            logger.error(
                "[ai:%s] ❌ Direct OTP fill failed: %s", request_id, exc,
            )
            return False

    async def _close_browser_session(self, request_id: str) -> None:
        """Close the browser session if it's still open."""
        if self._browser_session is None:
            return
        try:
            await self._browser_session.close()
            logger.info("[ai:%s] 🧹 Browser session closed.", request_id)
        except Exception as exc:
            logger.warning(
                "[ai:%s] ⚠️  Error closing browser session: %s",
                request_id, exc,
            )
        finally:
            self._browser_session = None

    @staticmethod
    def _build_error_result(
        request_id: str,
        movie_name: str,
        error: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        return {
            "request_id": request_id,
            "movie_name": movie_name,
            "success": False,
            "booking_id": None,
            "cinema": None,
            "show_time": None,
            "seats": None,
            "total_paid": None,
            "screenshot": None,
            "error": error,
            "dry_run": dry_run,
        }

    # ------------------------------------------------------------------
    # Screenshot helper
    # ------------------------------------------------------------------

    async def _save_final_screenshot(self, request_id: str) -> Optional[str]:
        """Save a final snapshot of whatever page the agent landed on."""
        try:
            os.makedirs("screenshots", exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            note_path = f"screenshots/ai_booking_{request_id}_{ts}.txt"
            with open(note_path, "w") as f:
                f.write(
                    f"AI agent completed at {datetime.now().isoformat()}\n"
                    f"Request ID: {request_id}\n"
                    f"Per-step screenshots are in the agent history.\n"
                )
            logger.info(
                "[ai:%s] Completion note saved: %s", request_id, note_path
            )
            return note_path
        except Exception as exc:
            logger.warning(
                "[ai:%s] Could not save screenshot note: %s", request_id, exc
            )
            return None


# ---------------------------------------------------------------------------
# Quick self-test (run directly)
# ---------------------------------------------------------------------------

async def _self_test() -> None:
    """Smoke-test the AI booking agent without actually opening a browser."""
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

    print("=" * 60)
    print("AI Booking Agent — Task Builder Test")
    print("=" * 60)

    bookings = config.get("booking_requests", [])
    if not bookings:
        print("No booking requests in config.")
        return

    # Test task building (no browser needed).
    try:
        agent = AIBrowserBookingAgent(config)
        print(f"✅ LLM configured: {type(agent.llm).__name__}")
    except RuntimeError as exc:
        print(f"⚠️  LLM not available: {exc}")
        print("   (Task builder still works without an API key)")
        # Create agent without LLM just to test build_task.
        agent = object.__new__(AIBrowserBookingAgent)
        agent.config = config
        agent.credentials_file = "credentials.enc"
        agent._credentials = None

    for req in bookings:
        print(f"\n--- Request: {req.get('id')} — {req.get('movie_name')} ---")
        try:
            task = agent.build_task(req, dry_run=True)
            print(task)
        except Exception as exc:
            print(f"❌ Error building task: {exc}")

    print("\n" + "=" * 60)
    print("✅ Task builder test complete.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(_self_test())
