"""Auth HTTP handlers; behavior preserved from gallery-t001."""
import re
import secrets
from datetime import datetime, timedelta, timezone
from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from app.audit import audit
from app.auth import authenticate, current_user
from app.config import settings
from app.db import connect
from app.emailer import send_verification_email
from app.security import SESSION_COOKIE, SESSION_MAX_AGE_SECONDS, hash_password, hash_token, now_iso, sign_session
from app.presentation import safe_next
from fastapi import APIRouter
from app.web.deps import templates, render_template

router = APIRouter()
import logging
logger = logging.getLogger("luomofile.mail")
VERIFICATION_REQUEST_MESSAGE = "如果填写的信息可用于注册，我们会请求发送验证邮件。请检查收件箱和垃圾邮件；邮件未送达时，可以在这里重新请求验证链接。"


def is_valid_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value.strip()))



def diagnostic_request_id(request: Request) -> str:
    value = request.headers.get("x-request-id", "").strip()
    return value if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value) else secrets.token_hex(8)



def create_email_verification(user_id: int) -> tuple[str, int]:
    token = secrets.token_urlsafe(32)
    now = now_iso()
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=settings.email_verification_ttl_seconds)).isoformat(timespec="seconds")
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO email_verification_tokens(user_id, token_hash, purpose, created_at, expires_at, used_at) VALUES (?, ?, 'register', ?, ?, ?)",
            (user_id, hash_token(token), now, expires_at, now),
        )
        conn.commit()
    return token, int(cur.lastrowid)



def activate_email_verification(user_id: int, token_id: int) -> None:
    now = now_iso()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE email_verification_tokens SET used_at=? WHERE user_id=? AND used_at IS NULL",
            (now, user_id),
        )
        changed = conn.execute(
            "UPDATE email_verification_tokens SET used_at=NULL WHERE id=? AND user_id=? AND used_at IS NOT NULL",
            (token_id, user_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError("Verification token could not be activated.")
        conn.commit()



def claim_verification_email(email: str, source_ip: str | None = None) -> bool:
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    identities = [("email", email.strip().lower(), 5, 60)]
    if source_ip:
        identities.append(("source", source_ip.strip(), 120, 0))
    buckets = [
        (hash_token(f"verify-email:{kind}:{value}"), maximum, cooldown)
        for kind, value, maximum, cooldown in identities
    ]
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = []
        for bucket_hash, maximum, cooldown in buckets:
            row = conn.execute(
                "SELECT window_started_at, last_sent_at, send_count FROM email_verification_send_limits WHERE bucket_hash=?",
                (bucket_hash,),
            ).fetchone()
            if row:
                window_start = datetime.fromisoformat(row["window_started_at"])
                last_sent = datetime.fromisoformat(row["last_sent_at"])
                if window_start.tzinfo is None:
                    window_start = window_start.replace(tzinfo=timezone.utc)
                if last_sent.tzinfo is None:
                    last_sent = last_sent.replace(tzinfo=timezone.utc)
                if now_dt - window_start < timedelta(hours=1) and int(row["send_count"]) >= maximum:
                    conn.rollback()
                    return False
                if cooldown and now_dt - last_sent < timedelta(seconds=cooldown):
                    conn.rollback()
                    return False
                current.append((bucket_hash, window_start, int(row["send_count"])))
            else:
                current.append((bucket_hash, None, 0))
        for bucket_hash, window_start, count in current:
            if window_start is None or now_dt - window_start >= timedelta(hours=1):
                conn.execute(
                    "INSERT INTO email_verification_send_limits(bucket_hash, window_started_at, last_sent_at, send_count) VALUES (?, ?, ?, 1) "
                    "ON CONFLICT(bucket_hash) DO UPDATE SET window_started_at=excluded.window_started_at, last_sent_at=excluded.last_sent_at, send_count=1",
                    (bucket_hash, now, now),
                )
            else:
                conn.execute(
                    "UPDATE email_verification_send_limits SET last_sent_at=?, send_count=? WHERE bucket_hash=?",
                    (now, count + 1, bucket_hash),
                )
        conn.commit()
    return True



@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, user=Depends(current_user)):
    return render_template("login.html", {"request": request, "user": user, "error": "", "next": safe_next(request.query_params.get("next")), "username": ""})



@router.head("/login")
def login_head() -> Response:
    return Response(status_code=200, media_type="text/html")



@router.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("")):
    user = authenticate(username.strip(), password)
    if not user:
        audit("login_failed", user=None, target_type="user", target_id=username.strip(), status="failed")
        return render_template("login.html", {"request": request, "user": None, "error": "用户名或密码不正确，请重新输入。", "next": safe_next(next), "username": username.strip()}, status_code=400)
    with connect() as conn:
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (now_iso(), user["id"]))
        conn.commit()
    if user["status"] != "active":
        response = RedirectResponse("/", status_code=303)
    else:
        response = RedirectResponse(safe_next(next), status_code=303)
    response.set_cookie(SESSION_COOKIE, sign_session(user["username"]), max_age=SESSION_MAX_AGE_SECONDS, httponly=True, secure=settings.app_env == "production", samesite="lax")
    audit("login_success", user=user, target_type="user", target_id=str(user["id"]))
    return response



@router.get("/logout")
@router.post("/logout")
def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response



@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, user=Depends(current_user)):
    return render_template("register.html", {"request": request, "user": user, "error": "", "message": ""})



