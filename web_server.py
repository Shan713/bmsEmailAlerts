#!/usr/bin/env python3
"""
FastAPI web dashboard for the BMS autonomous booking agent.

Run with::

    python run_web.py

Or directly with uvicorn (no --reload on Windows)::

    uvicorn web_server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

# Windows asyncio fix: ProactorEventLoop supports subprocess (needed by Playwright)
import sys
if sys.platform == "win32":
    try:
        import asyncio
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from booking_engine import (
    execute_booking,
    load_config,
    save_config,
)
from credential_manager import SecureCredentialManager
from monitor_manager import WatcherManager

# Load environment variables from .env (API keys, etc.)
from dotenv import load_dotenv
load_dotenv()

# Optional: import AI agent for direct booking
try:
    from ai_booking_agent import AIBrowserBookingAgent
    _AI_AVAILABLE = True
except ImportError:
    _AI_AVAILABLE = False

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FILE = "booking_agent.log"

# Ensure console can handle UTF-8 (emojis in log messages)
import sys
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("web_server")

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title="BMS Auto-Booking Dashboard",
    description="Web UI for managing and triggering BookMyShow ticket bookings.",
    version="1.0.0",
)

# ---------------------------------------------------------------------------
# Jinja2 templates
# ---------------------------------------------------------------------------

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ---------------------------------------------------------------------------
# Shared state (module-level — single-process FastAPI)
# ---------------------------------------------------------------------------

booking_lock = asyncio.Lock()
last_booking_result: Optional[Dict[str, Any]] = None
is_booking_in_progress = False
current_booking_id: Optional[str] = None
system_errors: List[Dict[str, Any]] = []  # [{timestamp, message}]


# ---------------------------------------------------------------------------
# Watcher manager for per-request background monitoring
# ---------------------------------------------------------------------------

watcher_manager = WatcherManager(default_interval=60)


def _add_error(message: str) -> None:
    """Record a system error with a timestamp (keep last 50)."""
    system_errors.append({
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "message": message,
    })
    if len(system_errors) > 50:
        system_errors.pop(0)
    logger.error("System error: %s", message)


# ---------------------------------------------------------------------------
# Startup — restore watchers from config
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def startup_restore_watchers():
    """On server start, re-create watchers for any monitoring requests."""
    try:
        config = load_config()
        requests = config.get("booking_requests", [])
        monitoring = [
            r for r in requests
            if r.get("status") in ("monitoring", "active")
            and r.get("auto_book")
            and r.get("movie_url")
        ]
        if monitoring:
            logger.info(
                "🚀 Restoring %d watcher(s) from config…", len(monitoring)
            )
            for req in monitoring:
                watcher_manager.add_watcher(req)
        logger.info(
            "✅ Watchers restored: %d active",
            watcher_manager.watcher_count(),
        )
    except Exception as exc:
        logger.warning("Could not restore watchers on startup: %s", exc)


# ---------------------------------------------------------------------------
# Routes — pages
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Render the main dashboard page."""
    try:
        config = load_config()
    except Exception as exc:
        config = {"booking_requests": [], "user_profile": {}, "notification_settings": {}}
        _add_error(f"Failed to load config: {exc}")

    # Read last N log lines for the initial render
    log_lines = _read_log_tail(50)

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "booking_requests": config.get("booking_requests", []),
            "notification_settings": config.get("notification_settings", {}),
            "user_profile": config.get("user_profile", {}),
            "is_booking": is_booking_in_progress,
            "current_booking_id": current_booking_id,
            "last_result": last_booking_result,
            "log_lines": log_lines,
            "errors": system_errors[-10:],
            "watchers": watcher_manager.get_all_status(),
            "watcher_count": watcher_manager.watcher_count(),
        },
    )


# ---------------------------------------------------------------------------
# Routes — API
# ---------------------------------------------------------------------------


@app.get("/api/requests")
async def api_requests():
    """Return JSON list of all booking requests."""
    try:
        config = load_config()
        return config.get("booking_requests", [])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/status")
async def api_status():
    """Return current agent status."""
    status = {
        "is_booking": is_booking_in_progress,
        "current_booking_id": current_booking_id,
        "last_result": last_booking_result,
        "errors": system_errors[-10:],
        "watchers": {
            "active_count": watcher_manager.watcher_count(),
            "monitors": watcher_manager.get_all_status(),
        },
    }

    # Try to get gift card balance if credentials exist
    try:
        cred_mgr = SecureCredentialManager()
        creds = cred_mgr.get_credentials()
        if creds and creds.get("gift_card"):
            status["gift_card_configured"] = True
            status["gift_card_e_code"] = creds["gift_card"]["e_code"][:4] + "****"
        else:
            status["gift_card_configured"] = False
    except Exception:
        status["gift_card_configured"] = False

    return status


@app.get("/api/logs")
async def api_logs(lines: int = Query(50, ge=1, le=500)):
    """Return the last *lines* lines of ``booking_agent.log``."""
    return {"lines": _read_log_tail(lines)}


