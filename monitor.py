import requests
import smtplib
import time
from email.message import EmailMessage

# === Configuration ===
URL = "https://in.bookmyshow.com/cinemas/coimbatore/broadway-cinemas-coimbatore/buytickets/BWCB/20250602"  # <- Replace with your BMS or other site URL
CHECK_INTERVAL = 60  # Time in seconds between checks

# === Email Settings ===
EMAIL_ADDRESS = "shanthu2005best@gmail.com"         # <- Your Gmail address
EMAIL_PASSWORD = "jhtn xxay yaee lusd"      # <- Use Gmail App Password (NOT your normal password)
RECEIVER_EMAIL = "shanthu2005@gmail.com" # <- Who should receive the alert

def send_email():
    msg = EmailMessage()
    msg['Subject'] = '🎟️ BMS Page is Live!'
    msg['From'] = EMAIL_ADDRESS
    msg['To'] = RECEIVER_EMAIL
    msg.set_content(f"The page is live! Visit it here:\n\n{URL}")

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            smtp.send_message(msg)
            print("✅ Email sent successfully!")
    except Exception as e:
        print(f"❌ Failed to send email: {e}")

def check_site():
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.7151.55 Safari/537.36"
    }
    try:
        response = requests.get(URL, headers=headers, timeout=10)
        if response.status_code == 200:
            print(f"[{time.ctime()}] ✅ Site is live!")
            send_email()
            return True
        else:
            print(f"[{time.ctime()}] ❌ Status Code: {response.status_code}")
    except Exception as e:
        print(f"[{time.ctime()}] ⚠️ Error checking site: {e}")
    return False


if __name__ == "__main__":
    print("🚀 Starting website monitor...")
    while True:
        if check_site():
            break  # Stop checking after it's live and email is sent
        time.sleep(CHECK_INTERVAL)
