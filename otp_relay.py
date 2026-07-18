#!/usr/bin/env python3
"""
Email-based OTP relay for the BMS autonomous booking agent.

Architecture
------------
::

    Bank sends OTP → iOS Shortcut reads SMS → sends email to agent's Gmail
                                                      │
    AI Agent polls Gmail inbox → finds OTP email → extracts OTP → fills it
                                                      → completes booking

This module provides the email polling side — connecting to Gmail via IMAP,
watching for incoming OTP emails forwarded by the iOS Shortcut, extracting
the numeric OTP code, and (optionally) sending a test email to verify the
pipeline is working.

Usage
-----

    from otp_relay import EmailOTPRelay

    relay = EmailOTPRelay(
        email="shanthu2005best@gmail.com",
        app_password="your-16-char-gmail-app-password",
    )

    # Send a test email to verify the pipeline
    relay.send_test_email()

    # Poll for an OTP (blocks for up to 120 seconds by default)
    otp = relay.poll_for_otp(timeout=120, interval=5)
    if otp:
        print(f"Got OTP: {otp}")
    else:
        print("No OTP received within timeout.")
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
import smtplib
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OTP extraction patterns
# ---------------------------------------------------------------------------

# Common OTP patterns found in bank verification emails/SMS forwards
OTP_PATTERNS = [
    # "OTP: 123456" or "OTP is 123456"
    re.compile(r"OTP\s*(?:is|code|number|:)?\s*[:]?\s*(\d{4,8})", re.IGNORECASE),
    # "One Time Password: 123456"
    re.compile(r"one[-\s]time\s*password\s*[:]?\s*(\d{4,8})", re.IGNORECASE),
    # "verification code: 123456"
    re.compile(r"verification\s*code\s*[:]?\s*(\d{4,8})", re.IGNORECASE),
    # "Your OTP for transaction" type messages — look for 4-8 digit standalone code
    re.compile(r"(?:code|pin)\s*[:]?\s*(\d{4,8})", re.IGNORECASE),
    # Standalone 6-digit number (common bank OTP format) — high-confidence context
    re.compile(r"\b(\d{6})\b"),
    # "Enter the code (123456) to"
    re.compile(r"(?:enter|use)\s*(?:the\s*)?(?:code|otp)\s*[:(]?\s*(\d{4,8})", re.IGNORECASE),
    # 4-digit PINs
    re.compile(r"\bPIN\s*[:]?\s*(\d{4})\b", re.IGNORECASE),
]


class EmailOTPRelay:
    """
    Polls a Gmail inbox for OTP emails forwarded by an iOS Shortcut.

    The iOS Shortcut should:
    1. Trigger when an SMS arrives from a bank shortcode
    2. Extract the SMS body
    3. Send an email to the configured Gmail address with subject "BMS OTP"
       or forward the SMS content as the email body

    Parameters
    ----------
    email_addr : str
        Gmail address to poll (e.g. ``shanthu2005best@gmail.com``).
    app_password : str
        Gmail App Password (16 chars, no spaces).  Generate at
        https://myaccount.google.com/apppasswords — select "Mail" as the app.
    imap_server : str
        IMAP server (default ``imap.gmail.com``).
    smtp_server : str
        SMTP server for sending test emails (default ``smtp.gmail.com``).
    smtp_port : int
        SMTP port (default 587 for TLS).
    """

    def __init__(
        self,
        email_addr: str,
        app_password: str,
        imap_server: str = "imap.gmail.com",
        smtp_server: str = "smtp.gmail.com",
        smtp_port: int = 587,
    ) -> None:
        self.email_addr = email_addr
        self.app_password = app_password
        self.imap_server = imap_server
        self.smtp_server = smtp_server
        self.smtp_port = smtp_port

        # Track seen email IDs to avoid re-processing old OTPs
        self._seen_uids: set[str] = set()

    # ------------------------------------------------------------------
    # OTP extraction
    # ------------------------------------------------------------------

    @staticmethod
    def extract_otp(text: str) -> Optional[str]:
        """
        Extract an OTP code from *text* using common bank OTP patterns.

        Returns the first match found, or ``None``.
        """
        if not text:
            return None

        # Try specific patterns first (high-confidence)
        for pattern in OTP_PATTERNS:
            match = pattern.search(text)
            if match:
                code = match.group(1)
                logger.debug("OTP matched by pattern %s → %s", pattern.pattern, code)
                return code

        return None

    # ------------------------------------------------------------------
    # IMAP polling
    # ------------------------------------------------------------------

    def _connect_imap(self) -> imaplib.IMAP4_SSL:
        """Connect and login to Gmail IMAP."""
        mail = imaplib.IMAP4_SSL(self.imap_server)
        mail.login(self.email_addr, self.app_password)
        return mail

    def _fetch_unseen_otp(self, mail: imaplib.IMAP4_SSL) -> Optional[str]:
        """
        Search INBOX for unseen emails, extract OTP from the first match.

        Returns the OTP string or ``None``.
        """
        try:
            mail.select("INBOX")

            # Search for ALL emails (not just unseen) within last few minutes
            # — the iOS Shortcut might mark them differently
            status, messages = mail.search(None, "ALL")
            if status != "OK" or not messages[0]:
                return None

            uid_list = messages[0].split()
            # Process in reverse (newest first)
            for uid in reversed(uid_list):
                uid_str = uid.decode()
                if uid_str in self._seen_uids:
                    continue

                self._seen_uids.add(uid_str)
                status, msg_data = mail.fetch(uid, "(RFC822)")
                if status != "OK":
                    continue

                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                # Extract body text
                body_text = self._get_email_body(msg)
                subject = msg.get("Subject", "")

                # Check subject for OTP-related keywords
                combined = f"{subject}\n{body_text}"

                otp = self.extract_otp(combined)
                if otp:
                    logger.info(
                        "📩 OTP email found! Subject: %s, OTP: %s",
                        subject, otp,
                    )
                    return otp
                else:
                    logger.debug(
                        "Email seen (subject='%s') — no OTP pattern matched.",
                        subject,
                    )

            return None

        except Exception as exc:
            logger.warning("Error fetching emails: %s", exc)
            return None

    @staticmethod
    def _get_email_body(msg) -> str:
        """Extract the plain-text or HTML body from an email Message."""
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == "text/plain":
                    try:
                        body += part.get_payload(decode=True).decode(
                            "utf-8", errors="replace"
                        )
                    except Exception:
                        pass
                elif content_type == "text/html" and not body.strip():
                    try:
                        html = part.get_payload(decode=True).decode(
                            "utf-8", errors="replace"
                        )
                        # Simple HTML → text: strip tags
                        body += re.sub(r"<[^>]+>", " ", html)
                    except Exception:
                        pass
        else:
            try:
                body = msg.get_payload(decode=True).decode(
                    "utf-8", errors="replace"
                )
            except Exception:
                body = str(msg.get_payload())

        return body

    def poll_for_otp(
        self,
        timeout: int = 120,
        interval: int = 5,
    ) -> Optional[str]:
        """
        Poll Gmail inbox for an OTP email, blocking for up to *timeout* seconds.

        Parameters
        ----------
        timeout : int
            Maximum seconds to wait (default 120).
        interval : int
            Seconds between each poll (default 5).

        Returns
        -------
        str or None
            The extracted OTP code, or ``None`` if timed out.
        """
        logger.info(
            "📬 Polling %s for OTP email (timeout=%ds, interval=%ds)…",
            self.email_addr, timeout, interval,
        )

        deadline = time.monotonic() + timeout
        mail = None

        try:
            mail = self._connect_imap()
            logger.info("✅ IMAP connected to %s", self.imap_server)

            while time.monotonic() < deadline:
                otp = self._fetch_unseen_otp(mail)
                if otp:
                    return otp

                remaining = max(0, deadline - time.monotonic())
                logger.debug(
                    "⏳ No OTP yet.  %.0fs remaining.  Retrying in %ds…",
                    remaining, min(interval, remaining),
                )
                time.sleep(min(interval, remaining))

            logger.warning("⏰ OTP polling timed out after %ds.", timeout)
            return None

        except imaplib.IMAP4.error as exc:
            logger.error("IMAP error: %s", exc)
            return None
        except Exception as exc:
            logger.error("Unexpected error polling for OTP: %s", exc)
            return None
        finally:
            if mail:
                try:
                    mail.logout()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Test — send a test email via SMTP
    # ------------------------------------------------------------------

    def send_test_email(self, to_addr: Optional[str] = None) -> bool:
        """
        Send a test OTP email to verify the SMTP pipeline works.

        Sends from *self.email_addr* to *to_addr* (defaults to self).

        Returns ``True`` if the email was sent successfully.
        """
        recipient = to_addr or self.email_addr
        test_otp = str(datetime.now().microsecond)[-6:]  # 6-digit "OTP"

        subject = "[BMS Agent Test] OTP Relay Verification"
        body = (
            f"This is a test email from the BMS autonomous booking agent.\n\n"
            f"Test OTP: {test_otp}\n\n"
            f"Sent at: {datetime.now().isoformat()}\n"
            f"Architecture: Bank OTP → iOS Shortcut → Gmail → AI Agent\n\n"
            f"If you received this email via the iOS Shortcut forward, "
            f"the pipeline is working correctly."
        )

        msg = MIMEMultipart()
        msg["From"] = self.email_addr
        msg["To"] = recipient
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        try:
            logger.info("📧 Sending test email to %s…", recipient)
            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()
                server.login(self.email_addr, self.app_password)
                server.send_message(msg)
            logger.info("✅ Test email sent! Check %s", recipient)
            return True
        except smtplib.SMTPAuthenticationError as exc:
            logger.error(
                "❌ SMTP authentication failed: %s\n"
                "   Make sure you're using a Gmail App Password, "
                "not your regular Gmail password.\n"
                "   Generate one at: https://myaccount.google.com/apppasswords",
                exc,
            )
            return False
        except Exception as exc:
            logger.error("❌ Failed to send test email: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _self_test() -> None:
    """Quick smoke-test of OTP extraction (no network calls)."""
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    print("=" * 60)
    print("EmailOTPRelay — OTP Extraction Test")
    print("=" * 60)

    test_cases = [
        ("Your OTP is 123456 for transaction", "123456"),
        ("OTP: 78901234", "78901234"),
        ("One Time Password: 567890", "567890"),
        ("Verification code: 4321", "4321"),
        ("Enter the code 999888 to complete payment", "999888"),
        ("Use OTP 246810 for verification", "246810"),
        ("PIN: 1234", "1234"),
        ("No OTP here", None),
        ("", None),
        # iOS Shortcut forwarded SMS
        (
            "Forwarded SMS from HDFC Bank:\n"
            "Your OTP for transaction of INR 450.00 at BookMyShow is 735291. "
            "Do not share with anyone.",
            "735291",
        ),
        # Multi-OTP — should match first pattern
        (
            "OTP is 111111. Your code is 222222.",
            "111111",
        ),
    ]

    passed = 0
    failed = 0
    for text, expected in test_cases:
        result = EmailOTPRelay.extract_otp(text)
        status = "✅" if result == expected else "❌"
        if result == expected:
            passed += 1
        else:
            failed += 1
        print(f"{status}  Input: {text[:70]:<70} → Expected: {str(expected):<10} Got: {result}")

    print(f"\n{passed}/{passed + failed} tests passed.")
    print("=" * 60)


if __name__ == "__main__":
    _self_test()