@app.post("/api/book/{request_id}")
async def trigger_booking(
    request_id: str,
    dry_run: bool = Query(False),
):
    """
    Trigger a booking for *request_id*.

    If a booking is already in progress, returns 409 Conflict.
    Set ``?dry_run=true`` for a dry‑run.
    """
    global is_booking_in_progress, current_booking_id, last_booking_result

    if booking_lock.locked():
        return JSONResponse(
            status_code=409,
            content={
                "error": "A booking is already in progress.",
                "current_booking_id": current_booking_id,
            },
        )

    async with booking_lock:
        is_booking_in_progress = True
        current_booking_id = request_id
        try:
            logger.info("[web] Triggering booking for %s (dry_run=%s)", request_id, dry_run)
            result = await execute_booking(request_id, dry_run=dry_run)
            last_booking_result = result

            # Update request status in config
            try:
                config = load_config()
                for r in config.get("booking_requests", []):
                    if r.get("id") == request_id:
                        if result.get("success"):
                            r["status"] = "booked"
                        elif not result.get("dry_run"):
                            # Keep as monitoring if dry-run
                            pass
                        break
                save_config(config)
            except Exception as exc:
                logger.warning("Could not update request status: %s", exc)

            return result
        except Exception as exc:
            _add_error(f"Booking {request_id} failed: {exc}")
            raise HTTPException(status_code=500, detail=str(exc))
        finally:
            is_booking_in_progress = False
            current_booking_id = None