@router.post("/register", response_class=HTMLResponse)
def register_submit(request: Request, username: str = Form(...), email: str = Form(...), password: str = Form(...), user=Depends(current_user)):
    username = username.strip()
    email = email.strip().lower()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        return render_template("register.html", {"request": request, "user": user, "error": "用户名需为 3 至 32 个字母、数字、点、下划线或短横线。", "message": ""}, status_code=400)
    if not is_valid_email(email):
        return render_template("register.html", {"request": request, "user": user, "error": "请输入有效的邮箱地址。", "message": ""}, status_code=400)
    if len(password) < 8:
        return render_template("register.html", {"request": request, "user": user, "error": "密码至少需要 8 个字符。", "message": ""}, status_code=400)

    now = now_iso()
    with connect() as conn:
        existing = conn.execute("SELECT id, status FROM users WHERE username=? OR lower(email)=?", (username, email)).fetchone()
        if existing:
            return render_template("register.html", {"request": request, "user": user, "error": "", "message": VERIFICATION_REQUEST_MESSAGE})
        cur = conn.execute(
            "INSERT INTO users(username, email, password_hash, role, status, created_at, updated_at) VALUES (?, ?, ?, 'developer', 'pending', ?, ?)",
            (username, email, hash_password(password), now, now),
        )
        conn.commit()
        user_id = cur.lastrowid

    if not claim_verification_email(email, request.client.host if request.client else None):
        return render_template("register.html", {"request": request, "user": user, "error": "", "message": VERIFICATION_REQUEST_MESSAGE})
    token, token_id = create_email_verification(user_id)
    verify_url = f"{settings.app_base_url}/verify-email?token={token}"
    try:
        send_verification_email(email, username, verify_url)
        activate_email_verification(user_id, token_id)
    except Exception as exc:
        logger.warning("verification_email_failed request_id=%s error_type=%s", diagnostic_request_id(request), type(exc).__name__)
    return render_template("register.html", {"request": request, "user": user, "error": "", "message": VERIFICATION_REQUEST_MESSAGE})



@router.get("/verify-email", response_class=HTMLResponse)
def verify_email(request: Request, token: str = ""):
    token = token.strip()
    token_hash = hash_token(token)
    with connect() as conn:
        row = conn.execute(
            """
            SELECT t.*, u.username, u.status AS user_status FROM email_verification_tokens t
            JOIN users u ON u.id=t.user_id
            WHERE t.token_hash=? AND t.used_at IS NULL AND t.purpose='register'
            """,
            (token_hash,),
        ).fetchone()
        if not row or datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
            return render_template("verify_email.html", {"request": request, "user": None, "valid": False, "token": "", "message": "验证链接无效或已过期，请重新请求验证邮件。"}, status_code=400)
        if row["user_status"] not in {"pending", "active"}:
            return render_template("verify_email.html", {"request": request, "user": None, "valid": False, "token": "", "message": "当前账户无法通过此链接启用，请联系管理员。"}, status_code=403)
    return render_template("verify_email.html", {"request": request, "user": None, "valid": True, "token": token, "message": "确认后即可启用相册、上传和分享功能。"})



@router.post("/verify-email", response_class=HTMLResponse)
def verify_email_confirm(request: Request, token: str = Form(...)):
    token_hash = hash_token(token.strip())
    now_dt = datetime.now(timezone.utc)
    now = now_iso()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT t.id, t.user_id, t.expires_at, u.status AS user_status FROM email_verification_tokens t "
            "JOIN users u ON u.id=t.user_id WHERE t.token_hash=? AND t.used_at IS NULL AND t.purpose='register'",
            (token_hash,),
        ).fetchone()
        if not row or datetime.fromisoformat(row["expires_at"]) < now_dt:
            conn.rollback()
            return render_template("verify_email.html", {"request": request, "valid": False, "token": "", "message": "验证链接无效或已过期，请重新请求验证邮件。"}, status_code=400)
        if row["user_status"] not in {"pending", "active"}:
            conn.rollback()
            return render_template("verify_email.html", {"request": request, "user": None, "valid": False, "token": "", "message": "当前账户无法通过此链接启用，请联系管理员。"}, status_code=403)
        changed = conn.execute("UPDATE email_verification_tokens SET used_at=? WHERE id=? AND used_at IS NULL", (now, row["id"])).rowcount
        if changed != 1:
            conn.rollback()
            return render_template("verify_email.html", {"request": request, "valid": False, "token": "", "message": "验证链接无效或已使用，请重新请求验证邮件。"}, status_code=400)
        conn.execute("UPDATE users SET status='active', updated_at=? WHERE id=? AND status='pending'", (now, row["user_id"]))
        conn.commit()
    return RedirectResponse("/login?verified=1", status_code=303)



@router.post("/resend-verification", response_class=HTMLResponse)
def resend_verification(request: Request, email: str = Form(...)):
    normalized_email = email.strip().lower()
    if is_valid_email(normalized_email) and claim_verification_email(normalized_email, request.client.host if request.client else None):
        try:
            with connect() as conn:
                user = conn.execute("SELECT id, username FROM users WHERE lower(email)=? AND status='pending'", (normalized_email,)).fetchone()
            if user:
                user_id = int(user["id"])
                token, token_id = create_email_verification(user_id)
                send_verification_email(normalized_email, user["username"], f"{settings.app_base_url}/verify-email?token={token}")
                activate_email_verification(user_id, token_id)
        except Exception as exc:
            logger.warning("verification_resend_failed request_id=%s error_type=%s", diagnostic_request_id(request), type(exc).__name__)
    return render_template("register.html", {"request": request, "user": None, "error": "", "message": "如果该邮箱对应待验证账户，且邮件服务可用，我们会请求发送新的验证邮件。请检查收件箱和垃圾邮件。", "submitted": True})



