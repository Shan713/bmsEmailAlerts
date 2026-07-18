#!/usr/bin/env python3
"""
Standalone startup script for the BMS web dashboard.

This script sets up Windows asyncio policy before anything else,
then starts uvicorn.  Use this instead of running uvicorn directly
to avoid the Windows ``NotImplementedError`` subprocess issue.

Usage::

    python run_web.py
    python run_web.py --port 4596
    python run_web.py --port 4596 --reload   # watch for changes
"""

import argparse
import asyncio
import platform
import sys

# ── Windows asyncio fix ──────────────────────────────────────────────
if platform.system() == "Windows":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except AttributeError:
        pass

import uvicorn

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Start BMS Web Dashboard")
    parser.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host (default: 0.0.0.0)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload (may cause issues on Windows with Playwright)")
    args = parser.parse_args()

    print(f"  BMS Dashboard starting on http://{args.host}:{args.port}")
    if args.reload:
        print("  Auto-reload enabled")

    uvicorn.run(
        "web_server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
