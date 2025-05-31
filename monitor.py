import os
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
import smtplib
from email.mime.text import MIMEText

def send_email(subject, body):
    sender = os.environ["EMAIL_SENDER"]
    password = os.environ["EMAIL_PASSWORD"]
    receiver = os.environ["EMAIL_RECEIVER"]

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = receiver

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.sendmail(sender, receiver, msg.as_string())
    print("[INFO] Email sent.")

def monitor():
    url = os.environ["BMS_URL"]
    movie_name = os.environ.get("MOVIE_NAME", "").lower()

    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/114.0.0.0 Safari/537.36")

    driver = webdriver.Chrome(options=options)
    driver.get(url)

    time.sleep(5)  # wait for page to load

    page_source = driver.page_source.lower()
    driver.quit()

    if movie_name in page_source:
        send_email(
            "🎬 Movie Available on BookMyShow!",
            f"The movie '{movie_name}' is listed at {url}.\nCheck and book your tickets!"
        )
    else:
        print(f"[INFO] Movie '{movie_name}' not found on the page yet.")

if __name__ == "__main__":
    print("[INFO] Starting monitor...")
    monitor()
