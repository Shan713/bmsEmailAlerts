import json
import os
from cryptography.fernet import Fernet
import getpass

class SecureCredentialManager:
    def __init__(self, credentials_file='credentials.enc'):
        self.credentials_file = credentials_file
        self.key_file = 'secret.key'
        self.fernet = self._load_or_create_key()
    
    def _load_or_create_key(self):
        """Load existing key or create a new one"""
        if os.path.exists(self.key_file):
            with open(self.key_file, 'rb') as f:
                key = f.read()
        else:
            key = Fernet.generate_key()
            with open(self.key_file, 'wb') as f:
                f.write(key)
            print(f"Created new encryption key: {self.key_file}")
        
        return Fernet(key)
    
    def store_credentials(self):
        """Interactively store user credentials"""
        credentials = {}

        print("Setting up your BookMyShow booking details...")
        print("Note: All data will be encrypted and stored securely.")

        # Contact details (email + phone — no password needed;
        # BMS asks for these at the payment stage, not for login)
        print("\n--- Contact Details (for ticket delivery) ---")
        email = input("BookMyShow Email: ").strip()
        phone = input("Phone Number: ").strip()
        upi_id = input("UPI ID (e.g., username@okhdfcbank) — press Enter to skip: ").strip()
        credentials['user_details'] = {
            'email': email,
            'phone': phone,
        }
        credentials['upi_id'] = upi_id

        # Email notification settings
        print("\n--- Notification Settings ---")
        print("⚠️  Gmail requires an App Password, NOT your regular Gmail password.")
        print("    Generate one at: https://myaccount.google.com/apppasswords")
        print("    Select 'Mail' as the app and your device, then copy the 16-char code.")
        credentials['notification_email'] = input("Notification Email (can be same as above): ").strip()
        credentials['email_app_password'] = getpass.getpass(
            "Gmail App Password (NOT your regular Gmail password — generate one at https://myaccount.google.com/apppasswords): "
        )

        # OTP Relay (for fully autonomous booking)
        print("\n--- OTP Relay Settings (for hands-free OTP filling) ---")
        print("Architecture: Bank OTP SMS → iOS Shortcut → forwards to Gmail")
        print("              → AI agent polls this inbox → extracts OTP → fills it")
        print("⚠️  This inbox is monitored continuously during booking for OTPs.")
        setup_otp = input("Configure OTP email relay? (y/n): ").lower() == 'y'
        if setup_otp:
            # Try to reuse notification email if already set
            notif_email = credentials.get('notification_email', '')
            default_otp_email = notif_email or email
            otp_email = input(f"OTP Relay Gmail address [{default_otp_email}]: ").strip()
            if not otp_email:
                otp_email = default_otp_email

            # Try to reuse email_app_password if already set
            default_app_pw = credentials.get('email_app_password', '')
            if default_app_pw:
                otp_app_pw = getpass.getpass(
                    f"Gmail App Password for {otp_email} [reuse existing]: "
                ).strip()
                if not otp_app_pw:
                    otp_app_pw = default_app_pw
            else:
                print("Generate at: https://myaccount.google.com/apppasswords")
                print("Select 'Mail' as the app, then copy the 16-char code.")
                otp_app_pw = getpass.getpass(
                    f"Gmail App Password for {otp_email}: "
                ).strip()

            if otp_email and otp_app_pw:
                credentials['otp_relay'] = {
                    'email': otp_email,
                    'app_password': otp_app_pw,
                }
                print(f"✅ OTP relay configured: {otp_email}")
            else:
                print("⚠️  Missing email or password — OTP relay NOT stored.")
        else:
            print("ℹ️  OTP relay not configured. "
                  "You'll need to enter OTPs manually during booking.")

        # BMS Gift Card (optional, required for auto-payment)
        store_gift_card = input("\nStore BMS Gift Card for auto-payment? (y/n): ").lower() == 'y'
        if store_gift_card:
            print("BMS Gift Card details (pre‑purchased card — no OTP needed):")
            e_code = input("Gift Card E‑Code: ").strip()
            gc_pin = getpass.getpass("Gift Card PIN: ")
            if e_code and gc_pin:
                credentials['gift_card'] = {
                    'e_code': e_code,
                    'pin': gc_pin,
                }
                print("✅ Gift card details stored.")
            else:
                print("⚠️  E‑Code or PIN was empty — gift card NOT stored.")
        else:
            print("ℹ️  Gift Card not stored — complete_payment() will not work.")

        # Payment information (optional)
        store_payment = input("\nStore payment info? (y/n): ").lower() == 'y'
        if store_payment:
            print("Payment Information (Debit/Credit Card):")
            card_number = getpass.getpass("Card Number (16 digits): ").strip()
            card_expiry = input("Card Expiry (MM/YY): ").strip()
            card_cvv = getpass.getpass("Card CVV (3 digits): ").strip()
            card_name = input("Cardholder Name (exactly as on card): ").strip()
            if card_number:
                credentials['card'] = {
                    'number': card_number,
                    'expiry': card_expiry,
                    'cvv': card_cvv,
                    'name': card_name,
                }
                print("✅ Card details stored.")
            else:
                print("⚠️  Card number was empty — card details NOT stored.")
        else:
            print("ℹ️  Card not stored — payment will require manual entry.")
        
        # Encrypt and save
        encrypted_data = self.fernet.encrypt(json.dumps(credentials).encode())
        with open(self.credentials_file, 'wb') as f:
            f.write(encrypted_data)
        
        print("\n✅ Credentials stored securely!")
        return True
    
    def get_credentials(self):
        """Retrieve and decrypt stored credentials"""
        if not os.path.exists(self.credentials_file):
            print("No credentials found. Run:  python setup_creds.py")
            return None

        try:
            with open(self.credentials_file, 'rb') as f:
                encrypted_data = f.read()

            decrypted_data = self.fernet.decrypt(encrypted_data)
            credentials = json.loads(decrypted_data.decode())

            # Backward compatibility: migrate old flat email/phone to user_details
            if "user_details" not in credentials:
                migrated = {}
                if "email" in credentials:
                    migrated["email"] = credentials.pop("email")
                if "phone" in credentials:
                    migrated["phone"] = credentials.pop("phone")
                if migrated:
                    credentials["user_details"] = migrated
                    # Re-save with the new structure
                    encrypted_data = self.fernet.encrypt(
                        json.dumps(credentials).encode()
                    )
                    with open(self.credentials_file, 'wb') as f:
                        f.write(encrypted_data)
                    print("ℹ️  Migrated credentials to new user_details format.")

            # Warn about legacy wallet_pin
            if "wallet_pin" in credentials and "gift_card" not in credentials:
                print(
                    "⚠️  Legacy 'wallet_pin' found but 'gift_card' is not set. "
                    "Payment has moved to BMS Gift Cards. "
                    "Please re‑run:  python setup_creds.py"
                )

            return credentials
        except Exception as e:
            print(f"Error loading credentials: {e}")
            return None
    
    def update_credential(self, key, value):
        """Update a specific credential"""
        credentials = self.get_credentials()
        if credentials:
            credentials[key] = value
            encrypted_data = self.fernet.encrypt(json.dumps(credentials).encode())
            with open(self.credentials_file, 'wb') as f:
                f.write(encrypted_data)
            return True
        return False

if __name__ == "__main__":
    manager = SecureCredentialManager()
    
    if input("Setup new credentials? (y/n): ").lower() == 'y':
        manager.store_credentials()
    
    # Test retrieval
    creds = manager.get_credentials()
    if creds:
        print("Credentials loaded successfully!")
        user = creds.get('user_details', {})
        print(f"Email: {user.get('email', 'N/A')}")
        print(f"Phone: {user.get('phone', 'N/A')}")
        print(f"UPI ID: {creds.get('upi_id', 'N/A') or '(not set)'}")
    else:
        print("Failed to load credentials.")