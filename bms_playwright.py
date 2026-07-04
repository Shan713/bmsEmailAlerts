"""
BookMyShow automation module using Playwright.

Provides a BMSPlaywrightAutomator class for browser automation with
stealth evasions, show search, seat selection, and gift-card payment.

Usage:
    import asyncio
    from bms_playwright import BMSPlaywrightAutomator

    async def main():
        config = json.load(open("config.json"))
        bms = BMSPlaywrightAutomator(config)
        browser, context, page = await bms.start()
        try:
            shows = await bms.find_shows(page, "Coolie", "2025-08-15", (18, 22))
            for s in shows:
                print(s)
        finally:
            await browser.close()

    asyncio.run(main())
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class BMSAutomationError(Exception):
    """Generic error for BMS automation failures."""


class PaymentPageNotFound(BMSAutomationError):
    """Raised when the payment-options page does not appear within the
    expected timeout after seat selection."""

    def __init__(self, message: str = "Payment options page did not load within 10 seconds."):
        super().__init__(message)


class MissingGiftCardCredentials(BMSAutomationError):
    """Raised when gift card details (e‑code + PIN) are not present in
    stored credentials."""

    def __init__(self, message: str = (
        "Gift card credentials not found. "
        "Run 'python setup_creds.py' and store your "
        "BMS Gift Card e‑code and PIN."
    )):
        super().__init__(message)


class InsufficientGiftCardBalance(BMSAutomationError):
    """Raised when the gift card does not cover the ticket cost."""

    def __init__(self, balance: float, required: float):
        self.balance = balance
        self.required = required
        shortfall = required - balance
        super().__init__(
            f"Insufficient gift card balance: ₹{balance:.2f} available, "
            f"₹{required:.2f} required (shortfall: ₹{shortfall:.2f})."
        )


class PaymentFailed(BMSAutomationError):
    """Raised when the final payment confirmation does not succeed."""

    def __init__(self, reason: str = "Payment failed — could not confirm success."):
        self.reason = reason
        super().__init__(reason)


class PaymentMethodNotFound(BMSAutomationError):
    """Raised when the requested payment method is not available on the page."""

    def __init__(self, message: str = "Payment method not found on payment page."):
        super().__init__(message)


class MissingUPIID(BMSAutomationError):
    """Raised when UPI ID is not set in stored credentials."""

    def __init__(self, message: str = (
        "UPI ID not found. "
        "Run 'python setup_creds.py' and enter your UPI ID "
        "(e.g., username@okhdfcbank)."
    )):
        super().__init__(message)


class PaymentTimeout(BMSAutomationError):
    """Raised when the UPI payment approval times out."""

    def __init__(self, message: str = "UPI payment approval timed out after 120 seconds."):
        super().__init__(message)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BMS_BASE_URL = "https://in.bookmyshow.com"

# Fallback user-agent used when config.json has no user_agents list.
_FALLBACK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Regex for parsing BookMyShow booking URLs.
# Matches: /movies/{city}/{movie-slug}/buytickets/{unique-code}/{YYYYMMDD}
# Examples:
#   /movies/chennai/spider-man-brand-new-day/buytickets/ET00447840/20260802
#   /movies/chennai/spider-man-brand-new-day-epiq-3d/buytickets/ET00505581/20260730
_BOOKING_URL_RE = re.compile(
    r"https?://in\.bookmyshow\.com"
    r"/movies/"
    r"(?P<city>[^/]+)"           # city slug (e.g. "chennai")
    r"/"
    r"(?P<movie_slug>.+?)"       # movie slug (may contain hyphens and digits, e.g. "spider-man-brand-new-day-epiq-3d")
    r"/buytickets/"
    r"(?P<unique_code>ET\d+)"    # unique movie code (e.g. "ET00447840")
    r"/"
    r"(?P<date>\d{8})"           # date in YYYYMMDD format
    r"(?:/.*)?$"                 # optional trailing path/query
)

# How long to wait (seconds) for elements before giving up.
_DEFAULT_TIMEOUT = 15_000  # ms
_NAVIGATION_TIMEOUT = 30_000  # ms


# ---------------------------------------------------------------------------
# Helper: slugify a movie name
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    """Convert a movie name to a URL-friendly slug (best-effort)."""
    slug = name.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug


# ---------------------------------------------------------------------------
# Utility: parse a BMS booking URL into its components
# ---------------------------------------------------------------------------

def parse_booking_url(url: str) -> Optional[Dict[str, str]]:
    """
    Parse a BookMyShow booking URL into its constituent parts.

    Expected format:
        https://in.bookmyshow.com/movies/{city}/{movie-slug}/buytickets/{unique-code}/{YYYYMMDD}

    Parameters
    ----------
    url : str
        Full BMS booking URL.

    Returns
    -------
    dict or None
        Keys: ``city``, ``movie_slug``, ``unique_code``, ``date`` (YYYYMMDD string).
        Returns ``None`` if the URL does not match the expected pattern.

    Examples
    --------
    >>> parse_booking_url("https://in.bookmyshow.com/movies/chennai/spider-man-brand-new-day/buytickets/ET00447840/20260802")
    {'city': 'chennai', 'movie_slug': 'spider-man-brand-new-day', 'unique_code': 'ET00447840', 'date': '20260802'}

    >>> parse_booking_url("https://in.bookmyshow.com/movies/chennai/spider-man-brand-new-day-epiq-3d/buytickets/ET00505581/20260730")
    {'city': 'chennai', 'movie_slug': 'spider-man-brand-new-day-epiq-3d', 'unique_code': 'ET00505581', 'date': '20260730'}
    """
    match = _BOOKING_URL_RE.match(url)
    if not match:
        logger.debug("URL does not match BMS booking pattern: %s", url)
        return None
    return {
        "city": match.group("city"),
        "movie_slug": match.group("movie_slug"),
        "unique_code": match.group("unique_code"),
        "date": match.group("date"),
    }


# ---------------------------------------------------------------------------
# Helper: filter shows by cinema name
# ---------------------------------------------------------------------------

def _apply_cinema_filter(
    shows: List[Dict[str, Any]],
    cinema_filter: List[str],
) -> List[Dict[str, Any]]:
    """
    Filter *shows* to only those whose ``venue`` name contains any of the
    strings in *cinema_filter* (case‑insensitive substring match).
    """
    filtered = [
        s for s in shows
        if any(cf.lower() in (s.get("venue") or "").lower() for cf in cinema_filter)
    ]
    logger.debug(
        "[cinema_filter] %d → %d shows after filtering by %s",
        len(shows), len(filtered), cinema_filter,
    )
    return filtered


# ---------------------------------------------------------------------------
# Main automator class
# ---------------------------------------------------------------------------

class BMSPlaywrightAutomator:
    """
    Playwright-based BookMyShow automation.

    Parameters
    ----------
    config : dict
        The parsed contents of ``config.json``.
    credentials_file : str
        Path to the encrypted credentials file (default ``credentials.enc``).
    """

    def __init__(
        self,
        config: Dict[str, Any],
        credentials_file: str = "credentials.enc",
    ) -> None:
        self.config = config
        self.credentials_file = credentials_file

        # Lazy-loaded references
        self._playwright = None
        self._browser = None
        self._context = None
        self._credentials: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_setting(self, *keys: str, default: Any = None) -> Any:
        """Walk nested ``config`` dict by *keys*, returning *default* on
        any missing key."""
        node = self.config
        for k in keys:
            if isinstance(node, dict):
                node = node.get(k)
            else:
                return default
        return node if node is not None else default

    def _pick_user_agent(self) -> str:
        """Return a random user-agent from config if available, else a
        realistic fallback."""
        agents: Optional[List[str]] = self._get_setting("system_settings", "user_agents")
        if agents and isinstance(agents, list) and len(agents) > 0:
            return random.choice(agents)
        return _FALLBACK_UA

    @property
    def headless(self) -> bool:
        """Whether to run the browser headless (from config)."""
        return bool(self._get_setting("system_settings", "headless_browser", default=False))

    def _load_credentials(self) -> Dict[str, Any]:
        """Load and decrypt credentials via the existing SecureCredentialManager."""
        if self._credentials is not None:
            return self._credentials

        # Import here so the module can be imported even without
        # credential_manager available (graceful degradation).
        try:
            from credential_manager import SecureCredentialManager
        except ImportError as exc:
            raise BMSAutomationError(
                "Cannot import credential_manager. Is credential_manager.py present?"
            ) from exc

        mgr = SecureCredentialManager(credentials_file=self.credentials_file)
        creds = mgr.get_credentials()
        if creds is None:
            raise BMSAutomationError(
                "No credentials found. Run 'python setup_creds.py' first."
            )
        self._credentials = creds
        return creds

    @property
    def city(self) -> str:
        """Best-effort city from config (user_profile.city or first booking request)."""
        city = self._get_setting("user_profile", "city")
        if city:
            return city
        bookings = self._get_setting("booking_requests", default=[])
        if bookings and isinstance(bookings, list) and len(bookings) > 0:
            return bookings[0].get("city", "coimbatore")
        return "coimbatore"

    def _city_slug(self) -> str:
        return self.city.strip().lower().replace(" ", "-")

    # ------------------------------------------------------------------
    # start() — launch browser with stealth evasions
    # ------------------------------------------------------------------

    async def start(self) -> Tuple[Any, Any, Any]:
        """
        Launch a Chromium browser with stealth evasions.

        Uses a standard non‑persistent context — no saved sessions needed
        since BMS does not require login before checkout.

        Returns
        -------
        (browser, context, page) : tuple
            The Playwright browser, BrowserContext, and a ready-to-use Page.
        """
        logger.info("Starting Playwright-based BMS automator …")

        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BMSAutomationError(
                "Playwright is not installed. Run:\n"
                "    pip install playwright\n"
                "    playwright install chromium\n"
            ) from exc

        try:
            try:
                from playwright_stealth import stealth_async  # type: ignore[import-untyped]
                _stealth_available = True
                _stealth_apply = stealth_async
            except ImportError:
                # playwright-stealth >= 2.0 uses the Stealth class.
                from playwright_stealth import Stealth  # type: ignore[import-untyped]
                _stealth_available = True
                _stealth_apply = Stealth().apply_stealth_async
            logger.info("playwright-stealth loaded — evasion enabled.")
        except ImportError:
            _stealth_available = False
            _stealth_apply = None
            logger.warning(
                "playwright-stealth not installed. Bot detection evasion will be "
                "weaker. Install it with:  pip install playwright-stealth"
            )

        self._playwright = await async_playwright().start()

        user_agent = self._pick_user_agent()

        logger.info(
            "Launching Chromium (headless=%s)",
            self.headless,
        )
        logger.debug("User-Agent: %s", user_agent)

        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
            ],
        )

        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=user_agent,
            permissions=[],
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )

        page = await self._context.new_page()

        # Apply stealth patches to the page.
        if _stealth_available and _stealth_apply is not None:
            await _stealth_apply(page)
            logger.debug("Stealth patches applied to page.")

        # Set default timeouts.
        page.set_default_timeout(_DEFAULT_TIMEOUT)
        page.set_default_navigation_timeout(_NAVIGATION_TIMEOUT)

        logger.info("Browser ready.  Page title: %s", await page.title())
        return self._browser, self._context, page

    # ------------------------------------------------------------------
    # find_shows(page, movie_name, date, time_range, *, booking_url=None)
    # ------------------------------------------------------------------

    async def find_shows(
        self,
        page: "playwright.async_api.Page",
        movie_name: str,
        date: str,
        time_range: Tuple[int, int],
        *,
        booking_url: Optional[str] = None,
        cinema_filter: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search for a movie on BookMyShow and scrape matching showtimes.

        Supports two modes:

        **Direct URL mode** (``booking_url`` is provided):
            Navigates straight to the booking page.  The URL is parsed to
            extract city, movie slug, and the unique ``ET…`` code.  The
            date embedded in the URL is used as the effective show date
            **unless** *date* is non-empty, in which case *date* overrides
            the URL date.  No search or date-picker interaction is needed
            because the booking page already lists cinemas and showtimes.

        **Search mode** (``booking_url`` is ``None``, the default):
            Searches from the homepage by *movie_name*, selects from
            suggestions, navigates to the booking page, picks the date,
            and scrapes the results.  If the resulting page URL contains
            a ``buytickets/ET…`` pattern the unique code is captured and
            logged for future direct-navigation use.

        Parameters
        ----------
        page : Page
            An active Playwright page (should already be logged in).
        movie_name : str
            The movie to search for (e.g. ``"Coolie"``).  Used for
            logging in direct-URL mode.
        date : str
            Desired show date in ``YYYY-MM-DD`` format.  In direct-URL
            mode this may be an empty string to fall back on the URL date.
        time_range : tuple
            ``(start_hour, end_hour)`` in 24-hour format, inclusive of
            *start_hour* and exclusive of *end_hour*.
        booking_url : str, optional
            A full BMS booking URL to jump to directly.  When provided,
            the search flow is skipped entirely.
        cinema_filter : list[str], optional
            If provided, only shows whose ``venue`` name contains any of
            these strings (case‑insensitive) will be returned.  This is
            a post‑scrape filter applied to results from both modes.

        Returns
        -------
        list[dict]
            Each dict has keys: ``venue``, ``show_time``, ``show_url``,
            ``price`` (may be ``None``).  An additional key
            ``_unique_code`` is included when a BMS ``ET…`` code is
            discovered (useful for constructing future direct URLs).
        """
        start_hour, end_hour = time_range

        # --- Direct-URL mode -------------------------------------------------
        if booking_url:
            shows = await self._find_shows_from_url(
                page, movie_name, date, start_hour, end_hour, booking_url,
            )
            if cinema_filter:
                shows = _apply_cinema_filter(shows, cinema_filter)
            return shows

        # =====================================================================
        # Search mode (original flow)
        # =====================================================================
        city_slug = self._city_slug()

        logger.info(
            "[search] Looking for '%s' in %s on %s, time window %02d:00–%02d:00",
            movie_name, self.city, date, start_hour, end_hour,
        )

        # --- Step 1: search for the movie ----------------------------------
        search_url = f"{BMS_BASE_URL}/"
        await page.goto(search_url, wait_until="domcontentloaded", timeout=_NAVIGATION_TIMEOUT)
        await page.wait_for_timeout(2000)

        # Dismiss any popups / location prompts.
        await self._dismiss_overlays(page)

        # Locate and fill the search box.
        search_selectors = [
            "input[placeholder*='Search' i]",
            "input[placeholder*='search' i]",
            "[data-testid='search-input']",
            "input[type='text']",
            ".search-input input",
            "#search-input",
        ]
        search_box = None
        for sel in search_selectors:
            try:
                search_box = await page.wait_for_selector(sel, timeout=3000)
                if search_box:
                    if await search_box.is_visible():
                        logger.debug("Found search box: %s", sel)
                        break
                    search_box = None
            except Exception:
                continue

        if search_box is None:
            logger.warning("Search box not found; trying direct URL navigation.")
            return await self._find_shows_direct(page, movie_name, date, time_range)

        await search_box.click()
        await page.wait_for_timeout(300)
        await search_box.fill("")
        await search_box.type(movie_name, delay=80)
        await page.wait_for_timeout(1500)

        # Click the first suggestion that matches the movie name.
        suggestion_selectors = [
            "[data-testid='search-suggestion']",
            ".search-suggestion",
            ".sc-gJqSRn",
            "li[role='option']",
            "ul[class*='search'] li",
            "div[class*='suggestion']",
        ]
        clicked_suggestion = False
        for sel in suggestion_selectors:
            try:
                items = await page.query_selector_all(sel)
                for item in items:
                    text = (await item.inner_text()).strip().lower()
                    if movie_name.lower() in text:
                        logger.info("Clicking suggestion: %s", text[:80])
                        await item.click()
                        await page.wait_for_timeout(3000)
                        clicked_suggestion = True
                        break
                if clicked_suggestion:
                    break
            except Exception:
                continue

        if not clicked_suggestion:
            logger.info("No suggestion found; submitting search via Enter.")
            await search_box.press("Enter")
            await page.wait_for_timeout(3000)

        # --- Step 2: capture unique code from the current URL ---------------
        self._log_parsed_url(page.url)

        # --- Step 3: navigate to the movie's booking page ------------------
        book_selectors = [
            "a:has-text('Book')",
            "button:has-text('Book')",
            "[data-testid='book-btn']",
            "a:has-text('Book Tickets')",
            "text=/Book Tickets/i",
        ]
        for sel in book_selectors:
            try:
                btn = await page.wait_for_selector(sel, timeout=3000)
                if btn:
                    logger.info("Clicking book button.")
                    await btn.click()
                    await page.wait_for_timeout(3000)
                    break
            except Exception:
                continue

        # Re-check URL after navigation to booking page.
        self._log_parsed_url(page.url)

        # --- Step 4: select date -------------------------------------------
        await self._select_date(page, date)

        # --- Step 5: scrape showtimes --------------------------------------
        shows = await self._scrape_shows(page, start_hour, end_hour)
        # Attach unique code if the current URL has one.
        parsed = parse_booking_url(page.url)
        if parsed:
            for s in shows:
                s.setdefault("_unique_code", parsed["unique_code"])
        if cinema_filter:
            shows = _apply_cinema_filter(shows, cinema_filter)
        return shows

    async def _find_shows_direct(
        self,
        page: "playwright.async_api.Page",
        movie_name: str,
        date: str,
        time_range: Tuple[int, int],
    ) -> List[Dict[str, Any]]:
        """Fallback: navigate directly to a guessed city+movie URL."""
        start_hour, end_hour = time_range
        movie_slug = _slugify(movie_name)
        city_slug = self._city_slug()

        urls_to_try = [
            f"{BMS_BASE_URL}/{city_slug}/movies/{movie_slug}",
            f"{BMS_BASE_URL}/movies/{city_slug}/{movie_slug}",
            f"{BMS_BASE_URL}/{city_slug}/movies/{movie_slug}/buytickets",
        ]

        for url in urls_to_try:
            logger.info("Trying direct URL: %s", url)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=_NAVIGATION_TIMEOUT)
                await page.wait_for_timeout(3000)
            except Exception as exc:
                logger.warning("Failed to load %s: %s", url, exc)
                continue

            # Check if we landed on a meaningful page.
            if "Page Not Found" in await page.title() or "404" in page.url:
                logger.warning("Page not found at %s", url)
                continue

            await self._dismiss_overlays(page)
            await self._select_date(page, date)
            shows = await self._scrape_shows(page, start_hour, end_hour)
            if shows:
                return shows

        logger.warning("Direct URL navigation did not yield any shows.")
        return []

    # ------------------------------------------------------------------
    # _find_shows_from_url — jump directly to a booking URL
    # ------------------------------------------------------------------

    async def _find_shows_from_url(
        self,
        page: "playwright.async_api.Page",
        movie_name: str,
        date: str,
        start_hour: int,
        end_hour: int,
        booking_url: str,
    ) -> List[Dict[str, Any]]:
        """
        Navigate directly to a BMS booking URL and scrape showtimes.

        The date embedded in the URL is parsed; if *date* is non-empty it
        overrides the URL date.
        """
        parsed = parse_booking_url(booking_url)
        if parsed is None:
            logger.error(
                "Booking URL does not match expected pattern: %s", booking_url,
            )
            return []

        logger.info(
            "[direct-url] Parsed booking URL — city=%s  movie_slug=%s  "
            "unique_code=%s  date(YYYYMMDD)=%s",
            parsed["city"], parsed["movie_slug"],
            parsed["unique_code"], parsed["date"],
        )

        # Resolve the effective date: caller override > URL date.
        effective_date = date
        if not effective_date:
            # Convert YYYYMMDD → YYYY-MM-DD
            raw = parsed["date"]
            effective_date = f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
            logger.info("[direct-url] Using date from URL: %s", effective_date)
        else:
            logger.info("[direct-url] Using caller-provided date: %s (URL had %s)",
                        effective_date, parsed["date"])

        logger.info(
            "[direct-url] Navigating to '%s' — movie='%s' (slug=%s) on %s, "
            "time window %02d:00–%02d:00",
            booking_url, movie_name, parsed["movie_slug"],
            effective_date, start_hour, end_hour,
        )

        # Navigate.
        await page.goto(booking_url, wait_until="domcontentloaded", timeout=_NAVIGATION_TIMEOUT)
        await page.wait_for_timeout(3000)

        await self._dismiss_overlays(page)

        # The booking page already lists cinemas/showtimes for the given
        # date, so we skip the date-picker.  Scrape directly.
        shows = await self._scrape_shows(page, start_hour, end_hour)

        # Attach the unique code to every result.
        unique_code = parsed["unique_code"]
        for s in shows:
            s["_unique_code"] = unique_code
            s["_city"] = parsed["city"]
            s["_movie_slug"] = parsed["movie_slug"]

        logger.info(
            "[direct-url] Found %d showtimes for %s (%s).",
            len(shows), parsed["movie_slug"], unique_code,
        )
        return shows

    # ------------------------------------------------------------------
    # _log_parsed_url — capture the unique ET… code from a page URL
    # ------------------------------------------------------------------

    def _log_parsed_url(self, url: str) -> Optional[Dict[str, str]]:
        """
        Try to parse *url* as a BMS booking URL and log what was found.

        This is called from the search flow after navigating to a movie /
        booking page so the unique ``ET…`` code can be saved for future
        direct-URL jumps.
        """
        parsed = parse_booking_url(url)
        if parsed:
            logger.info(
                "[search] Captured unique code from URL: %s (movie=%s, city=%s, date=%s)",
                parsed["unique_code"], parsed["movie_slug"],
                parsed["city"], parsed["date"],
            )
        return parsed

    # ------------------------------------------------------------------
    # Internal: date selection
    # ------------------------------------------------------------------

    async def _select_date(self, page: "playwright.async_api.Page", date_str: str) -> None:
        """
        Click the date in the horizontal date-picker on the show listing page.

        *date_str* is ``YYYY-MM-DD``.
        """
        logger.info("Selecting date: %s", date_str)
        await page.wait_for_timeout(1500)
        await self._dismiss_overlays(page)

        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            logger.error("Invalid date format: %s (expected YYYY-MM-DD)", date_str)
            return

        # Build multiple textual representations.
        day_num = str(target_date.day)
        month_names = [
            "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
        ]
        month_abbr = month_names[target_date.month]
        full_month = target_date.strftime("%B")

        date_patterns = [
            f"{day_num} {month_abbr}",      # "15 Aug"
            f"{day_num} {full_month}",       # "15 August"
            f"{full_month} {day_num}",       # "August 15"
            f"{month_abbr} {day_num}",       # "Aug 15"
            target_date.strftime("%d %b %Y"),  # "15 Aug 2025"
            target_date.strftime("%d %B %Y"),  # "15 August 2025"
        ]

        date_el_selectors = [
            "[data-testid='date-item']",
            ".date-item",
            ".date-list a",
            ".date-picker a",
            "[class*='date'] a",
            "[class*='Date'] a",
            "a[href*='date']",
            "ul[class*='date'] li",
            "div[class*='date'] > div",
            ".slick-slide",
        ]

        for sel in date_el_selectors:
            try:
                elements = await page.query_selector_all(sel)
                for el in elements:
                    text = (await el.inner_text()).strip()
                    for pattern in date_patterns:
                        if pattern.lower() in text.lower():
                            logger.info("Clicking date element: %s", text[:40])
                            await el.click()
                            await page.wait_for_timeout(2000)
                            return
            except Exception:
                continue

        logger.warning("Could not select date %s via date-picker.", date_str)

    # ------------------------------------------------------------------
    # Internal: scrape shows from the current page
    # ------------------------------------------------------------------

    async def _scrape_shows(
        self,
        page: "playwright.async_api.Page",
        start_hour: int,
        end_hour: int,
    ) -> List[Dict[str, Any]]:
        """
        Extract showtimes from the current page that fall within
        [*start_hour*, *end_hour*).
        """
        shows: List[Dict[str, Any]] = []
        await page.wait_for_timeout(2000)

        # --- Strategy A: BMS often uses a venue-card layout ----------------
        venue_selectors = [
            "[data-testid='cinema-container']",
            ".cinema-container",
            "[class*='venue']",
            "[class*='cinema']",
            "[class*='theatre']",
            "li[class*='list']",
            ".listing-card",
        ]

        venue_els: List[Any] = []
        for sel in venue_selectors:
            venue_els = await page.query_selector_all(sel)
            if venue_els:
                logger.debug("Found %d venue elements via '%s'.", len(venue_els), sel)
                break

        if not venue_els:
            # Broader fallback: scrape the whole page for showtime patterns.
            logger.warning("No venue containers found; using whole-page fallback.")
            return await self._scrape_shows_fallback(page, start_hour, end_hour)

        current_url = page.url

        for venue_el in venue_els:
            try:
                venue_text = (await venue_el.inner_text()).strip()
            except Exception:
                continue

            if not venue_text:
                continue

            # Extract venue name (first meaningful line or an h3 / strong tag).
            venue_name: Optional[str] = None
            for tag in ["h3", "h4", "strong", "[class*='name']", "[class*='title']"]:
                try:
                    name_el = await venue_el.query_selector(tag)
                    if name_el:
                        venue_name = (await name_el.inner_text()).strip()
                        if venue_name:
                            break
                except Exception:
                    continue
            if venue_name is None:
                # Use the first line as the venue name.
                venue_name = venue_text.split("\n")[0].strip()
            if not venue_name:
                continue

            # Find showtime buttons / links within the venue container.
            time_selectors = [
                "a[href*='buytickets']",
                "a:has-text(':')",
                "button:has-text(':')",
                "[data-testid='showtime']",
                ".showtime-btn",
                ".showtime",
                "[class*='time']",
                "[class*='show']",
            ]
            time_els: List[Any] = []
            for tsel in time_selectors:
                time_els = await venue_el.query_selector_all(tsel)
                if time_els:
                    break

            for time_el in time_els:
                try:
                    time_text = (await time_el.inner_text()).strip()
                except Exception:
                    continue

                # Parse hour from the text.
                hour = self._extract_hour(time_text)
                if hour is None:
                    continue
                if not (start_hour <= hour < end_hour):
                    continue

                # Get the show URL.
                show_url: Optional[str] = None
                try:
                    href = await time_el.get_attribute("href")
                    if href:
                        show_url = href if href.startswith("http") else f"{BMS_BASE_URL}{href}"
                except Exception:
                    pass
                if show_url is None:
                    show_url = current_url

                # Try to get a price.
                price: Optional[float] = self._extract_price(venue_text)
                # Also look for a price near the time element.
                if price is None:
                    try:
                        parent_text = (await time_el.evaluate("el => el.parentElement?.innerText || ''")).strip()
                        price = self._extract_price(parent_text)
                    except Exception:
                        pass

                shows.append({
                    "venue": venue_name,
                    "show_time": time_text,
                    "show_url": show_url,
                    "price": price,
                })
                logger.debug(
                    "Found show: %s | %s | ₹%s",
                    venue_name, time_text, price,
                )

        logger.info("Scraped %d showtimes matching the criteria.", len(shows))
        return shows

    async def _scrape_shows_fallback(
        self,
        page: "playwright.async_api.Page",
        start_hour: int,
        end_hour: int,
    ) -> List[Dict[str, Any]]:
        """Whole-page regex-based fallback scraper."""
        shows: List[Dict[str, Any]] = []
        body_text = await page.inner_text("body")
        current_url = page.url

        # Pattern: look for time strings like "10:30 AM" or "18:45".
        time_pattern = re.compile(
            r"(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)?",
        )
        for match in time_pattern.finditer(body_text):
            raw_hour = int(match.group(1))
            minute_str = match.group(2)
            ampm = (match.group(3) or "").upper()
            hour = self._normalize_hour(raw_hour, ampm)

            if not (start_hour <= hour < end_hour):
                continue

            time_str = f"{raw_hour:02d}:{minute_str} {ampm}".strip()

            # Try to find surrounding context for a venue name.
            pos = match.start()
            context_window = body_text[max(0, pos - 200):pos + 200]
            # Heuristic: the venue name is often the line above the time.
            lines = context_window.split("\n")
            venue = "Unknown"
            for line in reversed(lines):
                line = line.strip()
                if line and not re.search(r"\d{1,2}:\d{2}", line) and len(line) > 3:
                    venue = line
                    break

            shows.append({
                "venue": venue,
                "show_time": time_str,
                "show_url": current_url,
                "price": self._extract_price(context_window),
            })

        # Deduplicate.
        seen = set()
        unique: List[Dict[str, Any]] = []
        for s in shows:
            key = (s["venue"], s["show_time"])
            if key not in seen:
                seen.add(key)
                unique.append(s)

        logger.info("Fallback scraper found %d showtimes.", len(unique))
        return unique

    # ------------------------------------------------------------------
    # Internal: overlay / popup dismissal
    # ------------------------------------------------------------------

    async def _dismiss_overlays(self, page: "playwright.async_api.Page") -> None:
        """Attempt to close common overlays, popups, and location prompts."""
        dismiss_selectors = [
            "button:has-text('No thanks')",
            "button:has-text('Not Now')",
            "button:has-text('Later')",
            "button:has-text('Skip')",
            "button:has-text('Close')",
            "[data-testid='close-btn']",
            ".close-btn",
            ".modal-close",
            "[aria-label='Close']",
            ".popup-close",
            "button:has-text('Maybe Later')",
            "div[class*='Notification'] button",
            "text=✕",
            "text=×",
        ]
        for sel in dismiss_selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    logger.debug("Dismissing overlay: %s", sel)
                    await el.click()
                    await page.wait_for_timeout(500)
            except Exception:
                continue

        # Press Escape to close any remaining modals.
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal: time parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_hour(text: str) -> Optional[int]:
        """Pull a 24-hour hour integer from a time string."""
        match = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)?", text)
        if not match:
            return None
        hour = int(match.group(1))
        ampm = (match.group(3) or "").upper()
        return BMSPlaywrightAutomator._normalize_hour(hour, ampm)

    @staticmethod
    def _normalize_hour(hour: int, ampm: str) -> int:
        """Convert a 12-hour hour + AM/PM to 24-hour."""
        if ampm == "AM":
            return 0 if hour == 12 else hour
        elif ampm == "PM":
            return 12 if hour == 12 else hour + 12
        return hour

    # ------------------------------------------------------------------
    # Internal: price parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_price(text: str) -> Optional[float]:
        """Extract a price (₹) from *text*."""
        match = re.search(r"[₹$]\s*([\d,]+(?:\.\d{1,2})?)", text)
        if match:
            numeric = re.sub(r"[^\d.]", "", match.group(1))
            try:
                return float(numeric)
            except ValueError:
                pass
        # Alternative: "Rs. 250" style.
        match = re.search(r"(?:Rs\.?|INR)\s*([\d,]+(?:\.\d{1,2})?)", text, re.IGNORECASE)
        if match:
            numeric = re.sub(r"[^\d.]", "", match.group(1))
            try:
                return float(numeric)
            except ValueError:
                pass
        return None

    # ==================================================================
    # Seat selection
    # ==================================================================

    async def select_best_seats(
        self,
        page: "playwright.async_api.Page",
        show: Dict[str, Any],
        num_tickets: Optional[int] = None,
        seat_pref: str = "center",
    ) -> Dict[str, Any]:
        """
        Navigate to the seat-selection page for *show*, pick the best
        available seats, and click them.

        Parameters
        ----------
        page : Page
            An active Playwright page.  Should be on the show-listing page
            (the page that lists cinemas/showtimes) so that clicking a
            showtime navigates to seat selection.
        show : dict
            A showtime entry returned by :meth:`find_shows`, with at least
            ``show_url``.  May also contain a ``_showtime_element``
            Playwright element handle if the caller captured it at listing
            time (the automator will click it to open seat selection).
        num_tickets : int, optional
            How many seats to select.  Defaults to
            ``config.user_profile.max_tickets`` (or 2 if unset).
        seat_pref : str
            **Ignored** — kept for backward compatibility.  Scoring always
            uses pure centre-proximity with contiguous-block preference.
            The seat map centre (midpoint of all rows × midpoint of all
            columns) is the ideal location.

        Returns
        -------
        dict
            Keys:

            ``selected_seats`` (list[str])
                Seat IDs that were clicked (e.g. ``["H8","H9"]``).
            ``total_price`` (float or None)
                Sum of individual seat prices if available.
            ``category`` (str or None)
                Seat category (GOLD, SILVER, …) the chosen seats belong to.
            ``attempted`` (bool)
                ``True`` if seats were found and clicked.
            ``error`` (str or None)
                Error description when ``attempted`` is ``False``.
        """
        if num_tickets is None:
            num_tickets = int(self._get_setting("user_profile", "max_tickets", default=2))

        logger.info(
            "[seats] Selecting %d seat(s) for '%s' at %s (pref=%s)",
            num_tickets, show.get("venue", "?"), show.get("show_time", "?"), seat_pref,
        )

        # --- 1. Navigate to seat map ------------------------------------
        await self._navigate_to_seat_selection(page, show)

        # --- 2. Wait for seat map to materialise -------------------------
        seat_map_ready = await self._wait_for_seat_map(page)
        if not seat_map_ready:
            return {
                "selected_seats": [],
                "total_price": None,
                "category": None,
                "attempted": False,
                "error": "Seat map did not load — no SVG/canvas/seat elements found.",
            }

        # --- 3. Scrape all seat elements --------------------------------
        seats = await self._scrape_seats(page)
        available = [s for s in seats if s.get("status") == "available"]
        logger.info(
            "[seats] Scraped %d total seats, %d available.",
            len(seats), len(available),
        )
        if not available:
            return {
                "selected_seats": [],
                "total_price": None,
                "category": None,
                "attempted": False,
                "error": f"No available seats found ({len(seats)} total, 0 available).",
            }

        # --- 4. Score & pick seats (centre-proximity, contiguous-block-first) -
        chosen = self._score_seats(available, num_tickets)

        if len(chosen) < num_tickets:
            logger.warning(
                "[seats] Only %d available seat(s) — wanted %d.",
                len(chosen), num_tickets,
            )

        # --- 5. Click the chosen seats ----------------------------------
        clicked = await self._click_seats(page, chosen)
        if not clicked:
            return {
                "selected_seats": [],
                "total_price": None,
                "category": None,
                "attempted": False,
                "error": "Failed to click the chosen seat elements.",
            }

        # --- 6. Click "Proceed" / "Continue" (don't pay yet) ------------
        await self._click_proceed_button(page)

        selected_ids = [s["id"] for s in chosen]
        total = sum(s.get("price", 0) or 0 for s in chosen)
        category = chosen[0].get("category") if chosen else None

        logger.info(
            "[seats] ✅ Selected %s — total ₹%s — category=%s",
            selected_ids, total, category,
        )
        return {
            "selected_seats": selected_ids,
            "total_price": total if total > 0 else None,
            "category": category,
            "attempted": True,
            "error": None,
        }

    # ------------------------------------------------------------------
    # _navigate_to_seat_selection
    # ------------------------------------------------------------------

    async def _navigate_to_seat_selection(
        self,
        page: "playwright.async_api.Page",
        show: Dict[str, Any],
    ) -> None:
        """
        Reach the seat map for *show*.  Tries, in order:

        1. Click a stored ``_showtime_element`` handle.
        2. Navigate to ``show_url`` directly.
        3. Find and click a showtime button on the current listing page
           whose text/time matches *show*.
        """
        # --- Option 1: pre-captured element handle -----------------------
        show_el = show.get("_showtime_element")
        if show_el is not None:
            try:
                logger.info("[seats] Clicking stored showtime element handle.")
                await show_el.click()
                await page.wait_for_timeout(3000)
                return
            except Exception as exc:
                logger.warning("[seats] Stored element click failed: %s", exc)

        # --- Option 2: direct URL navigation -----------------------------
        show_url = show.get("show_url")
        if show_url:
            logger.info("[seats] Navigating to show URL: %s", show_url)
            await page.goto(show_url, wait_until="domcontentloaded", timeout=_NAVIGATION_TIMEOUT)
            await page.wait_for_timeout(3000)
            await self._dismiss_overlays(page)
            # Check if we're already on a seat-selection page.
            if await self._wait_for_seat_map(page):
                return
            # If not, the URL may land on an intermediate page that needs
            # another click (e.g., "Select Seats" button).
            logger.info("[seats] Not on seat map yet — looking for seat-selection trigger.")

        # --- Option 3: find and click a matching showtime on-page -------
        show_time_text = show.get("show_time", "")
        venue = show.get("venue", "")
        logger.info(
            "[seats] Searching page for clickable showtime — venue=%r time=%r",
            venue, show_time_text,
        )

        time_selectors = [
            "a[href*='buytickets']",
            "button:has-text(':')",
            "[data-testid='showtime']",
            ".showtime-btn",
            ".showtime",
            "[class*='showtime']",
        ]
        for sel in time_selectors:
            try:
                els = await page.query_selector_all(sel)
                for el in els:
                    text = (await el.inner_text()).strip()
                    # Match if the element text contains the showtime.
                    if show_time_text and show_time_text in text:
                        logger.info("[seats] Clicking showtime element: %s", text[:40])
                        await el.click()
                        await page.wait_for_timeout(3000)
                        return
            except Exception:
                continue

        # --- Last resort: look for ANY clickable book/select button -----
        last_resort = [
            "a:has-text('Book')",
            "button:has-text('Select Seats')",
            "button:has-text('Book')",
            "text=/Select Seats/i",
        ]
        for sel in last_resort:
            try:
                btn = await page.wait_for_selector(sel, timeout=2000)
                if btn:
                    logger.info("[seats] Clicking fallback: %s", sel)
                    await btn.click()
                    await page.wait_for_timeout(3000)
                    return
            except Exception:
                continue

        logger.warning("[seats] Could not find a way to reach seat selection.")

    # ------------------------------------------------------------------
    # _wait_for_seat_map — detect when the seat-picking UI is ready
    # ------------------------------------------------------------------

    async def _wait_for_seat_map(self, page: "playwright.async_api.Page") -> bool:
        """
        Poll for the seat-map container to appear.

        BMS may render seats inside an SVG, a ``#seat-layout`` div,
        or a ``.seat-map`` wrapper.  Returns ``True`` as soon as one is
        detected.
        """
        seat_map_selectors = [
            "#seatLayoutContainer",
            "#seat-layout",
            ".seat-map",
            ".seatmap",
            "[data-testid='seat-map']",
            "svg[class*='seat']",
            "svg[id*='seat']",
            "div[class*='seat-layout']",
            "canvas[id*='seat']",
            "div[class*='SeatMap']",
        ]
        for sel in seat_map_selectors:
            try:
                el = await page.wait_for_selector(sel, timeout=5000)
                if el:
                    logger.debug("[seats] Seat map detected via '%s'.", sel)
                    await page.wait_for_timeout(1000)
                    return True
            except Exception:
                continue

        # Broad check: any canvas or SVG on the page?
        try:
            if await page.query_selector("canvas") or await page.query_selector("svg"):
                logger.debug("[seats] Found canvas/svg on page — assuming seat map.")
                return True
        except Exception:
            pass

        return False

    # ------------------------------------------------------------------
    # _scrape_seats — extract every seat element from the page
    # ------------------------------------------------------------------

    async def _scrape_seats(
        self, page: "playwright.async_api.Page",
    ) -> List[Dict[str, Any]]:
        """
        Find all seat elements on the seat-selection page and return a
        list of dicts::

            {
                "id": "H8",
                "row": "H",
                "col": 8,
                "category": "GOLD",
                "price": 250.0,
                "status": "available" | "sold" | "blocked",
                "element": <Playwright element handle>,
            }
        """
        seats: List[Dict[str, Any]] = []

        # --- Pattern A: SVG circles / rects ---------------------------------
        svg_seat_selectors = [
            "svg circle",
            "svg rect",
            "svg [data-seat-id]",
            "svg [data-row]",
            "svg .seat",
            "svg [class*='seat']",
        ]
        for sel in svg_seat_selectors:
            els = await page.query_selector_all(sel)
            if els:
                logger.debug("[seats] Found %d SVG seats via '%s'.", len(els), sel)
                for el in els:
                    seat = await self._parse_seat_element(el)
                    if seat:
                        seats.append(seat)
                if seats:
                    break

        # --- Pattern B: DOM div / button seats ------------------------------
        if not seats:
            dom_seat_selectors = [
                "[data-seat-id]",
                "[data-testid*='seat']",
                "div[class*='seat']",
                "button[class*='seat']",
                "li[class*='seat']",
                ".seat-item",
            ]
            for sel in dom_seat_selectors:
                els = await page.query_selector_all(sel)
                if els:
                    logger.debug("[seats] Found %d DOM seats via '%s'.", len(els), sel)
                    for el in els:
                        seat = await self._parse_seat_element(el)
                        if seat:
                            seats.append(seat)
                    if seats:
                        break

        # --- Pattern C: Canvas — extract category labels at least -----------
        if not seats:
            has_canvas = await page.query_selector("canvas")
            if has_canvas:
                logger.warning(
                    "[seats] Seat map is a <canvas> — cannot scrape individual "
                    "seats programmatically. Categories/prices may still be "
                    "visible in page text."
                )
                # Try to extract seat categories and their prices from labels.
                seats = await self._scrape_seat_categories_from_labels(page)
                # Synthetic placeholder seats (one per category).
                # The caller will see these in the results but they won't be
                # clickable via element handles.

        logger.info("[seats] Total seat elements parsed: %d", len(seats))
        return seats

    # ------------------------------------------------------------------
    # _parse_seat_element — extract metadata from a single seat node
    # ------------------------------------------------------------------

    async def _parse_seat_element(
        self, el: "playwright.async_api.ElementHandle",
    ) -> Optional[Dict[str, Any]]:
        """Parse a single seat DOM/SVG element into a structured dict."""
        try:
            # --- Gather attributes ------------------------------------------
            seat_id   = await el.get_attribute("data-seat-id")
            row       = await el.get_attribute("data-row")
            col_str   = await el.get_attribute("data-col")
            category  = await el.get_attribute("data-category")
            aria_label = await el.get_attribute("aria-label")
            class_attr = await el.get_attribute("class") or ""
            fill      = await el.get_attribute("fill")

            # --- Derive status ----------------------------------------------
            status = "available"
            blocked_classes = ["sold", "blocked", "unavailable", "taken", "booked", "locked"]
            if any(bk in (class_attr or "").lower() for bk in blocked_classes):
                status = "sold"
            if "available" in (class_attr or "").lower():
                status = "available"
            # Fill colour heuristic: grey/dark → sold; coloured → available.
            if fill:
                fill_lower = fill.lower()
                if fill_lower in ("#ccc", "#ddd", "#999", "#aaa", "#b3b3b3", "grey", "gray", "#808080"):
                    status = "sold"

            # --- Derive seat ID if missing ----------------------------------
            if not seat_id and aria_label:
                # e.g. aria-label="Seat H8 available" or "Row H Seat 8"
                id_m = re.search(r"[A-Z]\d{1,2}", aria_label)
                if id_m:
                    seat_id = id_m.group(0)

            if not seat_id and row and col_str:
                seat_id = f"{row}{col_str}"

            # --- Derive row/col if only the ID is present -------------------
            if not row and seat_id:
                row_m = re.match(r"([A-Z]+)(\d+)", seat_id.strip())
                if row_m:
                    row = row_m.group(1)
                    col_str = col_str or row_m.group(2)

            col = int(col_str) if col_str and col_str.isdigit() else None

            if not seat_id:
                return None  # can't identify this seat

            # --- Derive category if missing ---------------------------------
            if not category:
                # Look in class names: "gold", "silver", "premium", …
                cat_m = re.search(
                    r"(gold|silver|premium|platinum|executive|club|recliner|balcony|lower|upper)",
                    class_attr, re.IGNORECASE,
                )
                if cat_m:
                    category = cat_m.group(1).upper()

            # --- Derive price -----------------------------------------------
            price: Optional[float] = None
            # Look for price in data attrs.
            price_str = await el.get_attribute("data-price")
            if price_str:
                try:
                    price = float(price_str)
                except ValueError:
                    pass
            # Look for price in a nearby label.
            if price is None:
                try:
                    parent = await el.evaluate(
                        "el => el.closest('div, g, li, tr')?.innerText || ''"
                    )
                    price = self._extract_price(parent)
                except Exception:
                    pass
            # Look in the aria-label.
            if price is None and aria_label:
                price = self._extract_price(aria_label)

            return {
                "id": seat_id,
                "row": row or seat_id[0] if seat_id else None,
                "col": col,
                "category": category,
                "price": price,
                "status": status,
                "element": el,
            }
        except Exception as exc:
            logger.debug("[seats] Failed to parse seat element: %s", exc)
            return None

    # ------------------------------------------------------------------
    # _scrape_seat_categories_from_labels — canvas fallback
    # ------------------------------------------------------------------

    async def _scrape_seat_categories_from_labels(
        self, page: "playwright.async_api.Page",
    ) -> List[Dict[str, Any]]:
        """
        When seats are on a ``<canvas>`` we cannot click individual seats,
        but we *can* extract category names and prices from textual labels
        on the page (e.g. a legend).  Returns synthetic placeholder entries.
        """
        results: List[Dict[str, Any]] = []
        body = await page.inner_text("body")

        # Look for patterns like "GOLD ₹200.00" or "SILVER - ₹150"
        cat_pattern = re.compile(
            r"(GOLD|SILVER|PREMIUM|PLATINUM|EXECUTIVE|CLUB|RECLINER|BALCONY|LOUNGE)",
            re.IGNORECASE,
        )
        for match in cat_pattern.finditer(body):
            cat = match.group(1).upper()
            # Grab a window of text around the match to find a price.
            start = max(0, match.start() - 50)
            end = min(len(body), match.end() + 50)
            price = self._extract_price(body[start:end])
            results.append({
                "id": f"{cat}_canvas",
                "row": None,
                "col": None,
                "category": cat,
                "price": price,
                "status": "available",
                "element": None,  # not clickable
            })

        # Deduplicate by category.
        seen = set()
        deduped = []
        for s in results:
            if s["category"] not in seen:
                seen.add(s["category"])
                deduped.append(s)
        return deduped

    # ------------------------------------------------------------------
    # _score_seats — centre-proximity with contiguous-block preference
    # ------------------------------------------------------------------

    def _score_seats(
        self,
        seats: List[Dict[str, Any]],
        num_tickets: int,
    ) -> List[Dict[str, Any]]:
        """
        Pick the best *num_tickets* seats from *seats*.

        Strategy (price and category are **not** considered):

        1. Compute the auditorium centre:
           - *ideal_row* = middle row index (e.g. row F out of A-L).
           - *ideal_col* = average of min and max column numbers.

        2. Score every seat by squared Euclidean distance from the centre:
           ``score = -(row_dist² + col_dist²)``
           (higher/less-negative = closer to centre).

        3. Try to find a **contiguous block** of *num_tickets* adjacent
           seats **in the same row**, sorted by column.  Slide a window
           over each row; the block with the highest total score wins.

        4. If no full block exists in any row, fall back to the top
           individually-scored seats (ignoring adjacency).

        Returns
        -------
        list[dict]
            The chosen seats, best-first.
        """
        if not seats:
            return []

        # ------------------------------------------------------------------
        # 1. Build row ordering and column bounds
        # ------------------------------------------------------------------
        # Map row letter → ordering index (A=0, B=1, …).  Preserve the
        # order the rows first appear in, but we also want the *letter*
        # to determine the index so A is always first, L is always last.
        all_rows_raw = sorted(
            {s["row"] for s in seats if s.get("row")},
            key=lambda r: (len(r), r),  # single-char rows (A-Z) sort first, then AA, AB, …
        )
        # Build a canonical row → index mapping covering all rows seen
        # (not just the available seats).
        row_to_idx: Dict[str, int] = {r: i for i, r in enumerate(all_rows_raw)}

        all_cols = [s["col"] for s in seats if s.get("col") is not None]
        if not row_to_idx or not all_cols:
            # Can't determine centre — return as-is (stable order).
            return list(seats)[:num_tickets]

        min_col = min(all_cols)
        max_col = max(all_cols)

        # Ideal centre (can be fractional).
        ideal_row_idx = (len(all_rows_raw) - 1) / 2.0
        ideal_col = (min_col + max_col) / 2.0

        # ------------------------------------------------------------------
        # 2. Compute individual centre-proximity score for each seat
        # ------------------------------------------------------------------
        def _centre_score(seat: Dict[str, Any]) -> float:
            r = seat.get("row")
            c = seat.get("col")

            row_idx = row_to_idx.get(r)
            row_dist = abs(row_idx - ideal_row_idx) if row_idx is not None else float("inf")
            col_dist = abs(c - ideal_col) if c is not None else float("inf")

            # Squared distance — negated so higher = better.
            # Seat exactly at the centre gets 0; everything else negative.
            return -(row_dist ** 2 + col_dist ** 2)

        for s in seats:
            s["_score"] = _centre_score(s)

        # Sort by score descending (best first).
        seats_sorted = sorted(seats, key=lambda s: s["_score"], reverse=True)

        # ------------------------------------------------------------------
        # 3. Contiguous-block search (per row)
        # ------------------------------------------------------------------
        # Group available seats by row.
        by_row: Dict[str, List[Dict[str, Any]]] = {}
        for s in seats_sorted:
            r = s.get("row")
            if r:
                by_row.setdefault(r, []).append(s)

        best_block: Optional[List[Dict[str, Any]]] = None
        best_block_score = float("-inf")

        for _row_letter, row_seats in by_row.items():
            # Sort this row's seats by column.
            row_seats_sorted = sorted(row_seats, key=lambda s: s.get("col") or 0)

            if len(row_seats_sorted) < num_tickets:
                continue

            # Slide a window over adjacent seats.
            for i in range(len(row_seats_sorted) - num_tickets + 1):
                window = row_seats_sorted[i : i + num_tickets]
                cols = [s.get("col") for s in window]

                # Check contiguity: co-1 from first to last equals (n-1).
                if all(c is not None for c in cols):
                    col_range = max(cols) - min(cols)  # type: ignore[type-var]
                    if col_range == num_tickets - 1:
                        block_score = sum(s["_score"] for s in window)
                        if block_score > best_block_score:
                            best_block_score = block_score
                            best_block = list(window)
                        continue

                # Non-contiguous window — also consider it as a candidate
                # (slightly penalised so contiguous beats it only on ties).
                block_score = sum(s["_score"] for s in window) - 1.0
                if block_score > best_block_score:
                    best_block_score = block_score
                    best_block = list(window)

        if best_block is not None:
            logger.info(
                "[seats] Chose contiguous block in row %s: %s (block_score=%.2f)",
                best_block[0].get("row", "?"),
                [s["id"] for s in best_block],
                best_block_score,
            )
            return best_block

        # ------------------------------------------------------------------
        # 4. Fallback: top individually-scored seats
        # ------------------------------------------------------------------
        logger.info(
            "[seats] No %d-adjacent block found — falling back to top individual seats.",
            num_tickets,
        )
        return seats_sorted[:num_tickets]

    # ------------------------------------------------------------------
    # _click_seats — click the chosen seat elements
    # ------------------------------------------------------------------

    async def _click_seats(
        self,
        page: "playwright.async_api.Page",
        chosen: List[Dict[str, Any]],
    ) -> bool:
        """Click each seat in *chosen* (by its ``element`` handle)."""
        if not chosen:
            return False

        clicked_count = 0
        for seat in chosen:
            el = seat.get("element")
            if el is None:
                logger.warning("[seats] No element handle for seat %s — skipping.", seat.get("id"))
                continue
            try:
                # Scroll into view first.
                await el.scroll_into_view_if_needed()
                await page.wait_for_timeout(200)
                await el.click()
                logger.debug("[seats] Clicked seat %s.", seat.get("id"))
                clicked_count += 1
                await page.wait_for_timeout(300)
            except Exception as exc:
                logger.warning("[seats] Click failed for %s: %s", seat.get("id"), exc)

        logger.info("[seats] Clicked %d/%d seat(s).", clicked_count, len(chosen))
        return clicked_count > 0

    # ------------------------------------------------------------------
    # _click_proceed_button — advance past seat selection
    # ------------------------------------------------------------------

    async def _click_proceed_button(self, page: "playwright.async_api.Page") -> bool:
        """
        After selecting seats, click the "Proceed" / "Continue" / "Pay"
        button to advance to the payment page.  Does NOT submit payment.
        """
        proceed_selectors = [
            "button:has-text('Proceed')",
            "button:has-text('Continue')",
            "button:has-text('Pay')",
            "button:has-text('Next')",
            "[data-testid='proceed-btn']",
            ".proceed-btn",
            "#proceedBtn",
            "#btnProceed",
            "a:has-text('Proceed')",
        ]
        for sel in proceed_selectors:
            try:
                btn = await page.wait_for_selector(sel, timeout=3000)
                if btn and await btn.is_enabled():
                    logger.info("[seats] Clicking '%s'.", sel)
                    await btn.click()
                    await page.wait_for_timeout(2000)
                    return True
            except Exception:
                continue

        logger.warning("[seats] Could not find a Proceed/Continue button.")
        return False

    # ==================================================================
    # Payment completion
    # ==================================================================

    # ==================================================================
    # Payment completion
    # ==================================================================

    async def complete_payment(
        self,
        page: "playwright.async_api.Page",
        expected_amount: Optional[float] = None,
        *,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Complete the booking using a **BMS Gift Card** (pre‑purchased
        e‑code + PIN).

        Call this **after** :meth:`select_best_seats` (which navigates
        through to the payment-options page).  The method:

        1. Waits for the payment-options page to appear.
        2. Selects "Gift Card" / "Voucher" as the payment method.
        3. Retrieves the gift card e‑code and PIN from stored credentials
           (raises :exc:`MissingGiftCardCredentials` if absent).
        4. Fills the e‑code and PIN fields, clicks "Apply" / "Redeem".
        5. Reads the **gift card balance** from the page and checks it
           against *expected_amount*.  Raises :exc:`InsufficientGiftCardBalance`
           if the balance does not cover the amount.
        6. Clicks "Pay Now" / "Confirm", waits for the success page, and
           extracts the booking ID.
        7. Saves a confirmation screenshot to
           ``screenshots/booking_confirm_{booking_id}.png``.

        Parameters
        ----------
        page : Page
            An active Playwright page that should already be on the
            payment-options screen.
        expected_amount : float, optional
            The amount you expect to be charged.  If provided, the method
            reads the gift card's remaining balance from the page and
            raises :exc:`InsufficientGiftCardBalance` when the balance is
            lower than this amount.  If omitted the balance check is still
            performed but only logs a warning.
        dry_run : bool, keyword-only
            When ``True``, stops after applying the gift card and checking
            the balance — never clicks "Pay Now".  A screenshot of the
            payment page is saved.  Returns ``success=False`` with
            ``dry_run=True`` in the result dict.

        Returns
        -------
        dict
            Keys:

            ``success`` (bool)
                ``True`` when payment was confirmed (always ``False`` in
                dry‑run mode).
            ``dry_run`` (bool, optional)
                ``True`` when the method was stopped before final payment.
            ``booking_id`` (str or None)
                The BMS booking / transaction ID extracted from the
                success page.
            ``cinema`` (str or None)
                Cinema name if visible on the confirmation.
            ``show_time`` (str or None)
                Show time if visible.
            ``seats`` (str or None)
                Seat numbers if visible.
            ``total_paid`` (str or None)
                Amount charged as displayed on the confirmation.
            ``screenshot`` (str or None)
                Path to the confirmation screenshot, relative to cwd.

        Raises
        ------
        PaymentPageNotFound
            If the payment-options page does not appear within 10 s.
        MissingGiftCardCredentials
            If gift card details (``e_code`` + ``pin``) are not found in
            stored credentials.
        InsufficientGiftCardBalance
            If *expected_amount* is provided and the gift card balance on
            the page is lower.
        PaymentFailed
            If the confirmation page does not appear or a booking ID
            cannot be extracted after clicking Pay (not raised in dry‑run
            mode).
        """
        # ------------------------------------------------------------------
        # 1. Wait for payment-options page
        # ------------------------------------------------------------------
        logger.info("[payment] Waiting for payment-options page …")
        payment_page_indicators = [
            "[data-testid='payment-options']",
            ".payment-options",
            ".payment-methods",
            "[class*='payment']",
            "text=/select.*payment/i",
            "text=/choose.*payment/i",
            "text=/payment.*method/i",
        ]
        found = False
        for sel in payment_page_indicators:
            try:
                el = await page.wait_for_selector(sel, timeout=10_000)
                if el:
                    logger.debug("[payment] Payment page detected via '%s'.", sel)
                    found = True
                    break
            except Exception:
                continue

        if not found:
            # Broader check: does the page contain payment-related text?
            try:
                body = await page.inner_text("body")
                if any(phrase in body.lower() for phrase in (
                    "gift card", "voucher", "redeem", "pay now",
                    "payment", "cards", "upi", "net banking",
                )):
                    logger.info("[payment] Payment-related text found on page (broad match).")
                    found = True
            except Exception:
                pass

        if not found:
            raise PaymentPageNotFound(
                "Payment options page did not load within 10 seconds. "
                "Current URL: " + page.url
            )

        await page.wait_for_timeout(1500)

        # ------------------------------------------------------------------
        # 2. Fill contact details (email + phone) for ticket delivery
        # ------------------------------------------------------------------
        logger.info("[payment] Filling contact details for ticket delivery …")
        creds = self._load_credentials()
        user_details = creds.get("user_details", {})
        contact_email = user_details.get("email", "")
        contact_phone = user_details.get("phone", "")

        if not contact_email or not contact_phone:
            logger.warning(
                "[payment] Missing contact details in credentials — "
                "email and phone may need to be filled manually. "
                "Run:  python setup_creds.py"
            )

        # Look for "Continue as Guest" or similar bypass options
        guest_selectors = [
            "text=/Continue as Guest/i",
            "text=/Guest Checkout/i",
            "text=/Skip Login/i",
            "button:has-text('Guest')",
            "a:has-text('Guest')",
        ]
        for sel in guest_selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    logger.info("[payment] Clicking guest checkout: %s", sel)
                    await el.click()
                    await page.wait_for_timeout(1500)
                    break
            except Exception:
                continue

        if contact_email:
            email_selectors = [
                "input[type='email']",
                "input[name='email']",
                "input[id*='email' i]",
                "input[placeholder*='email' i]",
                "input[placeholder*='Email']",
                "input[aria-label*='email' i]",
            ]
            email_filled = False
            for sel in email_selectors:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click()
                        await page.wait_for_timeout(200)
                        await el.fill("")
                        await el.type(contact_email, delay=60)
                        logger.info("[payment] Filled email: %s", contact_email)
                        email_filled = True
                        break
                except Exception:
                    continue
            if not email_filled:
                logger.warning(
                    "[payment] Could not find email input on payment page — "
                    "email may need to be filled manually."
                )

        if contact_phone:
            phone_selectors = [
                "input[type='tel']",
                "input[name='phone']",
                "input[name='mobile']",
                "input[id*='phone' i]",
                "input[id*='mobile' i]",
                "input[placeholder*='phone' i]",
                "input[placeholder*='mobile' i]",
                "input[aria-label*='phone' i]",
            ]
            phone_filled = False
            for sel in phone_selectors:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click()
                        await page.wait_for_timeout(200)
                        await el.fill("")
                        await el.type(contact_phone, delay=60)
                        logger.info("[payment] Filled phone: %s", contact_phone)
                        phone_filled = True
                        break
                except Exception:
                    continue
            if not phone_filled:
                logger.warning(
                    "[payment] Could not find phone input on payment page — "
                    "phone may need to be filled manually."
                )

        await page.wait_for_timeout(1000)

        # ------------------------------------------------------------------
        # 3. Select Gift Card / Voucher payment method
        # ------------------------------------------------------------------
        logger.info("[payment] Selecting Gift Card / Voucher …")

        # BMS may nest gift cards under tabs like "Wallets", "More Options",
        # or "Vouchers".  Try those first.
        giftcard_tab_selectors = [
            "text=/Gift Card/i",
            "text=/Voucher/i",
            "text=/Redeem/i",
            "text=/Gift Voucher/i",
            "text=/More Options/i",
            "text=/Wallets/i",
            "[data-testid='giftcard-tab']",
            "[data-testid='voucher-tab']",
            ".giftcard-tab",
            ".voucher-tab",
            "button:has-text('Gift Card')",
            "a:has-text('Gift Card')",
            "button:has-text('Voucher')",
            "a:has-text('Voucher')",
        ]
        for sel in giftcard_tab_selectors:
            try:
                tab = await page.query_selector(sel)
                if tab and await tab.is_visible():
                    logger.info("[payment] Clicking gift card / voucher tab: %s", sel)
                    await tab.click()
                    await page.wait_for_timeout(1500)
                    break
            except Exception:
                continue

        # Now look for the specific gift-card radio / label.
        giftcard_selectors = [
            "input[type='radio'][value*='gift' i]",
            "input[type='radio'][value*='voucher' i]",
            "input[type='radio'][id*='gift' i]",
            "input[type='radio'][id*='voucher' i]",
            "label[for*='gift' i]",
            "label[for*='voucher' i]",
            "[data-testid='giftcard-option']",
            "[data-testid='voucher-option']",
            ".giftcard-option",
            ".voucher-option",
            "div:has-text('Gift Card')",
            "span:has-text('Gift Card')",
            "div:has-text('Redeem Voucher')",
            "span:has-text('Redeem Voucher')",
            "div:has-text('E‑Code')",
            "div:has-text('e-code' i)",
        ]
        giftcard_selected = False
        for sel in giftcard_selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    logger.info("[payment] Selecting gift card via: %s", sel)
                    await el.click()
                    await page.wait_for_timeout(1000)
                    giftcard_selected = True
                    break
            except Exception:
                continue

        if not giftcard_selected:
            logger.warning(
                "[payment] Could not explicitly select Gift Card — "
                "it may already be the default or the page layout changed."
            )

        # ------------------------------------------------------------------
        # 4. Retrieve gift card credentials (reuses cached creds from step 2)
        # ------------------------------------------------------------------
        logger.info("[payment] Retrieving gift card credentials …")

        gift_card = creds.get("gift_card")
        if not isinstance(gift_card, dict):
            raise MissingGiftCardCredentials(
                "No gift_card entry found in credentials. "
                "Run 'python setup_creds.py' "
                "and store your BMS Gift Card e‑code and PIN."
            )

        e_code = gift_card.get("e_code")
        pin = gift_card.get("pin")
        if not e_code or not pin:
            raise MissingGiftCardCredentials(
                "Gift card e‑code or PIN is missing. "
                "Run 'python setup_creds.py' "
                "and ensure both fields are stored."
            )
        logger.info("[payment] Gift card e‑code: %s**** (PIN hidden)", e_code[:4])

        # ------------------------------------------------------------------
        # 5. Fill e‑code and PIN, then click Apply / Redeem
        # ------------------------------------------------------------------
        # --- e‑code field ---
        ecode_selectors = [
            "input[placeholder*='e-code' i]",
            "input[placeholder*='ecode' i]",
            "input[placeholder*='E-Code' i]",
            "input[placeholder*='gift card' i]",
            "input[id*='ecode' i]",
            "input[id*='e-code' i]",
            "input[name*='ecode' i]",
            "input[name*='e-code' i]",
            "[data-testid='ecode-input']",
            "#giftCardNumber",
            "#voucherCode",
            ".giftcard-number input",
            "input[aria-label*='e-code' i]",
        ]
        ecode_field = None
        for sel in ecode_selectors:
            try:
                ecode_field = await page.wait_for_selector(sel, timeout=5000)
                if ecode_field and await ecode_field.is_visible():
                    logger.debug("[payment] Found e‑code field: %s", sel)
                    break
                ecode_field = None
            except Exception:
                continue

        if ecode_field is None:
            # Broad fallback: first visible text input near "Gift Card" text.
            try:
                all_inputs = await page.query_selector_all("input[type='text'], input:not([type])")
                for inp in all_inputs:
                    if await inp.is_visible():
                        ecode_field = inp
                        logger.debug("[payment] Using first visible text input as e‑code field.")
                        break
            except Exception:
                pass

        if ecode_field is None:
            raise BMSAutomationError(
                "Could not find the e‑code input field on the payment page. "
                "The BMS layout may have changed."
            )

        await ecode_field.click()
        await page.wait_for_timeout(200)
        await ecode_field.fill("")
        await ecode_field.type(e_code, delay=80)
        logger.info("[payment] E‑code entered.")

        # --- PIN field ---
        pin_selectors = [
            "input[placeholder*='pin' i]",
            "input[placeholder*='PIN']",
            "input[id*='gift-pin' i]",
            "input[id*='voucher-pin' i]",
            "input[name*='gift-pin' i]",
            "input[name*='pin']",
            "[data-testid='giftcard-pin-input']",
            "#giftCardPin",
            "#voucherPin",
            ".giftcard-pin input",
            "input[aria-label*='pin' i]",
        ]
        pin_field = None
        for sel in pin_selectors:
            try:
                pin_field = await page.wait_for_selector(sel, timeout=5000)
                if pin_field and await pin_field.is_visible():
                    logger.debug("[payment] Found gift-card PIN field: %s", sel)
                    break
                pin_field = None
            except Exception:
                continue

        if pin_field is None:
            # Fallback: look for a password field near the e‑code field.
            try:
                all_pw_fields = await page.query_selector_all("input[type='password']")
                for pw in all_pw_fields:
                    if await pw.is_visible():
                        pin_field = pw
                        logger.debug("[payment] Using visible password field as PIN input.")
                        break
            except Exception:
                pass

        if pin_field is None:
            raise BMSAutomationError(
                "Could not find the gift card PIN input field on the payment page."
            )

        await pin_field.click()
        await page.wait_for_timeout(200)
        await pin_field.fill("")
        await pin_field.type(pin, delay=80)
        logger.info("[payment] Gift card PIN entered.")

        # --- Click Apply / Redeem ---
        apply_selectors = [
            "button:has-text('Apply')",
            "button:has-text('Redeem')",
            "button:has-text('Check Balance')",
            "button:has-text('Verify')",
            "button:has-text('Add')",
            "[data-testid='apply-giftcard-btn']",
            ".apply-giftcard-btn",
            ".redeem-btn",
        ]
        apply_clicked = False
        for sel in apply_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    logger.info("[payment] Clicking Apply / Redeem: %s", sel)
                    await btn.click()
                    await page.wait_for_timeout(3000)
                    apply_clicked = True
                    break
            except Exception:
                continue

        if not apply_clicked:
            # Sometimes pressing Enter in the PIN field works.
            logger.info("[payment] No Apply button found — pressing Enter on PIN field.")
            await pin_field.press("Enter")
            await page.wait_for_timeout(3000)

        # ------------------------------------------------------------------
        # 6. Check gift card balance after applying
        # ------------------------------------------------------------------
        logger.info("[payment] Reading gift card balance from page …")
        balance: Optional[float] = None
        balance_selectors = [
            "[data-testid='giftcard-balance']",
            ".giftcard-balance",
            ".balance-amount",
            "span:has-text('Balance') + span",
            "span:has-text('Remaining') + span",
            "text=/Balance.*₹/i",
            "text=/Gift Card Balance/i",
            "text=/Remaining Balance/i",
        ]
        for sel in balance_selectors:
            try:
                el = await page.query_selector(sel)
                if el:
                    balance_text = (await el.inner_text()).strip()
                    logger.debug("[payment] Gift card raw text: %s", balance_text)
                    numeric = re.sub(r"[^\d.]", "", balance_text)
                    if numeric:
                        balance = float(numeric)
                        break
            except Exception:
                continue

        if balance is None:
            # Broader scan for any ₹ amount near "balance" / "remaining".
            try:
                body = await page.inner_text("body")
                match = re.search(
                    r"(?:balance|remaining|available)[^\n]*?₹\s*([\d,]+(?:\.\d{1,2})?)",
                    body, re.IGNORECASE,
                )
                if match:
                    balance = float(re.sub(r"[^\d.]", "", match.group(1)))
            except Exception:
                pass

        if balance is not None:
            logger.info("[payment] Gift card balance: ₹%.2f", balance)
        else:
            logger.warning("[payment] Could not read gift card balance from page.")

        if expected_amount is not None and balance is not None:
            if balance < expected_amount:
                raise InsufficientGiftCardBalance(balance, expected_amount)
            logger.info(
                "[payment] Gift card ₹%.2f covers ₹%.2f — proceeding.",
                balance, expected_amount,
            )
        elif expected_amount is not None and balance is None:
            logger.warning(
                "[payment] Cannot verify gift card balance — "
                "proceeding anyway for ₹%.2f.", expected_amount,
            )

        # ------------------------------------------------------------------
        # 6b. Dry‑run — stop before clicking Pay Now
        # ------------------------------------------------------------------
        if dry_run:
            logger.info(
                "[payment] 🔍 DRY RUN — stopping before payment. "
                "Gift card balance: ₹%s",
                f"{balance:.2f}" if balance is not None else "?",
            )
            os.makedirs("screenshots", exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            dryrun_path = f"screenshots/dryrun_payment_{timestamp}.png"
            try:
                await page.screenshot(path=dryrun_path, full_page=True)
                logger.info("[payment] Dry‑run screenshot saved: %s", dryrun_path)
            except Exception as exc:
                logger.warning("[payment] Could not save dry‑run screenshot: %s", exc)
                dryrun_path = ""
            return {
                "success": False,
                "dry_run": True,
                "booking_id": None,
                "cinema": None,
                "show_time": None,
                "seats": None,
                "total_paid": None,
                "screenshot": dryrun_path or None,
            }

        # ------------------------------------------------------------------
        # 7. Click "Pay Now" / "Confirm"
        # ------------------------------------------------------------------
        logger.info("[payment] Clicking 'Pay Now' / 'Confirm' …")
        pay_selectors = [
            "button:has-text('Pay Now')",
            "button:has-text('Pay ₹')",
            "button:has-text('Confirm')",
            "button:has-text('Make Payment')",
            "button:has-text('Proceed to Pay')",
            "[data-testid='pay-now-btn']",
            "#payNowBtn",
            ".pay-now-btn",
            "input[type='submit'][value*='Pay']",
            "button[type='submit']",
        ]
        pay_clicked = False
        for sel in pay_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    logger.info("[payment] Clicking: %s", sel)
                    await btn.click()
                    await page.wait_for_timeout(3000)
                    pay_clicked = True
                    break
            except Exception:
                continue

        if not pay_clicked:
            raise BMSAutomationError(
                "Could not find a 'Pay Now' or 'Confirm' button on the payment page."
            )

        # ------------------------------------------------------------------
        # 8. Wait for confirmation and extract booking details
        # ------------------------------------------------------------------
        logger.info("[payment] Waiting for booking confirmation …")
        await page.wait_for_timeout(5000)

        # Try to detect a success/confirmation page.
        success_selectors = [
            "[data-testid='booking-confirmation']",
            ".booking-confirmation",
            ".confirmation-page",
            ".success-page",
            "[class*='confirmation']",
            "[class*='success']",
            "text=/booking confirmed/i",
            "text=/booking successful/i",
            "text=/thank you/i",
            "text=/payment successful/i",
        ]
        success_found = False
        for sel in success_selectors:
            try:
                el = await page.query_selector(sel)
                if el:
                    logger.info("[payment] Confirmation page detected via '%s'.", sel)
                    success_found = True
                    break
            except Exception:
                continue

        if not success_found:
            # Check URL — BMS often redirects to a booking-confirmation path.
            current_url = page.url
            if any(kw in current_url.lower() for kw in (
                "confirmation", "booking", "success", "thankyou", "receipt",
            )):
                logger.info("[payment] URL suggests confirmation page: %s", current_url)
                success_found = True

        # --- Extract booking ID -----------------------------------------------
        booking_id: Optional[str] = None
        booking_id_patterns = [
            r"(?:booking\s*(?:id|ref|no|number)[:\s#]*)([A-Z0-9]{4,20})",
            r"(?:transaction\s*(?:id|ref|no)[:\s#]*)([A-Z0-9]{4,20})",
            r"(?:order\s*(?:id|no)[:\s#]*)([A-Z0-9]{4,20})",
            r"([A-Z]{2}\d{6,})",
            r"(\d{10,})",
        ]
        body_text = ""
        try:
            body_text = await page.inner_text("body")
        except Exception:
            pass

        for pattern in booking_id_patterns:
            match = re.search(pattern, body_text, re.IGNORECASE)
            if match:
                booking_id = match.group(1)
                logger.info("[payment] Extracted booking ID: %s", booking_id)
                break

        if not booking_id and success_found:
            # Still a success but couldn't parse ID — generate a timestamp-based one.
            fallback_id = f"BMS_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            logger.warning(
                "[payment] Could not parse booking ID from page — "
                "using fallback: %s", fallback_id,
            )
            booking_id = fallback_id

        # --- Extract other details --------------------------------------------
        cinema: Optional[str] = None
        show_time: Optional[str] = None
        seats: Optional[str] = None
        total_paid: Optional[str] = None

        # Cinema name.
        cinema_match = re.search(
            r"(?:cinema|venue|theatre)[:\s]*(.+)", body_text, re.IGNORECASE,
        )
        if cinema_match:
            cinema = cinema_match.group(1).strip().split("\n")[0]

        # Show time.
        time_match = re.search(
            r"(?:show\s*time|timing|time)[:\s]*(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)",
            body_text, re.IGNORECASE,
        )
        if time_match:
            show_time = time_match.group(1).strip()

        # Seats.
        seat_match = re.search(
            r"(?:seats?|seat\s*nos?)[:\s]*([A-Z]+\d+(?:\s*,\s*[A-Z]+\d+)*)",
            body_text, re.IGNORECASE,
        )
        if seat_match:
            seats = seat_match.group(1).strip()

        # Amount paid.
        amount_match = re.search(
            r"(?:total|paid|amount|₹)\s*₹?\s*([\d,]+(?:\.\d{1,2})?)",
            body_text, re.IGNORECASE,
        )
        if amount_match:
            total_paid = f"₹{amount_match.group(1).strip()}"

        # --- Take confirmation screenshot -------------------------------------
        os.makedirs("screenshots", exist_ok=True)
        safe_id = (booking_id or "unknown").replace("/", "_").replace(" ", "_")
        screenshot_path = f"screenshots/booking_confirm_{safe_id}.png"
        try:
            await page.screenshot(path=screenshot_path, full_page=True)
            logger.info("[payment] Confirmation screenshot saved: %s", screenshot_path)
        except Exception as exc:
            logger.warning("[payment] Could not save screenshot: %s", exc)
            screenshot_path = ""

        # ------------------------------------------------------------------
        # Build return value
        # ------------------------------------------------------------------
        if not success_found and not booking_id:
            raise PaymentFailed(
                "Payment confirmation could not be verified. "
                "Check manually at: " + page.url
            )

        result: Dict[str, Any] = {
            "success": True,
            "booking_id": booking_id,
            "cinema": cinema,
            "show_time": show_time,
            "seats": seats,
            "total_paid": total_paid,
            "screenshot": screenshot_path or None,
        }
        logger.info("[payment] ✅ Payment complete — booking_id=%s", booking_id)
        return result

    # ------------------------------------------------------------------
    # complete_payment_upi — UPI push-payment flow (primary method)
    # ------------------------------------------------------------------

    async def complete_payment_upi(
        self,
        page: "playwright.async_api.Page",
        expected_amount: Optional[float] = None,
        *,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Complete booking using **UPI** (push notification to the user's phone).

        Call this **after** :meth:`select_best_seats`.  The method:

        1. Waits for the payment-options page.
        2. Fills contact details (email + phone) for ticket delivery.
        3. Selects "UPI" as the payment method.
        4. Fills the UPI ID from stored credentials.
        5. Clicks "Verify" / "Proceed" to trigger the UPI push notification.
        6. Polls for up to 120 s for the confirmation page.
        7. Extracts booking details and saves a screenshot.

        In dry‑run mode stops after step 5 and does not poll.

        Parameters
        ----------
        page : Page
            Active Playwright page on the payment-options screen.
        expected_amount : float, optional
            Logged for reference (UPI balance not checked).
        dry_run : bool, keyword-only
            When ``True``, stops after clicking "Verify" — never waits for
            phone approval.

        Returns
        -------
        dict
            Same structure as :meth:`complete_payment`.

        Raises
        ------
        PaymentMethodNotFound
            If the UPI option does not appear on the payment page.
        MissingUPIID
            If UPI ID is not set in stored credentials.
        PaymentTimeout
            If the confirmation page does not appear within 120 s.
        """
        # ------------------------------------------------------------------
        # 1. Wait for payment-options page
        # ------------------------------------------------------------------
        logger.info("[payment-upi] Waiting for payment-options page …")
        payment_page_indicators = [
            "[data-testid='payment-options']",
            ".payment-options",
            ".payment-methods",
            "[class*='payment']",
            "text=/select.*payment/i",
            "text=/choose.*payment/i",
            "text=/payment.*method/i",
        ]
        found = False
        for sel in payment_page_indicators:
            try:
                el = await page.wait_for_selector(sel, timeout=10_000)
                if el:
                    logger.debug("[payment-upi] Payment page detected via '%s'.", sel)
                    found = True
                    break
            except Exception:
                continue

        if not found:
            try:
                body = await page.inner_text("body")
                if any(phrase in body.lower() for phrase in (
                    "gift card", "voucher", "redeem", "pay now",
                    "payment", "cards", "upi", "net banking",
                )):
                    logger.info("[payment-upi] Payment-related text found (broad match).")
                    found = True
            except Exception:
                pass

        if not found:
            raise PaymentPageNotFound(
                "Payment options page did not load within 10 seconds. "
                "Current URL: " + page.url
            )

        await page.wait_for_timeout(1500)

        # ------------------------------------------------------------------
        # 2. Fill contact details (email + phone) for ticket delivery
        # ------------------------------------------------------------------
        logger.info("[payment-upi] Filling contact details …")
        creds = self._load_credentials()
        user_details = creds.get("user_details", {})
        contact_email = user_details.get("email", "")
        contact_phone = user_details.get("phone", "")

        if not contact_email or not contact_phone:
            logger.warning(
                "[payment-upi] Missing contact details — email/phone may "
                "need to be filled manually. Run:  python setup_creds.py"
            )

        # "Continue as Guest" or similar bypass
        guest_selectors = [
            "text=/Continue as Guest/i",
            "text=/Guest Checkout/i",
            "text=/Skip Login/i",
            "button:has-text('Guest')",
            "a:has-text('Guest')",
        ]
        for sel in guest_selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    logger.info("[payment-upi] Clicking guest checkout: %s", sel)
                    await el.click()
                    await page.wait_for_timeout(1500)
                    break
            except Exception:
                continue

        if contact_email:
            email_selectors = [
                "input[type='email']",
                "input[name='email']",
                "input[id*='email' i]",
                "input[placeholder*='email' i]",
                "input[placeholder*='Email']",
                "input[aria-label*='email' i]",
            ]
            for sel in email_selectors:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click()
                        await page.wait_for_timeout(200)
                        await el.fill("")
                        await el.type(contact_email, delay=60)
                        logger.info("[payment-upi] Filled email: %s", contact_email)
                        break
                except Exception:
                    continue

        if contact_phone:
            phone_selectors = [
                "input[type='tel']",
                "input[name='phone']",
                "input[name='mobile']",
                "input[id*='phone' i]",
                "input[id*='mobile' i]",
                "input[placeholder*='phone' i]",
                "input[placeholder*='mobile' i]",
                "input[aria-label*='phone' i]",
            ]
            for sel in phone_selectors:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click()
                        await page.wait_for_timeout(200)
                        await el.fill("")
                        await el.type(contact_phone, delay=60)
                        logger.info("[payment-upi] Filled phone: %s", contact_phone)
                        break
                except Exception:
                    continue

        await page.wait_for_timeout(1000)

        # ------------------------------------------------------------------
        # 3. Select "UPI" as payment method
        # ------------------------------------------------------------------
        logger.info("[payment-upi] Selecting UPI payment method …")

        upi_selectors = [
            "text=/UPI/i",
            "text=/Google Pay/i",
            "text=/PhonePe/i",
            "text=/Paytm/i",
            "text=/BHIM/i",
            "input[type='radio'][value*='upi' i]",
            "input[type='radio'][id*='upi' i]",
            "label[for*='upi' i]",
            "[data-testid='upi-option']",
            "[data-testid='upi-tab']",
            ".upi-option",
            ".upi-tab",
            "img[alt*='UPI' i]",
            "img[alt*='upi' i]",
            "div:has-text('UPI')",
            "span:has-text('UPI')",
            "button:has-text('UPI')",
            "a:has-text('UPI')",
        ]
        upi_selected = False
        for sel in upi_selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    logger.info("[payment-upi] Selecting UPI via: %s", sel)
                    await el.click()
                    await page.wait_for_timeout(1500)
                    upi_selected = True
                    break
            except Exception:
                continue

        # Try "More Options" / "Wallets" tabs that may hide UPI
        if not upi_selected:
            more_selectors = [
                "text=/More Options/i",
                "text=/More/i",
                "text=/Wallets/i",
                "text=/Other/i",
            ]
            for sel in more_selectors:
                try:
                    tab = await page.query_selector(sel)
                    if tab and await tab.is_visible():
                        logger.info("[payment-upi] Expanding tab: %s", sel)
                        await tab.click()
                        await page.wait_for_timeout(1000)
                        # Try UPI selectors again after expanding
                        for upi_sel in upi_selectors:
                            try:
                                el = await page.query_selector(upi_sel)
                                if el and await el.is_visible():
                                    await el.click()
                                    await page.wait_for_timeout(1500)
                                    upi_selected = True
                                    logger.info("[payment-upi] Selected UPI after expanding tab.")
                                    break
                            except Exception:
                                continue
                        if upi_selected:
                            break
                except Exception:
                    continue

        if not upi_selected:
            raise PaymentMethodNotFound("UPI option not found on payment page.")

        # ------------------------------------------------------------------
        # 4. Get UPI ID from credentials
        # ------------------------------------------------------------------
        upi_id = creds.get("upi_id", "")
        if not upi_id:
            raise MissingUPIID()
        logger.info("[payment-upi] UPI ID: %s", upi_id)

        # ------------------------------------------------------------------
        # 5. Fill UPI ID and click Verify / Proceed
        # ------------------------------------------------------------------
        upi_input_selectors = [
            "input[placeholder*='upi' i]",
            "input[placeholder*='vpa' i]",
            "input[placeholder*='UPI ID' i]",
            "input[id*='upi' i]",
            "input[id*='vpa' i]",
            "input[name*='upi' i]",
            "input[name*='vpa' i]",
            "[data-testid='upi-input']",
            "[data-testid='vpa-input']",
            ".upi-input input",
            "#vpaInput",
            "#upiInput",
        ]
        upi_field = None
        for sel in upi_input_selectors:
            try:
                upi_field = await page.wait_for_selector(sel, timeout=5000)
                if upi_field and await upi_field.is_visible():
                    logger.debug("[payment-upi] Found UPI input: %s", sel)
                    break
                upi_field = None
            except Exception:
                continue

        if upi_field is None:
            # Fallback: first visible text input after selecting UPI
            try:
                all_inputs = await page.query_selector_all(
                    "input[type='text'], input:not([type])"
                )
                for inp in all_inputs:
                    if await inp.is_visible():
                        upi_field = inp
                        logger.debug("[payment-upi] Using first visible text input as UPI field.")
                        break
            except Exception:
                pass

        if upi_field is None:
            raise BMSAutomationError(
                "Could not find the UPI ID input field on the payment page."
            )

        await upi_field.click()
        await page.wait_for_timeout(200)
        await upi_field.fill("")
        await upi_field.type(upi_id, delay=60)
        logger.info("[payment-upi] UPI ID entered: %s", upi_id)

        # Click Verify / Proceed / Continue
        verify_selectors = [
            "button:has-text('Verify')",
            "button:has-text('Proceed')",
            "button:has-text('Continue')",
            "button:has-text('Pay Now')",
            "button:has-text('Pay ₹')",
            "[data-testid='verify-upi-btn']",
            "[data-testid='proceed-btn']",
            ".verify-btn",
            "#verifyBtn",
        ]
        verify_clicked = False
        for sel in verify_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    logger.info("[payment-upi] Clicking: %s", sel)
                    await btn.click()
                    await page.wait_for_timeout(2000)
                    verify_clicked = True
                    break
            except Exception:
                continue

        if not verify_clicked:
            logger.info("[payment-upi] No Verify button found — pressing Enter on UPI field.")
            await upi_field.press("Enter")
            await page.wait_for_timeout(2000)

        logger.info("[payment-upi] ⏳ UPI payment initiated — approve on your phone now.")

        # ------------------------------------------------------------------
        # 5b. Dry‑run — stop after triggering UPI push
        # ------------------------------------------------------------------
        if dry_run:
            logger.info(
                "[payment-upi] 🔍 DRY RUN — would wait for phone approval. "
                "Stopping now."
            )
            os.makedirs("screenshots", exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            dryrun_path = f"screenshots/dryrun_upi_{timestamp}.png"
            try:
                await page.screenshot(path=dryrun_path, full_page=True)
                logger.info("[payment-upi] Dry‑run screenshot saved: %s", dryrun_path)
            except Exception as exc:
                logger.warning("[payment-upi] Could not save dry‑run screenshot: %s", exc)
                dryrun_path = ""
            return {
                "success": False,
                "dry_run": True,
                "booking_id": None,
                "cinema": None,
                "show_time": None,
                "seats": None,
                "total_paid": None,
                "screenshot": dryrun_path or None,
            }

        # ------------------------------------------------------------------
        # 6. Poll for confirmation (max 120 s, every 2 s)
        # ------------------------------------------------------------------
        logger.info("[payment-upi] Waiting up to 120 s for phone approval …")
        max_wait = 120
        poll_interval = 2
        elapsed = 0

        success_selectors = [
            "[data-testid='booking-confirmation']",
            ".booking-confirmation",
            ".confirmation-page",
            ".success-page",
            "[class*='confirmation']",
            "[class*='success']",
            "text=/booking confirmed/i",
            "text=/booking successful/i",
            "text=/thank you/i",
            "text=/payment successful/i",
            "text=/transaction successful/i",
        ]

        while elapsed < max_wait:
            await page.wait_for_timeout(poll_interval * 1000)
            elapsed += poll_interval

            # Check for success
            for sel in success_selectors:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        logger.info(
                            "[payment-upi] Confirmation page detected via '%s' "
                            "after %d s.", sel, elapsed,
                        )
                        found = True
                        break
                except Exception:
                    continue
            else:
                # Also check URL for confirmation keywords.
                current_url = page.url
                if any(kw in current_url.lower() for kw in (
                    "confirmation", "booking", "success", "thankyou", "receipt",
                )):
                    logger.info(
                        "[payment-upi] URL suggests confirmation after %d s: %s",
                        elapsed, current_url,
                    )
                else:
                    if elapsed % 10 == 0:
                        logger.info(
                            "[payment-upi] Still waiting … %d s elapsed.", elapsed,
                        )
                    continue
            break  # success found
        else:
            # Timed out — take screenshot and raise
            os.makedirs("screenshots", exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            timeout_path = f"screenshots/upi_timeout_{ts}.png"
            try:
                await page.screenshot(path=timeout_path, full_page=True)
                logger.info("[payment-upi] Timeout screenshot saved: %s", timeout_path)
            except Exception as exc:
                logger.warning("[payment-upi] Could not save timeout screenshot: %s", exc)
            raise PaymentTimeout()

        # ------------------------------------------------------------------
        # 7. Extract booking details
        # ------------------------------------------------------------------
        logger.info("[payment-upi] Confirmation received — extracting details …")

        body_text = ""
        try:
            body_text = await page.inner_text("body")
        except Exception:
            pass

        booking_id: Optional[str] = None
        booking_id_patterns = [
            r"(?:booking\s*(?:id|ref|no|number)[:\s#]*)([A-Z0-9]{4,20})",
            r"(?:transaction\s*(?:id|ref|no)[:\s#]*)([A-Z0-9]{4,20})",
            r"(?:order\s*(?:id|no)[:\s#]*)([A-Z0-9]{4,20})",
            r"([A-Z]{2}\d{6,})",
            r"(\d{10,})",
        ]
        for pattern in booking_id_patterns:
            match = re.search(pattern, body_text, re.IGNORECASE)
            if match:
                booking_id = match.group(1)
                logger.info("[payment-upi] Extracted booking ID: %s", booking_id)
                break

        if not booking_id:
            fallback_id = f"BMS_UPI_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            logger.warning(
                "[payment-upi] Could not parse booking ID — "
                "using fallback: %s", fallback_id,
            )
            booking_id = fallback_id

        # Extract other details.
        cinema: Optional[str] = None
        show_time: Optional[str] = None
        seats: Optional[str] = None
        total_paid: Optional[str] = None

        cinema_match = re.search(
            r"(?:cinema|venue|theatre)[:\s]*(.+)", body_text, re.IGNORECASE,
        )
        if cinema_match:
            cinema = cinema_match.group(1).strip().split("\n")[0]

        time_match = re.search(
            r"(?:show\s*time|timing|time)[:\s]*(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)",
            body_text, re.IGNORECASE,
        )
        if time_match:
            show_time = time_match.group(1).strip()

        seat_match = re.search(
            r"(?:seats?|seat\s*nos?)[:\s]*([A-Z]+\d+(?:\s*,\s*[A-Z]+\d+)*)",
            body_text, re.IGNORECASE,
        )
        if seat_match:
            seats = seat_match.group(1).strip()

        amount_match = re.search(
            r"(?:total|paid|amount|₹)\s*₹?\s*([\d,]+(?:\.\d{1,2})?)",
            body_text, re.IGNORECASE,
        )
        if amount_match:
            total_paid = f"₹{amount_match.group(1).strip()}"

        # Screenshot
        os.makedirs("screenshots", exist_ok=True)
        safe_id = (booking_id or "unknown").replace("/", "_").replace(" ", "_")
        screenshot_path = f"screenshots/booking_confirm_UPI_{safe_id}.png"
        try:
            await page.screenshot(path=screenshot_path, full_page=True)
            logger.info("[payment-upi] Confirmation screenshot saved: %s", screenshot_path)
        except Exception as exc:
            logger.warning("[payment-upi] Could not save screenshot: %s", exc)
            screenshot_path = ""

        result: Dict[str, Any] = {
            "success": True,
            "booking_id": booking_id,
            "cinema": cinema,
            "show_time": show_time,
            "seats": seats,
            "total_paid": total_paid,
            "screenshot": screenshot_path or None,
        }
        logger.info("[payment-upi] ✅ UPI payment complete — booking_id=%s", booking_id)
        return result

# ---------------------------------------------------------------------------
# Self-test / CLI helper
# ---------------------------------------------------------------------------

async def _self_test() -> None:
    """Quick smoke-test when the module is run directly."""
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ------------------------------------------------------------------
    # Inline URL parser tests (no browser needed)
    # ------------------------------------------------------------------
    print("=" * 60)
    print("BookMyShow Playwright Automator — URL Parser Tests")
    print("=" * 60)

    test_urls = [
        (
            "https://in.bookmyshow.com/movies/chennai/spider-man-brand-new-day/buytickets/ET00447840/20260802",
            {"city": "chennai", "movie_slug": "spider-man-brand-new-day", "unique_code": "ET00447840", "date": "20260802"},
        ),
        (
            "https://in.bookmyshow.com/movies/chennai/spider-man-brand-new-day-epiq-3d/buytickets/ET00505581/20260730",
            {"city": "chennai", "movie_slug": "spider-man-brand-new-day-epiq-3d", "unique_code": "ET00505581", "date": "20260730"},
        ),
        # Negative test — not a booking URL
        (
            "https://in.bookmyshow.com/chennai/movies/spider-man-brand-new-day",
            None,
        ),
        # Trailing slash/query should still match
        (
            "https://in.bookmyshow.com/movies/mumbai/avatar-3/buytickets/ET00123456/20251225/?utm=share",
            {"city": "mumbai", "movie_slug": "avatar-3", "unique_code": "ET00123456", "date": "20251225"},
        ),
    ]

    all_ok = True
    for url, expected in test_urls:
        result = parse_booking_url(url)
        status = "✅" if result == expected else "❌"
        if result != expected:
            all_ok = False
        print(f"  {status} {url[:80]}...")
        if result:
            print(f"      → city={result['city']}  slug={result['movie_slug']}  "
                  f"code={result['unique_code']}  date={result['date']}")
        else:
            print(f"      → None (expected: {expected})")

    print("=" * 60)
    if all_ok:
        print("✅ All URL parser tests passed.\n")
    else:
        print("❌ Some URL parser tests FAILED.\n")

    # ------------------------------------------------------------------
    # Seat-scoring tests (no browser needed)
    # ------------------------------------------------------------------
    print("=" * 60)
    print("BookMyShow Playwright Automator — Seat Scoring Tests")
    print("=" * 60)

    bms_seat_test = BMSPlaywrightAutomator({"user_profile": {"max_tickets": 2}})

    # Build a realistic 12-row × 11-col auditorium (A–L, cols 1–11).
    # A=front (row 0), L=back (row 11). Ideal row = F (index 5.5), ideal col = 6.0.
    _rows = list("ABCDEFGHIJKL")
    test_seats: List[Dict[str, Any]] = []
    for ri, r in enumerate(_rows):
        for c in range(1, 12):
            test_seats.append({
                "id": f"{r}{c}",
                "row": r,
                "col": c,
                "category": "GOLD",
                "price": 200 + ri * 10,  # price slopes for verification (not used)
                "status": "available",
            })

    def _run_scoring_test(
        label: str,
        seats: List[Dict[str, Any]],
        n: int,
        expected_ids: List[str],
    ) -> bool:
        result = bms_seat_test._score_seats(seats, n)
        got = [s["id"] for s in result]
        ok = got == expected_ids
        emoji = "✅" if ok else "❌"
        print(f"  {emoji} {label}")
        print(f"      want: {expected_ids}")
        print(f"      got:  {got}")
        if not ok:
            # Show scores for debugging.
            for s in result[:max(len(expected_ids), n)]:
                print(f"        {s['id']}: row={s['row']} col={s['col']} score={s.get('_score','?')}")
        return ok

    seat_all_ok = True

    # --- Test 1: Centre-only — symmetric blocks (F5,F6) and (F6,F7) tie;
    # algorithm picks first encountered (lower col) — both are equally centre.
    seat_all_ok &= _run_scoring_test(
        "2 tickets — centre block (F5,F6 or F6,F7, symmetric tie)",
        test_seats, 2,
        ["F5", "F6"],
    )

    # --- Test 2: 4 adjacent — two equally good blocks (F4-F7 and F5-F8);
    # both sum to same total score, lower-col block wins tie.
    seat_all_ok &= _run_scoring_test(
        "4 tickets — centre block (symmetric tie → lower col wins)",
        test_seats, 4,
        ["F4", "F5", "F6", "F7"],
    )

    # --- Test 3: 3 adjacent — again (F5,F6,F7) and (F6,F7,F8) tie.
    seat_all_ok &= _run_scoring_test(
        "3 tickets — centre block (symmetric tie → lower col wins)",
        test_seats, 3,
        ["F5", "F6", "F7"],
    )

    # --- Test 4: Remove F5-F8 from row F — no 4-adjacent block in any row.
    # Row G is next-best (closest to ideal row), picks block G4-G7.
    sparse_seats = [s for s in test_seats if s["id"] not in {"F5", "F6", "F7", "F8"}]
    seat_all_ok &= _run_scoring_test(
        "4 tickets — fallback (no contiguous block in row F)",
        sparse_seats, 4,
        ["G4", "G5", "G6", "G7"],
    )

    # --- Test 5: Price and category are completely ignored.
    # ₹50 front-row seats lose to ₹500 centre-row seats.
    cheap_front = [
        {"id": "A5", "row": "A", "col": 5, "category": "BUDGET", "price": 50, "status": "available"},
        {"id": "A6", "row": "A", "col": 6, "category": "BUDGET", "price": 50, "status": "available"},
        {"id": "A7", "row": "A", "col": 7, "category": "BUDGET", "price": 50, "status": "available"},
        {"id": "F5", "row": "F", "col": 5, "category": "PREMIUM", "price": 500, "status": "available"},
        {"id": "F6", "row": "F", "col": 6, "category": "PREMIUM", "price": 500, "status": "available"},
        {"id": "F7", "row": "F", "col": 7, "category": "PREMIUM", "price": 500, "status": "available"},
        {"id": "L5", "row": "L", "col": 5, "category": "BUDGET", "price": 50, "status": "available"},
        {"id": "L6", "row": "L", "col": 6, "category": "BUDGET", "price": 50, "status": "available"},
    ]
    seat_all_ok &= _run_scoring_test(
        "Price ignored — centre block beats cheap front/back rows",
        cheap_front, 2,
        ["F5", "F6"],
    )

    print("=" * 60)
    if seat_all_ok:
        print("✅ All seat-scoring tests passed.\n")
    else:
        print("❌ Some seat-scoring tests FAILED.\n")

    # ------------------------------------------------------------------
    # Browser-based smoke test
    # ------------------------------------------------------------------
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    if not os.path.exists(config_path):
        print(f"❌ config.json not found at {config_path}")
        sys.exit(1)

    with open(config_path, "r") as fh:
        config = json.load(fh)

    bms = BMSPlaywrightAutomator(config)

    print("=" * 60)
    print("BookMyShow Playwright Automator — Browser Test")
    print("=" * 60)
    print(f"  City:          {bms.city}")
    print(f"  Headless:      {bms.headless}")
    print(f"  User-Agent:    {bms._pick_user_agent()}")
    print("=" * 60)

    browser = None
    try:
        browser, context, page = await bms.start()
        print(f"✅ Browser launched. Page title: {await page.title()}")
        print("ℹ️  No login required — BMS asks for email/phone at payment stage.")

        # Check if contact details and gift card credentials are present
        try:
            creds = bms._load_credentials()
            user = creds.get("user_details", {})
            if user.get("email") and user.get("phone"):
                print(f"📧 Contact: {user['email']} / {user['phone']}")
            else:
                print("⚠️  Contact details NOT SET — run: python setup_creds.py")
        except Exception:
            pass

        try:
            creds = bms._load_credentials()
            gc = creds.get("gift_card")
            if isinstance(gc, dict) and gc.get("e_code") and gc.get("pin"):
                e_code = gc["e_code"]
                print(f"🎁 Gift Card: e‑code={e_code[:4]}**** (PIN hidden)")
            else:
                print("⚠️  Gift Card: NOT SET — complete_payment() will fail.")
                if creds.get("wallet_pin") or creds.get("walletpin"):
                    print("     ⚠️  Legacy wallet_pin detected — please re‑run:")
                    print("        python setup_creds.py")
                    print("     to store gift card details instead.")
                else:
                    print("     Run: python setup_creds.py")
        except Exception as exc:
            print(f"⚠️  Could not check gift card credentials: {exc}")

        # --- Demo 1: Search mode (backward compatible) ------------------
        bookings = config.get("booking_requests", [])
        if bookings:
            req = bookings[0]
            print(f"\n--- Demo 1: Search mode for '{req['movie_name']}' ---")
            shows = await bms.find_shows(
                page,
                req["movie_name"],
                req["date"],
                (18, 22),
            )
            print(f"🎬 Search mode — found {len(shows)} shows:")
            for s in shows:
                code = s.get("_unique_code", "?")
                print(f"    {s['venue']} — {s['show_time']} — ₹{s.get('price', '?')} — code={code}")

        # --- Demo 2: Direct URL mode ------------------------------------
        print("\n--- Demo 2: Direct-URL mode ---")
        sample_url = (
            "https://in.bookmyshow.com/movies/chennai/"
            "spider-man-brand-new-day/buytickets/ET00447840/20260802"
        )
        shows_url = await bms.find_shows(
            page,
            "Spider-Man: Brand New Day",   # movie_name for logging
            "",                             # date override: empty → use URL date
            (10, 24),                       # wide window to get everything
            booking_url=sample_url,
        )
        print(f"🎬 Direct-URL mode — found {len(shows_url)} shows:")
        for s in shows_url:
            code = s.get("_unique_code", "?")
            city = s.get("_city", "?")
            print(f"    {s['venue']} — {s['show_time']} — ₹{s.get('price', '?')} — "
                  f"code={code} city={city}")

    except BMSAutomationError as exc:
        print(f"❌ Automation error: {exc}")
    except Exception as exc:
        logger.exception("Unexpected error during self-test.")
    finally:
        if browser:
            await browser.close()
            print("🛑 Browser closed.")


if __name__ == "__main__":
    asyncio.run(_self_test())
