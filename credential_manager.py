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
        
        print("Setting up your BookMyShow credentials...")
        print("Note: All data will be encrypted and stored securely.")
        
        # Basic user info
        credentials['email'] = input("BookMyShow Email: ")
        credentials['phone'] = input("Phone Number: ")
        credentials['password'] = getpass.getpass("BookMyShow Password: ")
        
        # Email notification settings
        credentials['notification_email'] = input("Notification Email (can be same as above): ")
        credentials['email_app_password'] = getpass.getpass("Email App Password (for notifications): ")
        
        # Payment information (optional)
        store_payment = input("Store payment info? (y/n): ").lower() == 'y'
        if store_payment:
            print("Payment Information (for faster checkout):")
            credentials['card_number'] = getpass.getpass("Card Number (last 4 digits only): ")
            credentials['cardholder_name'] = input("Cardholder Name: ")
            # Note: Never store full card details or CVV
        
        # Encrypt and save
        encrypted_data = self.fernet.encrypt(json.dumps(credentials).encode())
        with open(self.credentials_file, 'wb') as f:
            f.write(encrypted_data)
        
        print("✅ Credentials stored securely!")
        return True
    
    def get_credentials(self):
        """Retrieve and decrypt stored credentials"""
        if not os.path.exists(self.credentials_file):
            print("No credentials found. Please run setup first.")
            return None
        
        try:
            with open(self.credentials_file, 'rb') as f:
                encrypted_data = f.read()
            
            decrypted_data = self.fernet.decrypt(encrypted_data)
            credentials = json.loads(decrypted_data.decode())
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
        print(f"Email: {creds.get('email', 'N/A')}")
    else:
        print("Failed to load credentials.")