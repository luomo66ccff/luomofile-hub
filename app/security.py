import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, status
from fastapi.responses import Response

from app.config import settings


SESSION_COOKIE = "luomofile_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 7

DANGEROUS_MIME_TYPES = {
    "text/html",
    "image/svg+xml",
    "application/javascript",
    "text/javascript",
    "application/x-msdownload",
    "application/x-sh",
}

INLINE_MIME_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 220_000).hex()
    return f"pbkdf2_sha256${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt, digest = stored.split("$", 2)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 220_000).hex()
    return hmac.compare_digest(check, digest)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def sign_session(username: str) -> str:
    secret = settings.session_secret or settings.public_link_secret or "dev-session-secret-change-me"
    exp = int((datetime.now(timezone.utc) + timedelta(seconds=SESSION_MAX_AGE_SECONDS)).timestamp())
    payload = f"{username}|{exp}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def verify_session(value: str | None) -> str | None:
    if not value:
        return None
    try:
        username, exp_raw, sig = value.split("|", 2)
        exp = int(exp_raw)
    except ValueError:
        return None
    if exp < int(datetime.now(timezone.utc).timestamp()):
        return None
    secret = settings.session_secret or settings.public_link_secret or "dev-session-secret-change-me"
    expected = hmac.new(secret.encode(), f"{username}|{exp}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return username


async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def require(condition: bool, code: int, message: str) -> None:
    if not condition:
        raise HTTPException(status_code=code, detail={"error": "forbidden", "message": message})


def content_disposition(mime_type: str | None, filename: str, force_attachment: bool = False) -> str:
    safe = filename.replace("\\", "_").replace("/", "_").replace("\r", "_").replace("\n", "_")
    disposition = "attachment" if force_attachment or mime_type in DANGEROUS_MIME_TYPES else "inline"
    if mime_type not in INLINE_MIME_TYPES and disposition == "inline":
        disposition = "attachment"
    return f'{disposition}; filename="{safe}"'
