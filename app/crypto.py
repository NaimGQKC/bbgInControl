"""Encrypt per-agent tokens before they go into the database."""
import base64
import hashlib

from cryptography.fernet import Fernet

from .config import get_settings


def _fernet() -> Fernet:
    key = get_settings().encryption_key
    if not key:
        # Dev fallback: derive a key from the session secret. Set ENCRYPTION_KEY in production.
        key = base64.urlsafe_b64encode(hashlib.sha256(get_settings().session_secret.encode()).digest()).decode()
    return Fernet(key.encode())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode() if value else ""


def decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode() if value else ""
