import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlsplit

from fastapi import HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.requests import ClientDisconnect

from app.config import settings


SESSION_COOKIE = "luomofile_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 7
CSRF_FORM_MAX_BYTES = 8192

DANGEROUS_MIME_TYPES = {
    "text/html",
    "image/svg+xml",
    "application/javascript",
    "text/javascript",
    "application/x-msdownload",
    "application/x-sh",
}

INLINE_MIME_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif"}


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
    secret = settings.session_secret
    if not secret or len(secret) < 32:
        raise RuntimeError("SESSION_SECRET must contain at least 32 characters.")
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
    if len(sig) != 64 or any(char not in "0123456789abcdef" for char in sig):
        return None
    if exp < int(datetime.now(timezone.utc).timestamp()):
        return None
    secret = settings.session_secret
    if not secret or len(secret) < 32:
        return None
    expected = hmac.new(secret.encode(), f"{username}|{exp}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return username


def form_csrf_token(request: Request) -> str:
    """Bind native-form proof to this exact signed, unexpired session cookie."""
    session = request.cookies.get(SESSION_COOKIE)
    if not verify_session(session):
        return ""
    payload = b"luomofile:native-form-csrf:v1\x00" + session.encode("utf-8")
    return hmac.new(settings.session_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


class _FormBodyTooLarge(Exception):
    pass


async def _csrf_form_fallback(request: Request) -> bool:
    """Read only a small native URL-encoded form, retaining body() for downstream."""
    if request.method.upper() != "POST":
        return False
    content_types = request.headers.getlist("content-type")
    if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
        return False
    expected = form_csrf_token(request)
    if not expected:
        return False
    lengths = request.headers.getlist("content-length")
    if len(lengths) > 1:
        return False
    declared = None
    if lengths:
        raw_length = lengths[0]
        if len(raw_length) > 10 or not raw_length.isascii() or not raw_length.isdigit():
            return False
        declared = int(raw_length)
        if declared > CSRF_FORM_MAX_BYTES:
            return False

    # Content-Length is only an early rejection hint. Count actual ASGI chunks
    # before Request.body() retains them, including chunked or dishonest clients.
    receive = request._receive
    received = 0

    async def bounded_receive():
        nonlocal received
        message = await receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > CSRF_FORM_MAX_BYTES:
                raise _FormBodyTooLarge
        return message

    request._receive = bounded_receive
    try:
        body = await request.body()
    except (_FormBodyTooLarge, ClientDisconnect):
        return False
    finally:
        request._receive = receive
    if len(body) > CSRF_FORM_MAX_BYTES or (declared is not None and declared != len(body)):
        return False
    try:
        fields = parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=128,
                           encoding="utf-8", errors="strict")
    except (ValueError, UnicodeError):
        return False
    tokens = [value for name, value in fields if name == "csrf_token"]
    return (len(tokens) == 1 and len(tokens[0]) == 64
            and all(char in "0123456789abcdef" for char in tokens[0])
            and hmac.compare_digest(tokens[0], expected))


def _origin_parts(value: str | None, *, origin_header: bool = False) -> tuple | None:
    if not value or value != value.strip() or any(ord(char) < 33 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return None
        if origin_header and (parsed.path or parsed.query or parsed.fragment):
            return None
        port = parsed.port
        if port == 0 or (parsed.netloc.endswith(":")):
            return None
        return parsed.scheme, parsed.hostname, port or (443 if parsed.scheme == "https" else 80)
    except (ValueError, UnicodeError):
        return None


def _source_rejected(request: Request) -> Response:
    subscription = request.url.path == "/admin/subscriptions" or request.url.path.startswith("/admin/subscriptions/")
    message = ("无法确认请求来源，本次操作未执行。请从本站重新打开订阅管理页后再试。" if subscription
               else "无法确认请求来源，本次操作未执行。请从本站重新打开操作页面后再试。")
    headers = {"Cache-Control": "private, no-store, max-age=0", "Pragma": "no-cache"}
    if ("application/json" in request.headers.get("accept", "")
            or request.headers.get("x-requested-with") == "XMLHttpRequest"
            or request.url.path.startswith("/api/")):
        return JSONResponse({"error": "cross_origin_rejected", "message": message}, status_code=403, headers=headers)
    if subscription:
        return HTMLResponse(
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>请求来源待确认 · LuomoFile Hub</title>'
            f'<link rel="stylesheet" href="/static/css/harbor.css?v={settings.app_version}">'
            f'<link rel="stylesheet" href="/static/css/pages.css?v={settings.app_version}">'
            '</head><body><main class="wrap"><section class="panel error-page"><p class="berth">请求未执行</p>'
            '<h1>请重新打开订阅管理</h1>'
            f'<p role="alert">{message}</p>'
            '<a class="btn btn-primary" href="/admin/subscriptions">返回订阅管理</a>'
            '</section></main></body></html>', status_code=403, headers=headers,
        )
    from app.presentation import error_response, is_html
    if is_html(request):
        path = request.url.path
        destination, label = "/", "返回首页重新打开"
        if path == "/verify-email":
            destination, label = "/register", "重新获取验证邮件"
            message = "无法确认请求来源，本次验证未执行。请从邮箱重新打开验证链接，或重新获取验证邮件。"
        elif path.startswith("/admin/api-keys"):
            destination, label = "/admin/api-keys", "返回 API 密钥管理"
        elif path.startswith("/admin/"):
            destination, label = "/admin", "返回管理首页"
        elif path.startswith("/albums"):
            destination, label = "/albums", "返回我的相册"
        elif path.startswith("/images"):
            destination, label = "/images", "返回我的图片"
        elif path.startswith("/files"):
            destination, label = "/files", "返回我的文件"
        return error_response(request, 403, message=message, return_to=destination, return_label=label)
    return Response("Cross-origin request rejected.", status_code=403, headers=headers)


def _apply_security_headers(request: Request, response: Response) -> Response:
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Native form navigation needs a source; never send it to another origin.
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.url.path.startswith(("/albums", "/images", "/media", "/admin", "/developer", "/login", "/register")) or SESSION_COOKIE in request.cookies:
        response.headers["Cache-Control"] = "private, no-store, max-age=0"
    if request.url.path.startswith("/sub/") or request.url.path == "/verify-email" or {"token", "created_key"}.intersection(request.query_params):
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "private, no-store, max-age=0"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


async def security_headers(request: Request, call_next):
    if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and SESSION_COOKIE in request.cookies:
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        expected = _origin_parts(settings.app_base_url)
        # A present but invalid Origin must not fall back to a valid Referer.
        supplied = _origin_parts(origin, origin_header=True) if origin is not None else _origin_parts(referer)
        origin_count = len(request.headers.getlist("origin"))
        same_origin = bool(expected and supplied and supplied == expected and origin_count <= 1)
        fetch_sites = request.headers.getlist("sec-fetch-site")
        fetch_site = fetch_sites[0].strip().lower() if len(fetch_sites) == 1 else ""
        bad_fetch_site = len(fetch_sites) > 1 or fetch_site == "cross-site"
        if not same_origin and not bad_fetch_site:
            # no-referrer documents can produce Origin:null on a real same-origin
            # native POST. Never extend this exception to a bad/foreign Origin,
            # conflicting Referer, multipart upload, or unproven session.
            can_fallback = (origin in {None, "null"} and origin_count <= 1 and expected is not None
                            and fetch_site == "same-origin"
                            and (referer is None or _origin_parts(referer) == expected))
            same_origin = bool(can_fallback and await _csrf_form_fallback(request))
        if not same_origin or bad_fetch_site:
            return _apply_security_headers(request, _source_rejected(request))
    response = await call_next(request)
    return _apply_security_headers(request, response)


def require(condition: bool, code: int, message: str) -> None:
    if not condition:
        raise HTTPException(status_code=code, detail={"error": "forbidden", "message": message})


def content_disposition(mime_type: str | None, filename: str, force_attachment: bool = False) -> str:
    safe = filename.replace("\\", "_").replace("/", "_").replace("\r", "_").replace("\n", "_")
    disposition = "attachment" if force_attachment or mime_type in DANGEROUS_MIME_TYPES else "inline"
    if mime_type not in INLINE_MIME_TYPES and disposition == "inline":
        disposition = "attachment"
    return f'{disposition}; filename="{safe}"'
