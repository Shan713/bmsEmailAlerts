import os
import requests
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

    try:
        response = requests.get(url)
        if response.status_code == 200:
            content = response.text.lower()
            if movie_name in content:
                send_email(
                    "🎬 Movie Available on BookMyShow!",
                    f"The movie '{movie_name}' is listed at {url}.\nCheck and book your tickets!"
                )
            else:
                print(f"[INFO] Movie '{movie_name}' not found on the page yet.")
        else:
            print(f"[WARN] Page returned status code {response.status_code}.")
    except Exception as e:
        print(f"[ERROR] Exception occurred: {e}")

if __name__ == "__main__":
    print("[INFO] Starting monitor...")
    monitor()
