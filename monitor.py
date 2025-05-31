import smtplib, ssl, os, time
from email.message import EmailMessage
import requests
from bs4 import BeautifulSoup

BMS_URL = os.getenv("BMS_URL")
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")
CHECK_INTERVAL = 300  # 5 minutes

def is_booking_live(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=10)
        return "Book Tickets" in response.text or "book tickets" in response.text
    except Exception as e:
        print(f"[ERROR] {e}")
        return False

def send_email():
    msg = EmailMessage()
    msg["Subject"] = "🎬 BookMyShow Alert: Tickets LIVE!"
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECEIVER
    msg.set_content(f"Hey! Booking is LIVE: {BMS_URL}")

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(EMAIL_SENDER, EMAIL_PASSWORD)
            smtp.send_message(msg)
        print("[INFO] Email sent!")
    except Exception as e:
        print(f"[ERROR] Email failed: {e}")

def main():
    print("[INFO] Starting monitor...")
    if is_booking_live(BMS_URL):
        print("[ALERT] Booking is LIVE!")
        send_email()
    else:
        print("[INFO] Still waiting...")

if __name__ == "__main__":
    main()