@app.post("/api/add-request")
async def add_request(data: Dict[str, Any]):
    """
    Add a new booking request to ``config.json``.

    Expected JSON body fields: ``movie_name`` (required), ``date`` (required),
    ``city``, ``cinemas`` (list), ``preferred_time_range`` (list),
    ``max_price``, ``auto_book``, ``booking_url``.
    """
    if not data.get("movie_name"):
        raise HTTPException(status_code=400, detail="movie_name is required.")
    if not data.get("date"):
        raise HTTPException(status_code=400, detail="date is required.")

    try:
        config = load_config()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    # Auto-generate ID
    existing_ids = {r.get("id", "") for r in config.get("booking_requests", [])}
    counter = 1
    while f"req_{counter:03d}" in existing_ids:
        counter += 1
    new_id = f"req_{counter:03d}"

    new_request = {
        "id": new_id,
        "movie_name": data["movie_name"],
        "date": data["date"],
        "preferred_time_range": data.get("preferred_time_range", ["evening"]),
        "cinemas": data.get("cinemas", []),
        "city": data.get("city", ""),
        "max_price": data.get("max_price", 0),
        "priority": len(config.get("booking_requests", [])) + 1,
        "auto_book": data.get("auto_book", True),
        "status": "monitoring",
        "movie_url": data.get("movie_url") or data.get("booking_url", None),
        "booking_url": data.get("booking_url", None),
        "payment_method": data.get("payment_method", "upi"),
        "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    config.setdefault("booking_requests", []).append(new_request)
    try:
        save_config(config)
        logger.info("[web] Added booking request: %s (%s)", new_id, data["movie_name"])

        # Start a background watcher for this request
        if new_request.get("auto_book") and new_request.get("movie_url"):
            watcher = watcher_manager.add_watcher(new_request)
            if watcher:
                logger.info(
                    "[web] Watcher started for %s (%s)", new_id, data["movie_name"]
                )
        else:
            logger.info(
                "[web] No watcher started for %s (auto_book=%s, movie_url=%s)",
                new_id, new_request.get("auto_book"), bool(new_request.get("movie_url")),
            )

        return {"success": True, "request": new_request}
    except Exception as exc:
        _add_error(f"Failed to save config: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/book-ai/{request_id}")
async def trigger_ai_booking(
    request_id: str,
    dry_run: bool = Query(False),
    use_direct_url: bool = Query(True),
):
    """
    Trigger an **AI-powered** booking for *request_id*.

    Uses ``AIBrowserBookingAgent`` with DeepSeek.  Set ``?dry_run=true``
    for a dry‑run.  Set ``?use_direct_url=false`` to use the full flow
    (homepage → search) instead of the direct URL shortcut.
    """
    global is_booking_in_progress, current_booking_id, last_booking_result

    if not _AI_AVAILABLE:
        return JSONResponse(
            status_code=503,
            content={"error": "AI agent not available. Install browser-use."},
        )

    if booking_lock.locked():
        return JSONResponse(
            status_code=409,
            content={
                "error": "A booking is already in progress.",
                "current_booking_id": current_booking_id,
            },
        )

    async with booking_lock:
        is_booking_in_progress = True
        current_booking_id = request_id
        try:
            config = load_config()
            request_data = next(
                (r for r in config.get("booking_requests", [])
                 if r.get("id") == request_id),
                None,
            )
            if not request_data:
                raise HTTPException(
                    status_code=404,
                    detail=f"Request '{request_id}' not found.",
                )

            agent = AIBrowserBookingAgent(config)

            if use_direct_url:
                movie_url = request_data.get("movie_url") or request_data.get("booking_url")
                if movie_url:
                    # Build the booking URL from movie_url + date
                    import re
                    date_formatted = request_data["date"].replace("-", "")
                    current_url = movie_url
                    match = re.search(r'(ET\d+)', current_url)
                    if match:
                        et_code = match.group(1)
                        # Extract city/slug from the URL
                        parts = current_url.split("/")
                        try:
                            city_idx = parts.index("movies") + 1
                            city = parts[city_idx]
                            slug = parts[city_idx + 1]
                            booking_url = (
                                f"https://in.bookmyshow.com/movies/{city}/{slug}"
                                f"/buytickets/{et_code}/{date_formatted}"
                            )
                        except (ValueError, IndexError):
                            booking_url = current_url
                    else:
                        booking_url = current_url

                    time_ranges = request_data.get("preferred_time_range", [])
                    showtimes = config.get("user_profile", {}).get("preferred_showtimes", {})
                    hours = []
                    for key in time_ranges:
                        for slot in showtimes.get(key, []):
                            try:
                                hours.append(int(slot.strip().split(":")[0]))
                            except (ValueError, IndexError):
                                pass
                    time_window = (min(hours), max(hours) + 1) if hours else None

                    logger.info(
                        "[web-ai] Direct booking for %s: %s",
                        request_id, booking_url,
                    )

                    result = await agent.execute_booking(
                        request_id=request_id,
                        booking_url=booking_url,
                        movie_name=request_data["movie_name"],
                        cinema=request_data.get("cinemas", [""])[0] if request_data.get("cinemas") else "",
                        date=request_data["date"],
                        city=request_data.get("city", "Coimbatore"),
                        time_window=time_window,
                        num_tickets=config.get("user_profile", {}).get("max_tickets", 2),
                        dry_run=dry_run,
                    )
                else:
                    logger.info(
                        "[web-ai] No movie_url — using full flow for %s",
                        request_id,
                    )
                    result = await agent.run(request_data, dry_run=dry_run)
            else:
                result = await agent.run(request_data, dry_run=dry_run)

            last_booking_result = result

            # Update status in config
            try:
                config = load_config()
                for r in config.get("booking_requests", []):
                    if r.get("id") == request_id:
                        if result.get("success"):
                            r["status"] = "booked"
                        break
                save_config(config)
            except Exception as exc:
                logger.warning("Could not update request status: %s", exc)

            return result
        except Exception as exc:
            _add_error(f"AI booking {request_id} failed: {exc}")
            raise HTTPException(status_code=500, detail=str(exc))
        finally:
            is_booking_in_progress = False
            current_booking_id = None


@app.delete("/api/delete-request/{request_id}")
async def delete_request(request_id: str):
    """Remove a booking request from config."""
    try:
        config = load_config()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    original_count = len(config.get("booking_requests", []))
    config["booking_requests"] = [
        r for r in config.get("booking_requests", [])
        if r.get("id") != request_id
    ]

    if len(config["booking_requests"]) == original_count:
        raise HTTPException(status_code=404, detail=f"Request '{request_id}' not found.")

    try:
        save_config(config)
        logger.info("[web] Deleted booking request: %s", request_id)

        # Stop the background watcher if one exists
        asyncio.create_task(watcher_manager.remove_watcher(request_id))

        return {"success": True, "deleted": request_id}
    except Exception as exc:
        _add_error(f"Failed to save config: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# API — Monitor / Watcher management
# ---------------------------------------------------------------------------


@app.get("/api/monitors")
async def api_monitors():
    """Return status of all active background watchers."""
    return {
        "active_count": watcher_manager.watcher_count(),
        "monitors": watcher_manager.get_all_status(),
    }


@app.post("/api/monitors/stop/{request_id}")
async def stop_monitor(request_id: str):
    """Stop the background watcher for a request."""
    removed = await watcher_manager.remove_watcher(request_id)
    if removed:
        logger.info("[web] Stopped watcher for %s", request_id)
        return {"success": True, "stopped": request_id}
    return {"success": False, "message": f"No watcher found for {request_id}"}


@app.post("/api/monitors/start/{request_id}")
async def start_monitor(request_id: str):
    """Start a background watcher for a request."""
    try:
        config = load_config()
        req = next(
            (r for r in config.get("booking_requests", [])
             if r.get("id") == request_id),
            None,
        )
        if not req:
            raise HTTPException(status_code=404, detail=f"Request '{request_id}' not found.")
        if not req.get("movie_url"):
            raise HTTPException(
                status_code=400,
                detail=f"Request '{request_id}' has no movie_url — cannot watch.",
            )

        watcher = watcher_manager.add_watcher(req)
        if watcher:
            return {"success": True, "started": request_id}
        return {"success": False, "message": f"Could not start watcher for {request_id}"}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_log_tail(lines: int) -> List[str]:
    """Return the last *lines* lines of the booking log file."""
    log_path = Path(LOG_FILE)
    if not log_path.exists():
        return ["[no log file yet]"]

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            all_lines = fh.readlines()
        return [line.rstrip("\n") for line in all_lines[-lines:]]
    except Exception as exc:
        return [f"[error reading log: {exc}]"]
