# utils/crypto.py
from cryptography.fernet import Fernet
import os
from dotenv import load_dotenv
load_dotenv()

key = os.getenv("ENCRYPTION_KEY")
if not key:
    raise ValueError("ENCRYPTION_KEY must be set in the environment variables.")

cipher_suite = Fernet(key)

def encrypt_data(data: str) -> bytes:
    return cipher_suite.encrypt(data.encode())

def decrypt_data(encrypted_data: bytes) -> str:
    return cipher_suite.decrypt(encrypted_data).decode()

