import base64
import json
import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from app.config import settings


def _derive_key(master_key: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100_000,
    )
    return kdf.derive(master_key.encode())


def encrypt_credentials(credentials: dict) -> str:
    """Encrypt a credentials dict to a base64 string (salt:nonce:ciphertext)."""
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive_key(settings.master_key, salt)
    aesgcm = AESGCM(key)
    plaintext = json.dumps(credentials).encode()
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    payload = salt + nonce + ciphertext
    return base64.b64encode(payload).decode()


def decrypt_credentials(encrypted: str) -> dict:
    """Decrypt credentials from base64 string back to dict."""
    payload = base64.b64decode(encrypted.encode())
    salt = payload[:16]
    nonce = payload[16:28]
    ciphertext = payload[28:]
    key = _derive_key(settings.master_key, salt)
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return json.loads(plaintext.decode())


def generate_device_key() -> str:
    """Generate a random 32-byte device key, base64 encoded."""
    return base64.b64encode(os.urandom(32)).decode()


def generate_pairing_code() -> str:
    """Generate an 8-character alphanumeric pairing code."""
    import secrets
    import string
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))
