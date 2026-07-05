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

# Movie page URL patterns before the buytickets page is opened.
# Supports both:
#   https://in.bookmyshow.com/{city}/movies/{movie-slug}/{ET...}
#   https://in.bookmyshow.com/movies/{city}/{movie-slug}/{ET...}
_MOVIE_PAGE_URL_RES = [
    re.compile(
        r"https?://in\.bookmyshow\.com"
        r"/(?P<city>[^/]+)"
        r"/movies/"
        r"(?P<movie_slug>[^/]+)"
        r"/(?P<unique_code>ET\d+)"
        r"(?:/.*)?$"
    ),
    re.compile(
        r"https?://in\.bookmyshow\.com"
        r"/movies/"
        r"(?P<city>[^/]+)"
        r"/(?P<movie_slug>[^/]+)"
        r"/(?P<unique_code>ET\d+)"
        r"(?:/.*)?$"
    ),
]

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


def parse_movie_page_url(url: str) -> Optional[Dict[str, str]]:
    """Parse a BMS movie page URL that includes the ET unique code."""
    match = None
    for pattern in _MOVIE_PAGE_URL_RES:
        match = pattern.match(url)
        if match:
            break
    if not match:
        logger.debug("URL does not match BMS movie-page pattern: %s", url)
        return None
    return {
        "city": match.group("city"),
        "movie_slug": match.group("movie_slug"),
        "unique_code": match.group("unique_code"),
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
                page, booking_url, date, time_range,
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

        # Handle the BMS region/city selection popup first.
        await self._handle_region_popup(page, city=self.city)
        await page.wait_for_timeout(1500)

        home_url = f"{BMS_BASE_URL}/explore/home/{city_slug}"
        if page.url != home_url:
            logger.info("[search] Navigating to city home page: %s", home_url)
            await page.goto(home_url, wait_until="domcontentloaded", timeout=_NAVIGATION_TIMEOUT)
            await page.wait_for_timeout(10000)

        async def _find_visible(selector_list: List[str]) -> Optional[Tuple[str, Any]]:
            for selector in selector_list:
                try:
                    matches = await page.query_selector_all(selector)
                    for match in matches:
                        if match and await match.is_visible():
                            return selector, match
                except Exception:
                    continue
        shell_selectors = [
            "span:has-text(\"Search for Movies\")",
            "[class*='search'][class*='ellipsis']",
            "[data-testid='search-trigger']",
            "[class*='search-icon']",
        ]
        shell = await _find_visible(shell_selectors)
        if shell is None:
            raise RuntimeError("Search shell did not appear on the city home page")
        logger.info("Clicked search shell.")
        await shell[1].click(force=True)

        input_selectors = [
            "input[type='text']",
            "input[type='search']",
            "input[placeholder*='Search']",
            "input[class*='search']",
        ]
        movie_input = None
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline and movie_input is None:
            movie_input = await _find_visible(input_selectors)
            if movie_input is None:
                await page.wait_for_timeout(250)
        if movie_input is None:
            raise RuntimeError("Search input did not appear after clicking shell")
        logger.info("Search input appeared.")
        movie_box = movie_input[1]
        await movie_box.click()
        await movie_box.fill("")
        await movie_box.type(movie_name, delay=80)
        await page.wait_for_timeout(1000)

        suggestion_selectors = [
            f"li:has-text('{movie_name}')",
            f"[class*='suggestion']:has-text('{movie_name}')",
            f"[class*='result']:has-text('{movie_name}')",
            f"a:has-text('{movie_name}')",
        ]
        suggestion = None
        deadline = asyncio.get_running_loop().time() + 3.0
        while asyncio.get_running_loop().time() < deadline and suggestion is None:
            suggestion = await _find_visible(suggestion_selectors)
            if suggestion is None:
                await page.wait_for_timeout(250)
        if suggestion is None:
            raise RuntimeError(f"No search suggestion found for '{movie_name}'")
        await suggestion[1].click(force=True)
        logger.info("Clicked suggestion for '%s'.", movie_name)

        suggestion_href = await suggestion[1].get_attribute("href")
        if not suggestion_href:
            raise RuntimeError(f"Clicked suggestion for '{movie_name}' did not expose an href")
        current_url = suggestion_href if suggestion_href.startswith("http") else f"{BMS_BASE_URL}{suggestion_href}"
        movie_page_parsed = parse_movie_page_url(current_url) or parse_booking_url(current_url)
        if movie_page_parsed is None:
            raise RuntimeError(f"Could not extract movie code from URL: {current_url}")
        movie_code = movie_page_parsed["unique_code"]
        movie_slug = movie_page_parsed["movie_slug"]
        requested_slug = _slugify(movie_name)
        if movie_slug != requested_slug:
            raise RuntimeError(
                f"Landing page movie slug '{movie_slug}' does not match requested movie '{movie_name}'"
            )
        logger.info("Extracted movie code: %s.", movie_code)

        date_key = date.replace("-", "")
        booking_url = (
            f"{BMS_BASE_URL}/movies/{self._city_slug()}/"
            f"{movie_slug}/buytickets/{movie_code}/{date_key}"
        )
        logger.info("Built booking URL: %s.", booking_url)
        return await self._find_shows_from_url(page, booking_url, date, time_range)

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

            await self._handle_region_popup(page)
            await page.wait_for_timeout(1500)
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
        booking_url: str,
        date: str,
        time_range: Tuple[int, int],
    ) -> List[Dict[str, Any]]:
        """
        Navigate directly to a BMS booking URL and scrape showtimes.

        The date embedded in the URL is parsed; if *date* is non-empty it
        overrides the URL date.
        """
        start_hour, end_hour = time_range
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
            "[direct-url] Navigating to '%s' — slug=%s on %s, time window %02d:00–%02d:00",
            booking_url, parsed["movie_slug"], effective_date, start_hour, end_hour,
        )

        # Navigate.
        await page.goto(booking_url, wait_until="domcontentloaded", timeout=_NAVIGATION_TIMEOUT)
        await page.wait_for_timeout(3000)
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        # Handle the BMS region/city selection popup only if the page is
        # actually showing one; otherwise the header city label can be
        # mistaken for a modal and we end up clicking away from the show list.
        parsed_city = parsed.get("city", "").capitalize()
        await self._handle_region_popup(page, city=parsed_city or self.city)
        await page.wait_for_timeout(1500)

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

        **Key design decisions to prevent wrong-cinema clicks:**

        * Cinema containers are identified by finding elements that contain
          BOTH a cinema-name heading AND showtime buttons — not by generic
          section/div selectors.
        * Cinema name is extracted ONLY from the heading element inside
          each container (not from arbitrary text lines).
        * Every show dict stores a stable CSS selector so the button can be
          **re-located** later (ElementHandles go stale).
        * Each scraped show is logged individually so you can visually
          verify cinema names match the page.
        """
        shows: List[Dict[str, Any]] = []
        await page.wait_for_timeout(2000)
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        time_pattern = re.compile(r"\b\d{1,2}:\d{2}\s*(?:AM|PM)\b", re.IGNORECASE)

        # =================================================================
        # Phase 1 — Find cinema containers by looking for elements that
        #           have BOTH a cinema-name heading AND showtime buttons.
        # =================================================================
        cinema_containers: List[Any] = []

        # Strategy A: look for containers that contain a link to /cinemas/
        # (the cinema name heading) AND a link to /buytickets/ (showtime).
        for sel in [
            # divs/sections containing BOTH a cinema link AND a booking link
            "div:has(a[href*='/cinemas/']):has(a[href*='/buytickets/'])",
            "li:has(a[href*='/cinemas/']):has(a[href*='/buytickets/'])",
            "section:has(a[href*='/cinemas/']):has(a[href*='/buytickets/'])",
            # BMS-specific test-id patterns
            "[data-testid='cinema-container']",
            "[data-testid='venue-container']",
            # venue / cinema card classes
            "[class*='venue-card']",
            "[class*='cinema-card']",
            "[class*='theatre-card']",
            "[class*='VenueCard']",
            "[class*='CinemaCard']",
            "[class*='listing-card']",
        ]:
            try:
                matches = await page.query_selector_all(sel)
                visible_matches = []
                for m in matches:
                    try:
                        if await m.is_visible():
                            text = (await m.inner_text()).strip()
                            if text and time_pattern.search(text):
                                visible_matches.append(m)
                    except Exception:
                        continue
                if visible_matches:
                    cinema_containers = visible_matches
                    logger.debug("[shows] Found %d cinema containers via '%s'.", len(cinema_containers), sel)
                    break
            except Exception:
                continue

        # Strategy B: if no containers found via structural selectors,
        # find all showtime links, walk up the DOM to find their parent
        # cinema containers, and group them.
        if not cinema_containers:
            logger.debug("[shows] No structural cinema containers — falling back to showtime-first detection.")
            showtime_els: List[Any] = []
            for sel in [
                "a[href*='/buytickets/']",
                "a[href*='seat-layout']",
                "[data-testid='showtime']",
                ".showtime-btn",
                ".showtime",
                "[class*='showtime']",
            ]:
                try:
                    matches = await page.query_selector_all(sel)
                    for m in matches:
                        try:
                            if await m.is_visible():
                                text = (await m.inner_text()).strip()
                                if time_pattern.search(text):
                                    showtime_els.append(m)
                        except Exception:
                            continue
                except Exception:
                    continue
                if showtime_els:
                    break

            if not showtime_els:
                # Last resort: any button/link with a time pattern.
                for sel in ["a", "button", "[role='button']", "div", "span"]:
                    try:
                        matches = await page.query_selector_all(sel)
                        for m in matches:
                            try:
                                if await m.is_visible():
                                    text = (await m.inner_text()).strip()
                                    if time_pattern.search(text):
                                        showtime_els.append(m)
                            except Exception:
                                continue
                    except Exception:
                        continue
                    if showtime_els:
                        break

            # Group showtime elements by their parent cinema name.
            cinema_groups: Dict[str, List[Any]] = {}
            for time_el in showtime_els:
                cinema_name = await self._find_parent_cinema_for_showtime(page, time_el)
                if not cinema_name:
                    continue
                cinema_name = cinema_name.strip()
                cinema_groups.setdefault(cinema_name, []).append(time_el)

            # Synthesise container info — we don't have real container
            # ElementHandles, but we have cinema_name → [showtime_els].
            # Store as tuples for the next loop.
            cinema_containers = [(name, els) for name, els in cinema_groups.items()]
            logger.debug("[shows] Grouped %d cinemas from %d showtime elements (fallback mode).",
                         len(cinema_containers), len(showtime_els))

        if not cinema_containers:
            screenshot_path = f"screenshots/show_scrape_{int(datetime.utcnow().timestamp())}.png"
            try:
                os.makedirs("screenshots", exist_ok=True)
                await page.screenshot(path=screenshot_path, full_page=True)
                logger.warning("[shows] No cinema containers found; screenshot saved to %s", screenshot_path)
            except Exception as exc:
                logger.warning("[shows] Could not save scrape screenshot: %s", exc)
            raise RuntimeError("No cinema containers found on the booking page.")

        # =================================================================
        # Phase 2 — For each cinema container, extract:
        #           1. Cinema name (from heading ONLY)
        #           2. Showtime buttons (from THIS container ONLY)
        #           3. Stable CSS selector for re-location
        # =================================================================
        seen: set[tuple[str, str, str]] = set()

        for item in cinema_containers:
            # --- Unwrap item: may be (ElementHandle) or (str, [ElementHandle]) ---
            if isinstance(item, tuple):
                # Fallback mode: (cinema_name, [showtime_elements])
                cinema_name, time_els = item
                venue_el = None
            else:
                # Normal mode: item is a Playwright ElementHandle
                venue_el = item
                cinema_name = ""
                time_els = []

                # --- Extract cinema name from THIS container's heading ONLY ---
                # Priority: <a> linking to /cinemas/ (most reliable BMS pattern),
                # then <h3>/<h4>, then any heading.
                heading_selectors_in_order = [
                    "a[href*='/cinemas/']",
                    "a[href*='/theatres/']",
                    "h3",
                    "h4",
                    "h2",
                    "h5",
                    "strong",
                    "[class*='venue-name']",
                    "[class*='cinema-name']",
                    "[class*='VenueName']",
                    "[class*='CinemaName']",
                ]
                for h_sel in heading_selectors_in_order:
                    try:
                        heading_els = await venue_el.query_selector_all(h_sel)
                        for heading_el in heading_els:
                            try:
                                if not await heading_el.is_visible():
                                    continue
                                heading_text = (await heading_el.inner_text()).strip()
                            except Exception:
                                continue
                            # Reject if it looks like a showtime or action button.
                            if not heading_text:
                                continue
                            if time_pattern.search(heading_text):
                                continue
                            if re.search(r"^(book|select|cancel|proceed|pay|continue|buy|close|skip)$",
                                         heading_text, re.I):
                                continue
                            if len(heading_text) >= 3:
                                cinema_name = heading_text
                                break
                        if cinema_name:
                            break
                    except Exception:
                        continue
                    if cinema_name:
                        break

                # --- Extract showtime elements from THIS container ONLY ---
                for tsel in [
                    "a[href*='/buytickets/']",
                    "a[href*='seat-layout']",
                    "[data-testid='showtime']",
                    ".showtime-btn",
                    ".showtime",
                    "[class*='showtime']",
                ]:
                    try:
                        matches = await venue_el.query_selector_all(tsel)
                        for match in matches:
                            try:
                                if not await match.is_visible():
                                    continue
                                time_text = (await match.inner_text()).strip()
                            except Exception:
                                continue
                            if time_pattern.search(time_text):
                                time_els.append(match)
                    except Exception:
                        continue
                    if time_els:
                        break

                # Fallback: generic button/a inside container.
                if not time_els:
                    for tsel in ["a", "button", "[role='button']", "div", "span"]:
                        try:
                            matches = await venue_el.query_selector_all(tsel)
                            for match in matches:
                                try:
                                    if not await match.is_visible():
                                        continue
                                    time_text = (await match.inner_text()).strip()
                                except Exception:
                                    continue
                                if time_pattern.search(time_text):
                                    time_els.append(match)
                        except Exception:
                            continue

                # --- If still no cinema name, try fallback extraction ---
                if not cinema_name:
                    try:
                        venue_text = (await venue_el.inner_text()).strip()
                        raw_lines = [line.strip() for line in venue_text.splitlines() if line.strip()]
                        for line in raw_lines:
                            if not time_pattern.search(line) and not re.search(
                                r"^cancelled?|^sold out|^no seats|^book now$|^\d{1,2}:\d{2}",
                                line, re.I,
                            ):
                                if len(line) >= 3:
                                    cinema_name = line
                                    break
                    except Exception:
                        pass

            if not cinema_name:
                logger.debug("[shows] Skipping container — could not determine cinema name.")
                continue

            # =============================================================
            # Process each showtime element in this cinema
            # =============================================================
            for time_el in time_els:
                try:
                    raw_text = (await time_el.inner_text()).strip()
                except Exception:
                    continue

                match = time_pattern.search(raw_text)
                if not match:
                    continue

                time_text = match.group(0).upper()
                hour = self._extract_hour(time_text)
                if hour is None or not (start_hour <= hour < end_hour):
                    continue

                href = None
                try:
                    href = await time_el.get_attribute("href")
                except Exception:
                    pass
                show_url = None
                if href:
                    show_url = href if href.startswith("http") else f"{BMS_BASE_URL}{href}"

                # --- Build a stable CSS selector for re-locating this button later ---
                show_selector = await self._build_show_selector(
                    page, time_el, cinema_name, time_text,
                )

                price: Optional[float] = self._extract_price(raw_text)
                if price is None:
                    try:
                        parent_text = (await time_el.evaluate(
                            "el => el.parentElement?.innerText || ''"
                        )).strip()
                        price = self._extract_price(parent_text)
                    except Exception:
                        pass

                dedupe_key = (cinema_name.lower(), time_text, show_url or "")
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)

                shows.append({
                    "cinema": cinema_name,
                    "venue": cinema_name,
                    "show_time": time_text,
                    "show_element": time_el,
                    "_showtime_element": time_el,
                    "_show_selector": show_selector,
                    "_cinema_name": cinema_name,
                    "_show_time_text": time_text,
                    "show_url": show_url,
                    "price": price,
                })

                logger.info(
                    '[shows] CINEMA: "%s" | TIME: "%s" | HAS ELEMENT: True | HAS URL: %s',
                    cinema_name, time_text, bool(show_url),
                )

        if not shows:
            screenshot_path = f"screenshots/show_scrape_{int(datetime.utcnow().timestamp())}.png"
            try:
                os.makedirs("screenshots", exist_ok=True)
                await page.screenshot(path=screenshot_path, full_page=True)
                logger.warning("[shows] Scrape found containers but no showtimes; screenshot saved to %s", screenshot_path)
            except Exception as exc:
                logger.warning("[shows] Could not save scrape screenshot: %s", exc)
            raise RuntimeError("No valid showtimes found on the booking page.")

        logger.info("Scraped %d showtimes matching the criteria.", len(shows))
        return shows

    # ------------------------------------------------------------------
    # _find_parent_cinema_for_showtime — walk up DOM from showtime button
    # ------------------------------------------------------------------

    async def _find_parent_cinema_for_showtime(
        self,
        page: "playwright.async_api.Page",
        time_el: "playwright.async_api.ElementHandle",
    ) -> Optional[str]:
        """
        Walk up the DOM from a showtime element to find the cinema name.

        Used as a fallback when structural cinema containers cannot be
        identified by CSS selectors alone.  Evaluates JavaScript to climb
        parent nodes looking for cinema-name patterns (links to /cinemas/,
        heading tags, or venue-name classed elements).
        """
        try:
            cinema_name = await time_el.evaluate("""
                (el) => {
                    let current = el;
                    for (let i = 0; i < 8; i++) {
                        if (!current || !current.parentElement) break;
                        current = current.parentElement;

                        // Priority 1: <a> linking to /cinemas/ or /theatres/
                        const cinemaLinks = current.querySelectorAll(
                            'a[href*="/cinemas/"], a[href*="/theatres/"]'
                        );
                        for (const link of cinemaLinks) {
                            const text = link.textContent.trim();
                            if (text.length >= 3 && text.length < 100
                                && !/\\d{1,2}:\\d{2}\\s*(AM|PM)/i.test(text)) {
                                return text;
                            }
                        }

                        // Priority 2: heading elements
                        const headings = current.querySelectorAll(
                            'h1, h2, h3, h4, h5, h6, strong, '
                            + '[class*="venue-name"], [class*="cinema-name"], '
                            + '[class*="VenueName"], [class*="CinemaName"], '
                            + '[class*="theatre-name"], [class*="TheatreName"]'
                        );
                        for (const h of headings) {
                            const text = h.textContent.trim();
                            if (text.length >= 3 && text.length < 100
                                && !/\\d{1,2}:\\d{2}\\s*(AM|PM)/i.test(text)
                                && !/^(book|select|cancel|proceed|pay|continue|buy|close|skip)$/i.test(text)) {
                                return text;
                            }
                        }
                    }
                    return null;
                }
            """)
            return cinema_name
        except Exception as exc:
            logger.debug("[shows] Failed to walk-up DOM for cinema name: %s", exc)
            return None

    # ------------------------------------------------------------------
    # _build_show_selector — stable CSS selector for re-locating a showtime button
    # ------------------------------------------------------------------

    async def _build_show_selector(
        self,
        page: "playwright.async_api.Page",
        time_el: "playwright.async_api.ElementHandle",
        cinema_name: str,
        time_text: str,
    ) -> str:
        """
        Build a stable CSS selector string that can re-locate *time_el*
        after navigation / DOM mutations.

        Tries, in order:

        1. A unique data-attribute on the button itself (``data-testid``,
           ``data-showtime``, ``id``).
        2. A parent-container selector (id or data attribute) combined
           with a ``:has-text()`` filter for the showtime.
        3. A Playwright locator chain: ``text=<cinema> >> text=<time>``
           (the ``>>`` operator means "inside" in Playwright).
        """
        # --- Strategy 1: unique attribute on the button itself ---
        for attr in ["data-testid", "data-showtime", "data-showtime-id",
                      "data-time", "data-venue", "id"]:
            try:
                value = await time_el.get_attribute(attr)
                if value:
                    # Escape double-quotes in the value.
                    safe_val = value.replace('"', '\\"')
                    return f'[{attr}="{safe_val}"]'
            except Exception:
                continue

        # --- Strategy 2: parent container selector + :has-text() ---
        try:
            parent_sel = await time_el.evaluate("""
                (el) => {
                    let current = el;
                    for (let i = 0; i < 6; i++) {
                        if (!current || !current.parentElement) break;
                        current = current.parentElement;
                        const id = current.getAttribute('id');
                        if (id) return '#' + CSS.escape(id);
                        const testid = current.getAttribute('data-testid');
                        if (testid) return '[data-testid="' + testid.replace(/"/g, '\\\\"') + '"]';
                        const venue = current.getAttribute('data-venue');
                        if (venue) return '[data-venue="' + venue.replace(/"/g, '\\\\"') + '"]';
                        const cinema = current.getAttribute('data-cinema');
                        if (cinema) return '[data-cinema="' + cinema.replace(/"/g, '\\\\"') + '"]';
                    }
                    return null;
                }
            """)
            if parent_sel:
                escaped_time = time_text.replace('"', '\\"')
                # Look for link or button inside the parent with this time text.
                return f'{parent_sel} a:has-text("{escaped_time}"), {parent_sel} button:has-text("{escaped_time}")'
        except Exception:
            pass

        # --- Strategy 3: Playwright text locator chain ---
        # This is a Playwright-specific selector using >> (inside) chaining.
        # It finds an element containing cinema_name, then inside it,
        # an element containing time_text.
        escaped_cinema = cinema_name.replace('"', '\\"')
        escaped_time = time_text.replace('"', '\\"')
        return f'text="{escaped_cinema}" >> text="{escaped_time}"'

    # ------------------------------------------------------------------
    # Internal: overlay / popup dismissal
    # ------------------------------------------------------------------

    async def _dismiss_overlays(self, page: "playwright.async_api.Page") -> None:
        """Attempt to close common overlays, popups, and location prompts."""
        dismiss_selectors = [
            # BMS-specific bottom-sheet / welcome modal close button.
            "[data-testid='modalClose']",
            "#bottomSheet-model-close",
            "div[id*='bottomSheet'] [data-testid='modalClose']",
            "div[id*='bottomSheet'] button",
            # Generic close / dismiss buttons.
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
    # Internal: region / city popup handling
    # ------------------------------------------------------------------

    async def _handle_region_popup(
        self,
        page: "playwright.async_api.Page",
        city: Optional[str] = None,
    ) -> bool:
        """
        Handle the BMS region/city selection popup that appears on first visit.

        BMS often shows a modal asking the user to select their city before
        allowing interaction with the rest of the page.  This method:

        1. Waits up to 5 seconds for a city-selection modal to appear.
        2. Tries, in order:
              a) Type *city* into the region search box and press Enter.
              b) Click the city name matching *city* (if provided).
              c) Click a close / skip button to dismiss the popup.
        3. If nothing is found within the timeout, logs a warning and
           returns ``False`` (the popup may not have appeared at all).

        Parameters
        ----------
        page : Page
            The Playwright page that may be showing the popup.
        city : str, optional
            The desired city name (e.g. ``"Coimbatore"``).  If the popup
            lists city names and this is provided, the matching city will
            be clicked.  Defaults to ``self.city`` when omitted.

        Returns
        -------
        bool
            ``True`` if a popup was detected and handled, ``False`` otherwise.
        """
        if city is None:
            city = self.city
        city = city.strip()

        logger.info("[region-popup] Checking for city/region selection popup …")

        async def _first_visible(selector_list: List[str]) -> Optional[Tuple[str, Any]]:
            for selector in selector_list:
                try:
                    element = await page.query_selector(selector)
                    if element and await element.is_visible() and await _looks_like_popup(element):
                        return selector, element
                except Exception:
                    continue
            return None

        async def _first_visible_text_match(text: str) -> Optional[Tuple[str, Any]]:
            pattern = re.compile(rf"^{re.escape(text)}$", re.IGNORECASE)
            candidate = page.locator("button, a, [role='button'], li, div, span").filter(has_text=pattern)
            try:
                count = await candidate.count()
                for index in range(count):
                    element = candidate.nth(index)
                    if await element.is_visible() and await _looks_like_popup(element):
                        return f"text=/{re.escape(text)}/i", element
            except Exception:
                return None
            return None

        async def _first_visible_input() -> Optional[Tuple[str, Any]]:
            role_selectors = [
                "textbox",
                "searchbox",
            ]
            for role in role_selectors:
                try:
                    locator = page.get_by_role(role)
                    count = await locator.count()
                    for index in range(count):
                        element = locator.nth(index)
                        if await element.is_visible() and await _looks_like_popup(element):
                            return f"role={role}", element
                except Exception:
                    continue

            input_selectors = [
                "input[placeholder='Search for your city']",
                "input[placeholder='Search for your city' i]",
                "input[placeholder*='city' i]",
                "input[placeholder*='location' i]",
                "input[placeholder*='search' i]",
                "input[type='search']",
                "[role='searchbox']",
                "[role='textbox']",
                "[contenteditable='true']",
                "textarea",
                "input[type='text']",
            ]
            for selector in input_selectors:
                try:
                    elements = await page.query_selector_all(selector)
                    for element in elements:
                        if element and await element.is_visible() and await _looks_like_popup(element):
                            return selector, element
                except Exception:
                    continue
            return None

        async def _looks_like_popup(element: Any) -> bool:
            try:
                return bool(await element.evaluate(
                    """
                    (el) => {
                        const popupLike = (node) => {
                            if (!node || !node.getAttribute) return false;
                            const role = (node.getAttribute('role') || '').toLowerCase();
                            if (role === 'dialog' || role === 'alertdialog') return true;
                            if ((node.getAttribute('aria-modal') || '').toLowerCase() === 'true') return true;
                            const label = `${node.id || ''} ${node.className || ''}`;
                            return /modal|popup|dialog|overlay|sheet|drawer/i.test(label);
                        };

                        let current = el;
                        for (let depth = 0; current && depth < 8; depth += 1, current = current.parentElement) {
                            if (popupLike(current)) return true;
                        }
                        return false;
                    }
                    """
                ))
            except Exception:
                return False

        region_indicators = [
            "text=/select.*city/i",
            "text=/choose.*city/i",
            "text=/detect.*location/i",
            "text=/your.*city/i",
            "text=/popular.*cities/i",
            "text=/select.*region/i",
            "[data-testid='city-modal']",
            "[data-testid='region-modal']",
            ".city-modal",
            ".region-modal",
            ".city-selector",
            "[class*='city-select']",
            "[class*='region-select']",
        ]
        city_selectors = [
            f"text=/^{re.escape(city)}$/i",
            f"a:has-text('{city}')",
            f"button:has-text('{city}')",
            f"[role='button']:has-text('{city}')",
            f"span:has-text('{city}')",
            f"div:has-text('{city}')",
            f"li:has-text('{city}')",
            f"[class*='city']:has-text('{city}')",
            f"[class*='City']:has-text('{city}')",
        ]
        close_selectors = [
            "button[aria-label='Close']",
            "[aria-label='Close']",
            "[data-testid='close-btn']",
            ".close-btn",
            ".modal-close",
            ".popup-close",
            "button:has-text('✕')",
            "button:has-text('×')",
            "button:has-text('X')",
            "span:has-text('✕')",
            "span:has-text('×')",
            "text=✕",
            "text=×",
            "button[class*='close' i]",
            "button[class*='dismiss' i]",
            "svg[class*='close' i]",
            ".modal button:first-child",
        ]
        deadline = asyncio.get_running_loop().time() + 5.0
        popup_detected = False
        while asyncio.get_running_loop().time() < deadline:
            try:
                found = await _first_visible(region_indicators + close_selectors)
                if found:
                    logger.debug("[region-popup] Popup detected via: %s", found[0])
                    popup_detected = True
                    break
            except Exception:
                pass
            await page.wait_for_timeout(250)

        if not popup_detected:
            logger.warning("[region-popup] No region/city popup detected within 5 seconds.")
            return False

        input_match = await _first_visible_input()
        if input_match:
            logger.info("[region-popup] Typing city '%s' into region search box: %s", city, input_match[0])
            await input_match[1].click()
            await page.wait_for_timeout(200)
            await input_match[1].fill("")
            await input_match[1].type(city, delay=60)
            await page.wait_for_timeout(500)
            await input_match[1].press("Enter")
            await page.wait_for_timeout(1500)
            logger.info("[region-popup] ✅ Region popup handled — city '%s' submitted via search box.", city)
            return True

        city_match = await _first_visible_text_match(city)
        if city_match is None:
            city_match = await _first_visible(city_selectors)
        if city_match:
            logger.info("[region-popup] Selecting city '%s' via: %s", city, city_match[0])
            await city_match[1].click()
            try:
                await page.evaluate(
                    "city => { try { localStorage.setItem('preferredCity', city); localStorage.setItem('selectedCity', city); } catch (e) {} }",
                    city,
                )
            except Exception:
                pass
            await page.wait_for_timeout(1500)
            logger.info("[region-popup] ✅ Region popup handled — city '%s' selected.", city)
            return True

        close_match = await _first_visible(close_selectors)
        if close_match:
            logger.info("[region-popup] Clicking close button: %s", close_match[0])
            await close_match[1].click()
            await page.wait_for_timeout(1500)
            logger.info("[region-popup] ✅ Region popup dismissed via close button.")
            return True

        logger.warning(
            "[region-popup] Popup was detected but no matching city search box, city button, or close control was found."
        )
        return False

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
        time_range: Optional[Tuple[int, int]] = None,
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
        await self._navigate_to_seat_selection(page, show, time_range=time_range)

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

        # --- 2b. SAFETY CHECK: verify we are on the RIGHT cinema ----------
        expected_cinema = show.get("cinema") or show.get("venue") or show.get("_cinema_name") or ""
        if expected_cinema:
            cinema_ok = await self._verify_cinema_on_seat_page(page, expected_cinema)
            if cinema_ok:
                logger.info(
                    '[seats] Cinema name on seat page: "%s" matches expected ✅',
                    expected_cinema,
                )
            else:
                logger.error(
                    '[seats] ❌ Wrong cinema! Expected "%s", but seat page '
                    "shows a different cinema. Skipping this show.",
                    expected_cinema,
                )
                try:
                    os.makedirs("screenshots", exist_ok=True)
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    path = f"screenshots/wrong_cinema_{ts}.png"
                    await page.screenshot(path=path, full_page=True)
                    logger.warning("[seats] Wrong-cinema screenshot saved: %s", path)
                except Exception as exc:
                    logger.warning("[seats] Could not save wrong-cinema screenshot: %s", exc)
                return {
                    "selected_seats": [],
                    "total_price": None,
                    "category": None,
                    "attempted": False,
                    "error": (
                        f"Wrong cinema! Expected \"{expected_cinema}\", "
                        f"but landed on a different cinema."
                    ),
                }
        else:
            logger.info("[seats] ⚠️ No expected cinema name — skipping cinema verification.")

        # Some seat-layout pages open with a modal asking for ticket count.
        await self._handle_seat_count_prompt(page, num_tickets)

        # --- 3. Scrape all seat elements --------------------------------
        seats = await self._scrape_seats(page, num_tickets)
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
        time_range: Optional[Tuple[int, int]] = None,
    ) -> None:
        """
        Reach the seat map for *show*.

        **Design decisions to prevent wrong-cinema clicks:**

        1. **Direct URL is tried first** (most reliable — no DOM ambiguity).
        2. The stored ``ElementHandle`` is **never trusted directly** —
           instead the button is **re-located** using the stored CSS
           selector or by matching cinema-name + time-text.
        3. **Button text is verified** before clicking.
        4. **Post-click outcome is detected** (URL change, modal, inline
           expansion, or nothing) with detailed logging.
        5. Falls back to theater-search and showtime-on-page search with
           **cinema-context verification**.
        """
        cinema_name = show.get("cinema") or show.get("venue") or show.get("_cinema_name") or ""
        show_time_text = show.get("show_time") or show.get("_show_time_text") or ""
        show_url = show.get("show_url")
        show_selector = show.get("_show_selector", "")

        # Pre-flight: close any leftover accessibility modal from a previous attempt.
        await self._close_accessibility_modal(page)

        logger.info(
            '[seats] Attempting cinema="%s" time="%s"',
            cinema_name, show_time_text,
        )

        # =================================================================
        # Option A — Direct URL navigation (most reliable)
        # =================================================================
        if show_url:
            logger.info("[seats] Navigating to show URL: %s", show_url)
            url_before = page.url
            await page.goto(show_url, wait_until="domcontentloaded", timeout=_NAVIGATION_TIMEOUT)
            await page.wait_for_timeout(3000)
            await self._handle_region_popup(page, self.city)
            await self._dismiss_overlays(page)
            url_after = page.url

            if url_after != url_before:
                logger.info("[seats] ✅ After navigation — URL changed to: %s", url_after[:120])
            else:
                logger.info("[seats] ⚠️ After navigation — URL unchanged: %s", url_after[:120])

            # Check if we landed on seat map.
            if await self._wait_for_seat_map(page):
                logger.info("[seats] ✅ Seat map detected after direct URL navigation.")
                return

            # If not, the URL may land on an intermediate cinema page.
            # Look for a "Select Seats" / showtime button on this page.
            logger.info("[seats] Not on seat map yet — searching intermediate page for showtime trigger.")
            if await self._click_showtime_for_venue(page, cinema_name, show_time_text, time_range=time_range):
                await page.wait_for_timeout(2000)
                if await self._wait_for_seat_map(page):
                    logger.info("[seats] ✅ Seat map detected after clicking showtime on intermediate page.")
                    return

        # =================================================================
        # Option B — Re-locate the button using stored CSS selector
        # =================================================================
        if show_selector:
            logger.info('[seats] Re-locating button via stored selector: "%s"', show_selector[:80])
            try:
                button = page.locator(show_selector).first
                if await button.count() > 0 and await button.is_visible():
                    # --- Verify button text matches expected showtime ---
                    button_text = (await button.inner_text()).strip()
                    if show_time_text and show_time_text.upper() in button_text.upper():
                        logger.info(
                            '[seats] Re-located showtime button: text="%s" ✅',
                            button_text[:40],
                        )
                    else:
                        logger.warning(
                            '[seats] ❌ Button text mismatch! Expected "%s", found "%s". '
                            "Trying fallback strategies.",
                            show_time_text, button_text[:40],
                        )
                        # Don't click — fall through to Option C.
                        show_selector = ""  # invalidate
                    if show_selector:
                        # --- Click and detect outcome ---
                        logger.info("[seats] Clicking showtime button...")
                        url_before = page.url
                        await button.click()
                        if await self._detect_and_handle_post_click_state(
                            page, url_before, cinema_name,
                        ):
                            return
                else:
                    logger.warning("[seats] ⚠️ Stored selector matched no visible element.")
            except Exception as exc:
                logger.warning("[seats] ⚠️ Re-location via stored selector failed: %s", exc)

        # =================================================================
        # Option C — Find button by cinema-context + time-text matching
        # =================================================================
        logger.info(
            '[seats] Searching page for showtime button — cinema="%s" time="%s"',
            cinema_name, show_time_text,
        )

        # --- C1: Find cinema container, then find time button inside it ---
        button = await self._find_showtime_button_in_cinema_context(
            page, cinema_name, show_time_text,
        )
        if button is not None:
            # --- Verify button text ---
            try:
                button_text = (await button.inner_text()).strip()
                if show_time_text and show_time_text.upper() in button_text.upper():
                    logger.info(
                        '[seats] Found button in cinema context: text="%s" ✅',
                        button_text[:40],
                    )
                    logger.info("[seats] Clicking showtime button...")
                    url_before = page.url
                    await button.click()
                    if await self._detect_and_handle_post_click_state(
                        page, url_before, cinema_name,
                    ):
                        return
                else:
                    logger.warning(
                        '[seats] ❌ Cinema-context button mismatch! '
                        'Expected "%s", found "%s".',
                        show_time_text, button_text[:40],
                    )
            except Exception as exc:
                logger.warning("[seats] ❌ Cinema-context button click failed: %s", exc)

        # --- C2: Use theater search box ---
        if cinema_name:
            if await self._search_theatre_and_click_showtime(
                page, cinema_name, show_time_text, time_range=time_range,
            ):
                await page.wait_for_timeout(2000)
                if await self._wait_for_seat_map(page):
                    logger.info("[seats] ✅ Seat map detected after theater search.")
                    return
                # May have landed on a cinema page — try showtime click again.
                if await self._click_showtime_for_venue(
                    page, cinema_name, show_time_text, time_range=time_range,
                ):
                    await page.wait_for_timeout(2000)
                    if await self._wait_for_seat_map(page):
                        logger.info("[seats] ✅ Seat map detected after cinema-page showtime click.")
                        return

        # --- C3: Last resort — any Book / Select Seats button ---
        for sel in [
            "a:has-text('Book')",
            "button:has-text('Select Seats')",
            "button:has-text('Book')",
            "text=/Select Seats/i",
        ]:
            try:
                btn = await page.wait_for_selector(sel, timeout=2000)
                if btn:
                    logger.info("[seats] Clicking fallback: %s", sel)
                    url_before = page.url
                    await btn.click()
                    if await self._detect_and_handle_post_click_state(
                        page, url_before, cinema_name,
                    ):
                        return
            except Exception:
                continue

        logger.warning("[seats] ❌ Could not reach seat selection by any method.")

    # ------------------------------------------------------------------
    # _detect_and_handle_post_click_state — after clicking, figure out what happened
    # ------------------------------------------------------------------

    async def _detect_and_handle_post_click_state(
        self,
        page: "playwright.async_api.Page",
        url_before: str,
        cinema_name: str,
    ) -> bool:
        """
        After clicking a showtime button, detect what happened and handle it.

        Returns ``True`` if the seat map is now accessible, ``False`` otherwise.
        """
        await page.wait_for_timeout(2000)

        url_after = page.url

        # --- Check 1: URL changed → navigation occurred ---
        if url_after != url_before:
            logger.info("[seats] ✅ After click — URL changed to: %s", url_after[:120])
            await self._handle_region_popup(page, self.city)
            await self._dismiss_overlays(page)
            if await self._wait_for_seat_map(page):
                logger.info("[seats] ✅ Seat map detected on new page.")
                return True
            # May have landed on intermediate page — give showtime click another try.
            logger.info("[seats] New URL but no seat map yet — may be intermediate page.")
            return False

        # --- Check 2: Modal / overlay appeared ---
        modal_selectors = [
            "[class*='modal']:visible",
            "[class*='overlay']:visible",
            "[class*='popup']:visible",
            "[class*='dialog']:visible",
            "[role='dialog']",
            "[data-testid='seat-modal']",
            ".seat-modal",
        ]
        for sel in modal_selectors:
            try:
                modal_el = await page.query_selector(sel)
                if modal_el and await modal_el.is_visible():
                    logger.info("[seats] ✅ After click — seat map modal appeared (via '%s').", sel)
                    await page.wait_for_timeout(1000)
                    if await self._wait_for_seat_map(page):
                        logger.info("[seats] ✅ Seat map found inside modal.")
                        return True
                    break
            except Exception:
                continue

        # --- Check 3: Inline seat map expanded below cinema ---
        if await self._wait_for_seat_map(page):
            logger.info("[seats] ✅ After click — seat map appeared inline on same page.")
            return True

        # --- Nothing detected — error ---
        logger.warning("[seats] ❌ After click — URL is still: %s (unchanged)", url_after[:120])
        logger.warning("[seats] ❌ No modal, no inline seat map found.")
        try:
            os.makedirs("screenshots", exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = f"screenshots/debug_showtime_click_fail_{ts}.png"
            await page.screenshot(path=path, full_page=True)
            logger.warning("[seats] Screenshot saved: %s", path)
        except Exception as exc:
            logger.warning("[seats] Could not save debug screenshot: %s", exc)
        return False

    # ------------------------------------------------------------------
    # _find_showtime_button_in_cinema_context — find a time button near a cinema heading
    # ------------------------------------------------------------------

    async def _find_showtime_button_in_cinema_context(
        self,
        page: "playwright.async_api.Page",
        cinema_name: str,
        show_time_text: str,
    ) -> Optional[Any]:
        """
        Find a showtime button that is inside the same cinema container
        as *cinema_name*.

        Uses Playwright locator chaining: find an element containing the
        cinema name, then walk up to its container, then find a button
        inside that container with the matching time text.  This prevents
        clicking a button from a DIFFERENT cinema.
        """
        time_pattern = re.compile(r"\b\d{1,2}:\d{2}\s*(?:AM|PM)\b", re.IGNORECASE)

        # Strategy: find the cinema-name element, then find the nearest
        # common ancestor that also contains showtime buttons.
        try:
            # Find elements containing the cinema name.
            cinema_locator = page.locator(f"text={cinema_name}").first
            if await cinema_locator.count() == 0:
                # Try substring match.
                cinema_locator = page.locator(f":has-text('{cinema_name}')").first
            if await cinema_locator.count() == 0 or not await cinema_locator.is_visible():
                return None

            # Get the cinema container by walking up from the cinema name
            # element to a parent that contains time-pattern buttons.
            container_handle = await cinema_locator.evaluate_handle("""
                (el) => {
                    let current = el;
                    for (let i = 0; i < 6; i++) {
                        if (!current || !current.parentElement) break;
                        current = current.parentElement;
                        const timeLinks = current.querySelectorAll(
                            'a[href*="/buytickets/"], a[href*="seat-layout"], '
                            + 'button, a, [role="button"]'
                        );
                        for (const link of timeLinks) {
                            if (/\\d{1,2}:\\d{2}\\s*(AM|PM)/i.test(link.textContent)) {
                                return current;  // This parent has showtimes → it's the container
                            }
                        }
                    }
                    return el.closest('div, section, li');
                }
            """)

            # Within THIS container only, find the button with matching time.
            time_pattern_str = show_time_text.replace('"', '\\"')
            button = page.locator(f"text={time_pattern_str}").first

            # Verify the button we found is actually inside the cinema container.
            # If not, try a more specific locator.
            try:
                # Check if the found button's text also matches the expected time.
                btn_text = (await button.inner_text()).strip()
                if show_time_text.upper() not in btn_text.upper():
                    return None
            except Exception:
                pass

            return button

        except Exception as exc:
            logger.debug("[seats] Cinema-context button search failed: %s", exc)
            return None

    async def _search_theatre_and_click_showtime(
        self,
        page: "playwright.async_api.Page",
        venue: str,
        show_time_text: str,
        time_range: Optional[Tuple[int, int]] = None,
    ) -> bool:
        """Use the buytickets theater search to narrow to *venue* and click a showtime."""
        search_icon_candidates = page.locator("use[href*='icon-search-icon']")
        selected_icon = None
        for idx in range(await search_icon_candidates.count()):
            candidate = search_icon_candidates.nth(idx)
            try:
                if not await candidate.is_visible():
                    continue
                box = await candidate.bounding_box()
                if box and box.get("x", 0) > 900 and box.get("y", 0) > 150:
                    selected_icon = candidate
                    break
            except Exception:
                continue

        if selected_icon is not None:
            logger.info("[seats] Clicking theater search icon.")
            try:
                await selected_icon.evaluate("el => el.closest('div')?.click()")
            except Exception:
                try:
                    await selected_icon.click()
                except Exception:
                    pass
            await page.wait_for_timeout(800)
        else:
            logger.warning("[seats] Theater search icon not found; using a fallback click.")

        await self._handle_region_popup(page, self.city)
        await self._dismiss_overlays(page)

        search_input = None
        for selector in [
            "input[placeholder*='Search by cinema or area']",
            "input[placeholder*='cinema or area']",
            "input[placeholder*='search' i]",
            "input[type='text']",
        ]:
            try:
                candidate = page.locator(selector).first
                if await candidate.count() and await candidate.is_visible():
                    search_input = candidate
                    break
            except Exception:
                continue

        if search_input is None:
            logger.warning("[seats] Theater search input not found after clicking the icon.")
            return False

        logger.info("[seats] Searching theater list for %r.", venue)
        try:
            await search_input.fill(venue)
        except Exception:
            await search_input.click()
            await search_input.press("Control+A")
            await search_input.type(venue, delay=40)

        await page.wait_for_timeout(1200)
        return await self._click_showtime_for_venue(page, venue, show_time_text, time_range=time_range)

    async def _click_showtime_for_venue(
        self,
        page: "playwright.async_api.Page",
        venue: str,
        show_time_text: str,
        time_range: Optional[Tuple[int, int]] = None,
    ) -> bool:
        """Click the best matching showtime inside the currently visible theater card."""
        desired_hour = self._extract_hour(show_time_text)
        start_hour, end_hour = time_range if time_range is not None else (0, 24)
        time_pattern = re.compile(r"\b\d{1,2}:\d{2}\s*[AP]M\b", re.IGNORECASE)

        candidate_containers = page.locator("div").filter(has_text=venue)
        best_clickable = None
        best_distance = None

        for idx in range(await candidate_containers.count()):
            container = candidate_containers.nth(idx)
            try:
                if not await container.is_visible():
                    continue
                text = (await container.inner_text()).strip()
                if venue.lower() not in text.lower():
                    continue
                if not time_pattern.search(text):
                    continue
            except Exception:
                continue

            for selector in ["button", "a", "div"]:
                try:
                    clickables = container.locator(selector)
                    for cidx in range(await clickables.count()):
                        clickable = clickables.nth(cidx)
                        try:
                            if not await clickable.is_visible():
                                continue
                            txt = (await clickable.inner_text()).strip()
                            if not txt or not time_pattern.search(txt):
                                continue
                            hour = self._extract_hour(txt)
                            if hour is None:
                                continue
                            if not (start_hour <= hour < end_hour):
                                continue
                            if show_time_text and show_time_text in txt:
                                logger.info("[seats] Clicking exact showtime %r for %r.", txt, venue)
                                await clickable.click()
                                await page.wait_for_timeout(2500)
                                return True
                            target_hour = desired_hour if desired_hour is not None else hour
                            distance = abs(target_hour - hour)
                            if best_distance is None or distance < best_distance:
                                best_distance = distance
                                best_clickable = clickable
                        except Exception:
                            continue
                except Exception:
                    continue

        if best_clickable is not None:
            try:
                txt = (await best_clickable.inner_text()).strip()
            except Exception:
                txt = ""
            logger.info("[seats] Clicking nearest in-range showtime %r for %r.", txt, venue)
            try:
                await best_clickable.click()
                await page.wait_for_timeout(2500)
                return True
            except Exception as exc:
                logger.warning("[seats] Nearest showtime click failed: %s", exc)

        return False

    async def _handle_seat_count_prompt(
        self,
        page: "playwright.async_api.Page",
        num_tickets: int,
    ) -> bool:
        """Handle the initial 'How many seats?' popup if BMS shows one."""
        prompt_visible = False
        for selector in [
            "text=How many seats?",
            "text=/How many seats/i",
            "span:has-text('How many seats?')",
            "div:has-text('How many seats?')",
        ]:
            try:
                candidate = page.locator(selector).first
                if await candidate.count() and await candidate.is_visible():
                    prompt_visible = True
                    break
            except Exception:
                continue

        if not prompt_visible:
            return False

        logger.info("[seats] Seat-count prompt detected; selecting %d ticket(s).", num_tickets)

        proceed = None
        for selector in [
            "button:has-text('Select Seats')",
            "button:has-text('Continue')",
            "button:has-text('Proceed')",
        ]:
            try:
                candidate = page.locator(selector).first
                if await candidate.count() and await candidate.is_visible():
                    proceed = candidate
                    break
            except Exception:
                continue

        if proceed is None:
            logger.warning("[seats] Seat-count prompt visible but no Select Seats button found.")
            return False

        try:
            await proceed.click()
            await page.wait_for_timeout(2000)
            return True
        except Exception as exc:
            logger.warning("[seats] Failed to advance seat-count prompt: %s", exc)
            return False

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
        try:
            if "/seat-layout/" in page.url:
                logger.debug("[seats] Seat-layout route detected via URL.")
                await page.wait_for_timeout(1000)
                return True
        except Exception:
            pass

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

        # Treat the dedicated seat-layout route or its modal prompt as a
        # ready signal, but do not rely on generic SVG/canvas presence.
        try:
            if "/seat-layout/" in page.url:
                if await page.locator("text=How many seats?").count() or await page.locator("button:has-text('Select Seats')").count():
                    logger.debug("[seats] Seat layout route detected — assuming seat map is ready.")
                    return True
        except Exception:
            pass

        return False

    # ------------------------------------------------------------------
    # _verify_cinema_on_seat_page — safety check that we're on the right cinema
    # ------------------------------------------------------------------

    async def _verify_cinema_on_seat_page(
        self,
        page: "playwright.async_api.Page",
        expected_cinema: str,
    ) -> bool:
        """
        Check whether *expected_cinema* appears on the current seat map page.

        Searches the page body text, visible headings, and URL for the
        cinema name.  Returns ``True`` if a match is found (case‑insensitive
        substring), ``False`` otherwise.

        This is a **safety check** to prevent booking seats at the wrong
        cinema after a mis-click or mis-navigation.
        """
        if not expected_cinema:
            return True  # can't verify

        expected_lower = expected_cinema.strip().lower()

        # --- Check 1: page body text ----------------------------------------
        try:
            body = (await page.inner_text("body")).lower()
            if expected_lower in body:
                logger.debug("[seats] Cinema name found in page body text.")
                return True
        except Exception:
            pass

        # --- Check 2: visible headings (h1-h4) -----------------------------
        for tag in ["h1", "h2", "h3", "h4"]:
            try:
                headings = await page.query_selector_all(tag)
                for h in headings:
                    try:
                        if not await h.is_visible():
                            continue
                        text = (await h.inner_text()).strip().lower()
                        if expected_lower in text:
                            logger.debug("[seats] Cinema name found in <%s> heading.", tag)
                            return True
                    except Exception:
                        continue
            except Exception:
                continue

        # --- Check 3: URL may contain cinema slug --------------------------
        # e.g. /cinemas/pvr-brookfields/... or /theatre/...
        try:
            url_lower = page.url.lower()
            # Extract cinema-related path segments.
            cinema_slug_match = re.search(
                r"/(?:cinemas?|theatres?|venue)/([^/]+)", url_lower,
            )
            if cinema_slug_match:
                slug = cinema_slug_match.group(1).replace("-", " ")
                if expected_lower in slug or slug in expected_lower:
                    logger.debug("[seats] Cinema name matched via URL slug: %s", slug)
                    return True
        except Exception:
            pass

        # --- Check 4: cinema name near seat map element --------------------
        try:
            for sel in [
                "#seatLayoutContainer",
                "#seat-layout",
                ".seat-map",
                ".seatmap",
                "[data-testid='seat-map']",
            ]:
                el = await page.query_selector(sel)
                if el:
                    parent_text = (await el.evaluate(
                        "el => el.closest('div, section, main')?.innerText || ''"
                    )).lower()
                    if expected_lower in parent_text:
                        logger.debug("[seats] Cinema name found near seat map container.")
                        return True
                    break
        except Exception:
            pass

        logger.warning(
            "[seats] ❌ Cinema verification FAILED — '%s' not found on page.",
            expected_cinema,
        )
        return False

    # ------------------------------------------------------------------
    # _scrape_seats — extract every seat element from the page
    # ------------------------------------------------------------------

    async def _scrape_seats(
        self, page: "playwright.async_api.Page", num_tickets: int = 2,
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
                "button[aria-label*='seat' i]",
                "[role='button'][aria-label*='seat' i]",
                "[aria-label*='seat' i]",
                "[role='gridcell']",
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

        # --- Pattern B2: Accessibility-grid seats --------------------------
        if not seats:
            seats = await self._scrape_seats_from_accessibility_grid(page, num_tickets)

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
    # _scrape_seats_from_accessibility_grid — text/ARIA fallback
    # ------------------------------------------------------------------

    async def _scrape_seats_from_accessibility_grid(
        self,
        page: "playwright.async_api.Page",
        num_tickets: int = 2,
    ) -> List[Dict[str, Any]]:
        """
        Scrape seats from the accessibility seat-selection surface.

        The accessibility modal on BMS requires navigating through its UI:
        1. Verify modal is open (or open it)
        2. Set the ticket quantity to num_tickets
        3. Select the first available seat category
        4. Scrape the seat buttons from the modal
        """
        candidates: List[Dict[str, Any]] = []
        
        # Step 0: Open accessibility modal if needed
        try:
            access_button = page.get_by_role(
                "button",
                name=re.compile(r"Open accessibility|Accessibility", re.IGNORECASE),
            )
            if await access_button.count() > 0:
                try:
                    # Check if modal is already open by looking for "Accessibility Seat Selection" heading
                    heading = page.locator("h1, h2, h3, h4").filter(
                        has_text=re.compile(r"Accessibility.*Seat", re.IGNORECASE)
                    )
                    is_open = await heading.count() > 0 if heading else False
                except Exception:
                    is_open = False
                
                if not is_open and await access_button.first.is_visible():
                    logger.info("[seats] Opening accessibility seat selection surface.")
                    try:
                        await access_button.first.click()
                        await page.wait_for_timeout(1500)
                    except Exception as exc:
                        logger.debug("[seats] Accessibility button click failed: %s", exc)
        except Exception:
            pass

        # Step 1: Try to set ticket quantity using the slider or dropdown on the main page
        # (The modal might inherit this value)
        try:
            # Look for the ticket count slider/input on the main seat page
            qty_controls = [
                page.locator("[role='slider']"),  # slider control
                page.locator("input[type='range']"),  # range input
            ]
            for control in qty_controls:
                try:
                    if await control.count() > 0 and await control.first.is_visible():
                        current = (await control.first.get_attribute("aria-valuenow")) or "0"
                        if str(num_tickets) not in current:
                            # For sliders, set the value via playwright's locator
                            await control.first.evaluate(
                                f"el => {{ el.value = {num_tickets}; el.dispatchEvent(new Event('change', {{bubbles: true}})); }}"
                            )
                            await page.wait_for_timeout(500)
                            logger.info(f"[seats] Set page ticket quantity to {num_tickets}.")
                        break
                except Exception:
                    continue
        except Exception:
            pass

        # Step 2: Find and select a category from the modal's category dropdown
        # The modal shows "Select Category" with options like GOLD, SILVER, etc.
        category_selected = False
        try:
            # Strategy 1: Look for button/select with "Select Category" text
            category_selectors = [
                ("button", "Select Category"),
                ("button", "Choose"),
                ("[role='combobox']", "Select"),
                ("select", None),
            ]
            
            category_btn = None
            for selector, text_filter in category_selectors:
                try:
                    if text_filter:
                        category_locator = page.locator(selector).filter(
                            has_text=re.compile(re.escape(text_filter), re.IGNORECASE)
                        )
                    else:
                        category_locator = page.locator(selector)
                    
                    if await category_locator.count() > 0:
                        category_btn = category_locator.first
                        logger.debug(f"[seats] Found category dropdown via '{selector}' with text '{text_filter}'")
                        break
                except Exception:
                    continue
            
            if category_btn and await category_btn.is_visible():
                try:
                    await category_btn.click()
                    await page.wait_for_timeout(500)
                    logger.debug("[seats] Clicked category dropdown button")
                except Exception as e:
                    logger.debug(f"[seats] Failed to click category button: {e}")
                    category_btn = None
            
            # Step 2b: Find and click category option (GOLD, SILVER, etc.)
            if category_btn:
                # Wait for options to appear after clicking
                try:
                    # Look for visible category text anywhere on the page
                    category_pattern = re.compile(
                        r"^(GOLD|SILVER|PLATINUM|PREMIUM|EXECUTIVE|CLUB|RECLINER|BALCONY|LOUNGE)$",
                        re.IGNORECASE
                    )
                    
                    # Try multiple selector strategies for category options
                    option_selectors = [
                        ("button", None),
                        ("[role='option']", None),
                        ("li", None),
                        ("div[class*='option']", None),
                        ("div", None),
                    ]
                    
                    for opt_selector, _ in option_selectors:
                        option_locator = page.locator(opt_selector)
                        try:
                            if await option_locator.count() > 0:
                                for idx in range(min(await option_locator.count(), 50)):
                                    try:
                                        opt_el = option_locator.nth(idx)
                                        if not await opt_el.is_visible():
                                            continue
                                        
                                        opt_text = (await opt_el.inner_text()).strip().upper()
                                        if category_pattern.match(opt_text):
                                            logger.debug(f"[seats] Found category option: {opt_text}")
                                            await opt_el.click()
                                            await page.wait_for_timeout(1000)
                                            logger.info(f"[seats] Selected category: {opt_text}")
                                            category_selected = True
                                            break
                                    except Exception:
                                        continue
                                
                                if category_selected:
                                    break
                        except Exception:
                            continue
                except Exception as e:
                    logger.debug(f"[seats] Category option selection failed: {e}")
        except Exception as e:
            logger.debug(f"[seats] Category selection strategy failed: {e}")

        # Step 3: If we successfully selected a category, scrape seats from the modal
        # Also scrape if we find seat-like elements even if category selection failed
        should_scrape = category_selected
        
        # Check for seat-like elements even if category wasn't explicitly selected
        if not should_scrape:
            try:
                seat_candidates = await page.locator("button[aria-label*='seat' i], [role='button'][aria-label*='[A-Z]' i]").count()
                if seat_candidates > 0:
                    logger.debug(f"[seats] Found {seat_candidates} seat-like elements; attempting scrape")
                    should_scrape = True
            except Exception:
                pass
        
        if should_scrape:
            logger.debug(f"[seats] Attempting to scrape seats (category_selected={category_selected})")
            row_markers: List[Tuple[str, float]] = []
            
            # Find row markers (A, B, C, etc.)
            for row_letter in [chr(code) for code in range(ord("A"), ord("Z") + 1)]:
                try:
                    row_locator = page.locator(
                        "div, span, li, button"
                    ).filter(
                        has_text=re.compile(rf"^\s*{re.escape(row_letter)}\s*$", re.IGNORECASE)
                    )
                    
                    if await row_locator.count() > 0:
                        row_el = row_locator.first
                        if await row_el.is_visible():
                            box = await row_el.bounding_box()
                            if box and 100 < box["y"] < 1000:  # reasonable Y bounds for modal content
                                row_markers.append((row_letter, box["y"]))
                except Exception:
                    continue
            
            if row_markers:
                logger.debug(f"[seats] Found {len(row_markers)} row markers: {[r[0] for r in row_markers]}")
                row_markers.sort(key=lambda item: item[1])
                
                # Scrape seat buttons from the modal
                seen: set[Tuple[str, int, int]] = set()
                seat_elements = page.locator(
                    "button, [role='button'], div[class*='seat'], span"
                )
                
                try:
                    seat_count = await seat_elements.count()
                except Exception:
                    seat_count = 0
                
                logger.debug(f"[seats] Found {seat_count} potential seat elements to iterate through")
                
                for idx in range(min(seat_count, 200)):  # limit iterations
                    try:
                        el = seat_elements.nth(idx)
                        if not await el.is_visible():
                            continue
                        
                        # Get text and aria-label
                        text = ""
                        try:
                            text = (await el.inner_text()).strip()
                        except Exception:
                            pass
                        
                        aria_label = ""
                        try:
                            aria_label = (await el.get_attribute("aria-label")) or ""
                        except Exception:
                            pass
                        
                        # Look for seat pattern like "A1", "B12", etc.
                        seat_match = None
                        if aria_label:
                            seat_match = re.search(r"([A-Z]{1,2}\d{1,2})", aria_label, re.IGNORECASE)
                        if not seat_match and text:
                            seat_match = re.search(r"([A-Z]{1,2}\d{1,2})", text, re.IGNORECASE)
                        
                        if not seat_match:
                            continue
                        
                        seat_id = seat_match.group(1).upper()
                        row = seat_id[0]
                        
                        # Parse column number
                        col_match = re.search(r"(\d+)$", seat_id)
                        if not col_match:
                            continue
                        col = int(col_match.group(1))
                        
                        # Get bounding box to deduplicate
                        box = await el.bounding_box()
                        if not box:
                            continue
                        
                        dedupe_key = (seat_id, int(box["x"]), int(box["y"]))
                        if dedupe_key in seen:
                            continue
                        seen.add(dedupe_key)
                        
                        # Check availability via class or aria attributes
                        class_attr = (await el.get_attribute("class")) or ""
                        status = "available"
                        if any(token in class_attr.lower() for token in ["sold", "blocked", "unavailable", "taken", "booked"]):
                            status = "sold"
                        
                        logger.debug(f"[seats] Found seat {seat_id} with status {status}")
                        candidates.append({
                            "id": seat_id,
                            "row": row,
                            "col": col,
                            "category": None,
                            "price": None,
                            "status": status,
                            "element": el,
                        })
                    except Exception:
                        continue
            else:
                logger.debug(f"[seats] No row markers found in accessibility modal")
        else:
            logger.debug(f"[seats] NOT attempting accessibility scrape (category_selected={category_selected}, seat_candidates=0)")
        
        if candidates:
            logger.info("[seats] Accessibility-grid fallback found %d seat(s).", len(candidates))
        
        # Close the modal after we're done scraping
        await self._close_accessibility_modal(page)
        return candidates

    # ------------------------------------------------------------------
    # _close_accessibility_modal — dismiss the accessibility UI
    # ------------------------------------------------------------------

    async def _close_accessibility_modal(self, page: "playwright.async_api.Page") -> None:
        """Close the accessibility seat selection modal if it's open."""
        try:
            close_btn = page.get_by_role("button", name=re.compile(r"Close accessibility modal", re.IGNORECASE))
            if await close_btn.count() and await close_btn.first.is_visible():
                try:
                    await close_btn.first.click()
                    await page.wait_for_timeout(500)
                    logger.debug("[seats] Closed accessibility modal.")
                except Exception:
                    pass
        except Exception:
            pass

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

        await self._handle_region_popup(page, city=self.city)
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

        await self._handle_region_popup(page, city=self.city)
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
