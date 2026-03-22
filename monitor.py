import undetected_chromedriver as uc
from PIL import Image
import pytesseract
import smtplib
from email.message import EmailMessage
import time
import io
import random

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
URL = "https://in.bookmyshow.com/movies/coimbatore/coolie/buytickets/ET00395817/20250815"
CHECK_INTERVAL = 60
EMAIL_ADDRESS = "shanthu2005best@gmail.com"
EMAIL_PASSWORD = "fqlb eava leom evka"
RECEIVER_EMAILS = ["shanthu2005@gmail.com","tejeshwarcdr@gmail.com"]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:104.0) Gecko/20100101 Firefox/104.0",
]

def send_email(receiver):
    msg = EmailMessage()
    msg['Subject'] = '🎟️ BMS Booking Link is Live!'
    msg['From'] = EMAIL_ADDRESS
    msg['To'] = receiver
    msg.set_content(f"The booking link is now live:\n\n{URL}")
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            smtp.send_message(msg)
            print("✅ Email sent successfully!")
    except Exception as e:
        print(f"❌ Failed to send email: {e}")

def is_booking_available():
    print(f"[{time.ctime()}] 📸 Taking screenshot & scanning for status text...")

    options = uc.ChromeOptions()
    # options.add_argument("--headless=new")  # Uncomment if you want headless
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"user-agent={random.choice(USER_AGENTS)}")

    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "profile.managed_default_content_settings.stylesheets": 2,
        "profile.managed_default_content_settings.fonts": 2,
    }
    options.add_experimental_option("prefs", prefs)

    try:
        driver = uc.Chrome(options=options)
        driver.set_window_size(1920, 1080)
        driver.get(URL)
        time.sleep(random.uniform(7, 12))

        screenshot = driver.get_screenshot_as_png()
        driver.quit()

        image = Image.open(io.BytesIO(screenshot))
        extracted_text = pytesseract.image_to_string(image).strip().lower()

        print("📝 OCR Extracted Text Snippet:")
        print(extracted_text[:300].replace('\n',' '))

        blocked_keywords = [
            "uh-oh! we couldn't find anything",
            "you are unable to access",
            "captcha",
            "please verify",
            "blocked",
            "access denied",
            "unusual traffic",
            "security service",
            "automated access",
            "are you a robot"
        ]

        # Check if blocked
        if any(keyword in extracted_text for keyword in blocked_keywords):
            print(f"[{time.ctime()}] ❌ Shows not available or blocked detected.")
            return False

        # List of cinemas to check for presence
        cinemas = ["pvr brookfields", "inox"]

        # Check if any cinema is mentioned in OCR text
        if any(cinema in extracted_text for cinema in cinemas):
            print(f"[{time.ctime()}] ✅ Found listed cinemas! Shows available.")
            return True
        else:
            print(f"[{time.ctime()}] ❌ No target cinemas found in OCR text.")
            return False

    except Exception as e:
        print(f"⚠️ Error during OCR-based check: {e}")
        return False


if __name__ == "__main__":
    print("🚀 Monitoring BMS booking link (OCR mode)...")
    backoff = CHECK_INTERVAL
    while True:
        if is_booking_available():
            for receiver in RECEIVER_EMAILS:
                print(f"📧 Sending email notification to {receiver}...")
                send_email(receiver)
            break
        else:
            print(f"[{time.ctime()}] Waiting {backoff} seconds before retry...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 100)
