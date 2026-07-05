#!/usr/bin/env python3
"""
Standalone credential-setup script for the BMS booking agent.

Imports ONLY ``credential_manager`` (which depends only on ``cryptography``)
— no Selenium / undetected-chromedriver imports, so it works on Python 3.13+.

Usage::

    python setup_creds.py
"""

from __future__ import annotations

import sys

# Only import credential_manager — it only needs cryptography, which
# works on all Python versions.
from credential_manager import SecureCredentialManager


def main() -> None:
    print("=" * 60)
    print("  BMS Auto‑Booking — Credential Setup")
    print("=" * 60)
    print()
    print("This script will securely store your contact details")
    print("(email + phone for ticket delivery), email app password,")
    print("and optionally a BMS Gift Card for automatic payment.")
    print()
    print("⚠️  Gmail App Password (NOT your regular Gmail password — generate one at https://myaccount.google.com/apppasswords)")
    print("    Select 'Mail' → your device → copy the 16-character code.")
    print()
    print("All data is encrypted with Fernet (AES‑128‑CBC) and")
    print("stored locally in 'credentials.enc'.")
    print()

    manager = SecureCredentialManager()

    try:
        success = manager.store_credentials()
        if success:
            print()
            print("=" * 60)
            print("  ✅ Credentials stored successfully!")
            print()
            print("  Next steps:")
            print("    1. Test the agent:    python book_ticket.py --dry-run")
            print("    2. Or use the web UI: uvicorn web_server:app --reload")
            print("=" * 60)
        else:
            print("❌ Credential setup did not complete.", file=sys.stderr)
            sys.exit(1)
    except KeyboardInterrupt:
        print("\n\n⏹️  Setup cancelled by user.")
        sys.exit(1)
    except Exception as exc:
        print(f"\n❌ Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
