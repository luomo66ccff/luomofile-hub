import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


_KEY_CONTEXT = b"luomofile-subscription-token-display-v1\0"


def _fernet() -> Fernet:
    secret = settings.subscription_token_encryption_secret.encode("utf-8")
    if len(secret) < 32:
        raise RuntimeError("SUBSCRIPTION_TOKEN_ENCRYPTION_SECRET must contain at least 32 bytes.")
    key = base64.urlsafe_b64encode(hashlib.sha256(_KEY_CONTEXT + secret).digest())
    return Fernet(key)


def encrypt_subscription_token(token: str) -> str:
    return _fernet().encrypt(token.encode("utf-8")).decode("ascii")


def decrypt_subscription_token(ciphertext: str) -> str | None:
    if not ciphertext:
        return None
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError):
        return None
