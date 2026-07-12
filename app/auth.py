import secrets
from typing import Iterable

from fastapi import Depends, HTTPException, Request, status

from app.db import connect
from app.security import SESSION_COOKIE, hash_token, verify_password, verify_session


def current_user(request: Request):
    username = verify_session(request.cookies.get(SESSION_COOKIE))
    if not username:
        return None
    with connect() as conn:
        return conn.execute("SELECT * FROM users WHERE username = ? AND status IN ('active', 'pending')", (username,)).fetchone()


def require_user(user=Depends(current_user)):
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
    if user["status"] != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Email verification required")
    return user


def require_admin(user=Depends(require_user)):
    if user["role"] != "admin" or user["status"] != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")
    return user


def authenticate(username: str, password: str):
    with connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = ? AND status IN ('active', 'pending')", (username,)).fetchone()
    if user and verify_password(password, user["password_hash"]):
        return user
    return None


def generate_api_key() -> str:
    return "lfk_" + secrets.token_urlsafe(24)


def require_api_key(scopes: Iterable[str]):
    required = set(scopes)

    def dependency(request: Request):
        token = ""
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
        token = token or request.headers.get("x-api-key", "").strip()
        if not token.startswith("lfk_"):
            raise HTTPException(status_code=401, detail={"error": "unauthorized", "message": "API key required."})
        hashed = hash_token(token)
        with connect() as conn:
            row = conn.execute(
                """
                SELECT k.*, u.status AS user_status FROM api_keys k
                JOIN users u ON u.id = k.user_id
                WHERE k.key_hash = ? AND k.status = 'active'
                """,
                (hashed,),
            ).fetchone()
            if not row or row["user_status"] != "active":
                raise HTTPException(status_code=401, detail={"error": "unauthorized", "message": "Invalid API key."})
            granted = {item.strip() for item in row["scopes"].split(",") if item.strip()}
            if not required.issubset(granted):
                raise HTTPException(status_code=403, detail={"error": "forbidden", "message": "Missing required scope."})
            return row

    return dependency
