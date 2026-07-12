import asyncio
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.audit import audit
from app.auth import authenticate, current_user, generate_api_key, require_admin, require_api_key, require_user
from app.cleanup import cleanup_once
from app.config import ensure_dirs, settings
from app.db import connect, init_db
from app.emailer import send_verification_email
from app.files import can_manage_file, delete_file, generate_direct_link, revoke_direct_link, save_upload, serve_file, update_file_tags
from app.security import SESSION_COOKIE, SESSION_MAX_AGE_SECONDS, hash_password, hash_token, now_iso, security_headers, sign_session
from app.error_redirect import error_redirect_middleware


app = FastAPI(title=settings.app_name, docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory="app/web/templates")
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
app.middleware("http")(security_headers)
app.middleware("http")(error_redirect_middleware)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict) and (request.url.path.startswith("/api/") or wants_json(request)):
        return JSONResponse(exc.detail, status_code=exc.status_code)
    return JSONResponse({"error": "request_failed", "message": str(exc.detail)}, status_code=exc.status_code)


def wants_json(request: Request) -> bool:
    return (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in (request.headers.get("accept") or "")
    )


def ensure_upload_user(user) -> None:
    if not user:
        raise HTTPException(status_code=401, detail={"error": "unauthorized", "message": "Please log in to upload files."})
    if user["status"] != "active":
        raise HTTPException(status_code=403, detail={"error": "email_verification_required", "message": "Please verify your email address before uploading files."})


def require_internal_token(request: Request) -> None:
    auth = request.headers.get("authorization", "")
    token = auth.split(" ", 1)[1].strip() if auth.lower().startswith("bearer ") else ""
    if not settings.internal_token or not token or not hmac.compare_digest(token, settings.internal_token):
        raise HTTPException(status_code=401, detail={"error": "unauthorized", "message": "Internal token required."})


def storage_level(used: int, max_bytes: int | None) -> str:
    if not max_bytes:
        return "unknown"
    pct = used * 100 / max_bytes
    if pct >= 95:
        return "critical"
    if pct >= 90:
        return "degraded"
    if pct >= 80:
        return "warning"
    return "operational"


def is_valid_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value.strip()))


def create_email_verification(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = now_iso()
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=settings.email_verification_ttl_seconds)).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute("UPDATE email_verification_tokens SET used_at=? WHERE user_id=? AND used_at IS NULL", (now, user_id))
        conn.execute(
            "INSERT INTO email_verification_tokens(user_id, token_hash, purpose, created_at, expires_at) VALUES (?, ?, 'register', ?, ?)",
            (user_id, hash_token(token), now, expires_at),
        )
        conn.commit()
    return token


@app.on_event("startup")
async def startup() -> None:
    ensure_dirs()
    init_db()
    asyncio.create_task(cleanup_loop())


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(settings.cleanup_interval_seconds)
        try:
            await asyncio.to_thread(cleanup_once)
        except Exception:
            pass


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.head("/health")
def health_head() -> Response:
    return Response(status_code=200, media_type="application/json")


@app.get("/api/public/status")
def public_status() -> dict[str, str]:
    return {"status": "operational", "label": "Operational", "service": "LuomoFile Hub", "updated_at": now_iso(), "version": settings.app_version}


@app.get("/api/public/version")
def public_version() -> dict[str, str]:
    return {"service": "LuomoFile Hub", "version": settings.app_version}


@app.get("/api/public/storage")
def public_storage() -> dict:
    with connect() as conn:
        rows = conn.execute("SELECT name, enabled, max_capacity_bytes, used_bytes_cache FROM storage_backends ORDER BY priority").fetchall()
    available = any(row["enabled"] for row in rows)
    levels = [storage_level(int(row["used_bytes_cache"] or 0), row["max_capacity_bytes"]) for row in rows if row["enabled"]]
    status = "operational" if available and "critical" not in levels and "degraded" not in levels else "degraded"
    return {"status": status, "storage": "available" if available else "unavailable", "backends": [{"name": row["name"], "enabled": bool(row["enabled"]), "level": storage_level(int(row["used_bytes_cache"] or 0), row["max_capacity_bytes"])} for row in rows]}


@app.get("/", response_class=HTMLResponse)
def home(request: Request, user=Depends(current_user)):
    show_private_dashboard = bool(user and user["status"] == "active")
    with connect() as conn:
        stats = {}
        if show_private_dashboard:
            stats = {
                "files": conn.execute("SELECT COUNT(*) AS c FROM files WHERE status='active' AND owner_id=?", (user["id"],)).fetchone()["c"],
                "bytes": conn.execute("SELECT COALESCE(SUM(size_bytes),0) AS c FROM files WHERE status='active' AND owner_id=?", (user["id"],)).fetchone()["c"],
                "images": conn.execute("SELECT COUNT(*) AS c FROM files WHERE status='active' AND purpose='image' AND owner_id=?", (user["id"],)).fetchone()["c"],
                "temp": conn.execute("SELECT COUNT(*) AS c FROM files WHERE status='active' AND purpose='temp' AND owner_id=?", (user["id"],)).fetchone()["c"],
            }
            if user["role"] == "admin":
                stats.update({
                    "site_files": conn.execute("SELECT COUNT(*) AS c FROM files WHERE status='active'").fetchone()["c"],
                    "site_bytes": conn.execute("SELECT COALESCE(SUM(size_bytes),0) AS c FROM files WHERE status='active'").fetchone()["c"],
                })
        backends = conn.execute("SELECT * FROM storage_backends ORDER BY priority").fetchall() if show_private_dashboard else []
        recent = conn.execute("SELECT * FROM files WHERE owner_id=? ORDER BY uploaded_at DESC LIMIT 8", (user["id"],)).fetchall() if show_private_dashboard else []
        links = conn.execute("SELECT * FROM files WHERE owner_id=? AND link_enabled=1 ORDER BY link_created_at DESC LIMIT 6", (user["id"],)).fetchall() if show_private_dashboard else []
    return templates.TemplateResponse("home.html", {"request": request, "user": user, "stats": stats, "backends": backends, "recent": recent, "recent_links": links, "show_private_dashboard": show_private_dashboard})


@app.head("/")
def home_head() -> Response:
    return Response(status_code=200, media_type="text/html")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": ""})


@app.head("/login")
def login_head() -> Response:
    return Response(status_code=200, media_type="text/html")


@app.post("/login")
def login_submit(username: str = Form(...), password: str = Form(...)):
    user = authenticate(username.strip(), password)
    if not user:
        audit("login_failed", user=None, target_type="user", target_id=username.strip(), status="failed")
        return RedirectResponse("/login?error=1", status_code=303)
    with connect() as conn:
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (now_iso(), user["id"]))
        conn.commit()
    if user["status"] != "active":
        response = RedirectResponse("/", status_code=303)
    else:
        response = RedirectResponse("/admin" if user["role"] == "admin" else "/developer", status_code=303)
    response.set_cookie(SESSION_COOKIE, sign_session(user["username"]), max_age=SESSION_MAX_AGE_SECONDS, httponly=True, secure=True, samesite="lax")
    audit("login_success", user=user, target_type="user", target_id=str(user["id"]))
    return response


@app.get("/logout")
@app.post("/logout")
def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, user=Depends(current_user)):
    return templates.TemplateResponse("register.html", {"request": request, "user": user, "error": "", "message": ""})


@app.post("/register", response_class=HTMLResponse)
def register_submit(request: Request, username: str = Form(...), email: str = Form(...), password: str = Form(...), user=Depends(current_user)):
    username = username.strip()
    email = email.strip().lower()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        return templates.TemplateResponse("register.html", {"request": request, "user": user, "error": "Username must be 3-32 characters and use letters, numbers, dots, underscores, or hyphens.", "message": ""}, status_code=400)
    if not is_valid_email(email):
        return templates.TemplateResponse("register.html", {"request": request, "user": user, "error": "Please enter a valid email address.", "message": ""}, status_code=400)
    if len(password) < 8:
        return templates.TemplateResponse("register.html", {"request": request, "user": user, "error": "Password must be at least 8 characters.", "message": ""}, status_code=400)

    now = now_iso()
    with connect() as conn:
        existing = conn.execute("SELECT id, status FROM users WHERE username=? OR lower(email)=?", (username, email)).fetchone()
        if existing:
            return templates.TemplateResponse("register.html", {"request": request, "user": user, "error": "This username or email is already registered.", "message": ""}, status_code=400)
        cur = conn.execute(
            "INSERT INTO users(username, email, password_hash, role, status, created_at, updated_at) VALUES (?, ?, ?, 'developer', 'pending', ?, ?)",
            (username, email, hash_password(password), now, now),
        )
        conn.commit()
        user_id = cur.lastrowid

    token = create_email_verification(user_id)
    verify_url = f"{settings.app_base_url}/verify-email?token={token}"
    try:
        send_verification_email(email, username, verify_url)
    except Exception:
        return templates.TemplateResponse("register.html", {"request": request, "user": user, "error": "Account was created, but verification email could not be sent. Please contact the administrator.", "message": ""}, status_code=500)
    return templates.TemplateResponse("register.html", {"request": request, "user": user, "error": "", "message": "Verification email sent. Please open the link in your mailbox to activate your account."})


@app.get("/verify-email", response_class=HTMLResponse)
def verify_email(request: Request, token: str):
    token_hash = hash_token(token.strip())
    now_dt = datetime.now(timezone.utc)
    now = now_iso()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT t.*, u.username FROM email_verification_tokens t
            JOIN users u ON u.id=t.user_id
            WHERE t.token_hash=? AND t.used_at IS NULL
            """,
            (token_hash,),
        ).fetchone()
        if not row or datetime.fromisoformat(row["expires_at"]) < now_dt:
            return templates.TemplateResponse("register.html", {"request": request, "user": None, "error": "Verification link is invalid or expired.", "message": ""}, status_code=400)
        conn.execute("UPDATE email_verification_tokens SET used_at=? WHERE id=?", (now, row["id"]))
        conn.execute("UPDATE users SET status='active', updated_at=? WHERE id=?", (now, row["user_id"]))
        conn.commit()
    response = RedirectResponse("/developer", status_code=303)
    response.set_cookie(SESSION_COOKIE, sign_session(row["username"]), max_age=SESSION_MAX_AGE_SECONDS, httponly=True, secure=True, samesite="lax")
    return response


@app.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request, user=Depends(current_user)):
    return templates.TemplateResponse("upload.html", {"request": request, "user": user, "mode": "file", "anonymous_upload": settings.allow_anonymous_upload})


@app.head("/upload")
def upload_head() -> Response:
    return Response(status_code=200, media_type="text/html")


@app.post("/upload")
async def upload_form(
    request: Request,
    file: UploadFile = File(...),
    visibility: str = Form("unlisted"),
    ttl_seconds: int | None = Form(None),
    storage_policy: str = Form("auto"),
    purpose: str = Form("other"),
    generate_link: bool = Form(False),
    user=Depends(current_user),
):
    ensure_upload_user(user)
    result = await save_upload(file, user["id"] if user else None, visibility, ttl_seconds, storage_policy, purpose, anonymous=user is None, generate_link=generate_link)
    audit("upload_file", request=request, user=user, target_type="file", target_id=result["file_id"], detail=purpose)
    if wants_json(request):
        result["redirect_url"] = f"/admin/files/{result['file_id']}" if user and user["role"] == "admin" else "/images" if purpose == "image" else "/"
        return JSONResponse(result)
    return RedirectResponse(f"/admin/files/{result['file_id']}" if user and user["role"] == "admin" else "/", status_code=303)


@app.get("/temp", response_class=HTMLResponse)
def temp_page(request: Request, user=Depends(current_user)):
    return templates.TemplateResponse("upload.html", {"request": request, "user": user, "mode": "temp", "anonymous_upload": settings.allow_anonymous_upload})


@app.head("/temp")
def temp_head() -> Response:
    return Response(status_code=200, media_type="text/html")


@app.post("/temp")
async def temp_upload(request: Request, file: UploadFile = File(...), ttl_seconds: int = Form(86400), storage_policy: str = Form("auto"), generate_link: bool = Form(False), user=Depends(current_user)):
    ensure_upload_user(user)
    result = await save_upload(file, user["id"] if user else None, "unlisted", ttl_seconds, storage_policy, "temp", anonymous=user is None, generate_link=generate_link)
    audit("upload_file", request=request, user=user, target_type="file", target_id=result["file_id"], detail="temp")
    if wants_json(request):
        result["redirect_url"] = result["public_url"] or "/"
        return JSONResponse(result)
    return RedirectResponse(result["public_url"] or "/", status_code=303)


@app.get("/images", response_class=HTMLResponse)
def images_page(request: Request, user=Depends(current_user)):
    with connect() as conn:
        if user and user["status"] == "active":
            rows = conn.execute("SELECT * FROM files WHERE status='active' AND purpose='image' AND owner_id=? ORDER BY uploaded_at DESC LIMIT 80", (user["id"],)).fetchall()
        else:
            rows = []
    return templates.TemplateResponse("images.html", {"request": request, "user": user, "files": rows})


@app.head("/images")
def images_head() -> Response:
    return Response(status_code=200, media_type="text/html")


@app.get("/images/{file_id}", response_class=HTMLResponse)
def image_detail(file_id: str, request: Request, user=Depends(require_user)):
    with connect() as conn:
        row = conn.execute("SELECT * FROM files WHERE file_id=? AND purpose='image'", (file_id,)).fetchone()
    if not row or not can_manage_file(row, user):
        raise HTTPException(status_code=404, detail="Image not found")
    return templates.TemplateResponse("image_detail.html", {"request": request, "user": user, "file": row})


@app.get("/files", response_class=HTMLResponse)
def files_page(
    request: Request,
    q: str = "",
    purpose: str = "",
    visibility: str = "",
    status: str = "active",
    storage_backend: str = "",
    link_enabled: str = "",
    sort: str = "uploaded_at",
    order: str = "desc",
    scope: str = "my",
    page: int = 1,
    page_size: int = 20,
    user=Depends(require_user),
):
    allowed_sort = {"uploaded_at", "size_bytes", "access_count", "original_filename"}
    sort = sort if sort in allowed_sort else "uploaded_at"
    order = "asc" if order.lower() == "asc" else "desc"
    page = max(1, page)
    page_size = min(100, max(1, page_size))
    clauses = []
    args = []
    if user["role"] != "admin" or scope != "all":
        clauses.append("owner_id=?")
        args.append(user["id"])
    if q:
        clauses.append("(original_filename LIKE ? OR tags LIKE ?)")
        args.extend([f"%{q}%", f"%{q}%"])
    if status == "expired":
        clauses.append("expires_at IS NOT NULL AND expires_at < ?")
        args.append(now_iso())
    for field, value in [("purpose", purpose), ("visibility", visibility), ("storage_backend", storage_backend)]:
        if value:
            clauses.append(f"{field}=?")
            args.append(value)
    if status and status not in {"all", "expired"}:
        clauses.append("status=?")
        args.append(status)
    if link_enabled in {"0", "1"}:
        clauses.append("link_enabled=?")
        args.append(int(link_enabled))
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    offset = (page - 1) * page_size
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(DISTINCT file_id) AS c FROM files {where}", args).fetchone()["c"]
        rows = conn.execute(f"SELECT DISTINCT * FROM files {where} ORDER BY {sort} {order.upper()}, id DESC LIMIT ? OFFSET ?", (*args, page_size, offset)).fetchall()
    filters = {"q": q, "purpose": purpose, "visibility": visibility, "status": status, "storage_backend": storage_backend, "link_enabled": link_enabled, "sort": sort, "order": order, "scope": scope, "page_size": page_size}
    return templates.TemplateResponse("files.html", {"request": request, "user": user, "files": rows, "filters": filters, "page": page, "page_size": page_size, "total": total, "has_prev": page > 1, "has_next": offset + len(rows) < total})


@app.get("/files/{file_id}", response_class=HTMLResponse)
def file_detail(file_id: str, request: Request, user=Depends(require_user)):
    with connect() as conn:
        row = conn.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
    if not row or not can_manage_file(row, user):
        raise HTTPException(status_code=404, detail="File not found")
    return templates.TemplateResponse("file_detail.html", {"request": request, "user": user, "file": row})


@app.post("/files/{file_id}/tags")
def file_tags_update(file_id: str, request: Request, tags: str = Form(""), user=Depends(require_user)):
    update_file_tags(file_id, tags, user)
    audit("update_tags", request=request, user=user, target_type="file", target_id=file_id)
    return RedirectResponse(request.headers.get("referer") or f"/files/{file_id}", status_code=303)


@app.post("/files/{file_id}/delete")
def file_delete(file_id: str, request: Request, user=Depends(require_user)):
    with connect() as conn:
        row = conn.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
    if not row or not can_manage_file(row, user):
        raise HTTPException(status_code=404, detail="File not found")
    delete_file(file_id)
    audit("delete_file", request=request, user=user, target_type="file", target_id=file_id)
    return RedirectResponse("/files", status_code=303)


def selected_manageable_files(file_ids: list[str], user):
    if not file_ids:
        return []
    placeholders = ",".join("?" for _ in file_ids)
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM files WHERE file_id IN ({placeholders})", file_ids).fetchall()
    return [row for row in rows if can_manage_file(row, user)]


@app.post("/files/bulk/delete")
def files_bulk_delete(request: Request, file_ids: list[str] = Form([]), user=Depends(require_user)):
    rows = selected_manageable_files(file_ids, user)
    for row in rows:
        delete_file(row["file_id"])
        audit("delete_file", request=request, user=user, target_type="file", target_id=row["file_id"], detail="bulk")
    return RedirectResponse(request.headers.get("referer") or "/files", status_code=303)


@app.post("/files/bulk/revoke-links")
def files_bulk_revoke_links(request: Request, file_ids: list[str] = Form([]), user=Depends(require_user)):
    rows = selected_manageable_files(file_ids, user)
    for row in rows:
        if int(row["link_enabled"] or 0) == 1:
            revoke_direct_link(row["file_id"], user)
            audit("revoke_link", request=request, user=user, target_type="file", target_id=row["file_id"], detail="bulk")
    return RedirectResponse(request.headers.get("referer") or "/files", status_code=303)


@app.get("/files/cleanup/test-files", response_class=HTMLResponse)
def files_cleanup_test_confirm(request: Request, user=Depends(require_admin)):
    names = ("hello.txt", "policy.txt", "luomofile-test.txt")
    with connect() as conn:
        rows = conn.execute("SELECT * FROM files WHERE original_filename IN (?, ?, ?) ORDER BY uploaded_at DESC", names).fetchall()
    return templates.TemplateResponse("files_cleanup_confirm.html", {"request": request, "user": user, "files": rows})


@app.post("/files/cleanup/test-files")
def files_cleanup_test_run(request: Request, user=Depends(require_admin)):
    names = ("hello.txt", "policy.txt", "luomofile-test.txt")
    with connect() as conn:
        rows = conn.execute("SELECT file_id FROM files WHERE original_filename IN (?, ?, ?) AND status!='deleted'", names).fetchall()
    for row in rows:
        delete_file(row["file_id"])
        audit("delete_file", request=request, user=user, target_type="file", target_id=row["file_id"], detail="clean_test_files")
    return RedirectResponse("/files?scope=all", status_code=303)


@app.get("/f/{file_id}/{filename}")
def public_file(file_id: str, filename: str, request: Request, user=Depends(current_user)):
    return serve_file(file_id, request, filename, user=user)


@app.get("/i/{file_id}")
def image_file(file_id: str, request: Request, user=Depends(current_user)):
    return serve_file(file_id, request, user=user)


@app.get("/t/{file_id}")
def temp_file(file_id: str, request: Request, user=Depends(current_user)):
    return serve_file(file_id, request, user=user)


@app.post("/api/v1/files/upload")
async def api_upload(file: UploadFile = File(...), visibility: str = Form("unlisted"), ttl_seconds: int | None = Form(None),
                     storage_policy: str = Form("auto"), purpose: str = Form("api"), generate_link: bool = Form(False),
                     key=Depends(require_api_key(["files:upload"]))):
    result = await save_upload(file, key["user_id"], visibility, ttl_seconds, storage_policy, purpose, generate_link=generate_link)
    return result


@app.post("/api/v1/temp/upload")
async def api_temp_upload(file: UploadFile = File(...), ttl_seconds: int = Form(86400), storage_policy: str = Form("auto"), generate_link: bool = Form(False),
                          key=Depends(require_api_key(["temp:upload"]))):
    return await save_upload(file, key["user_id"], "unlisted", ttl_seconds, storage_policy, "temp", generate_link=generate_link)


@app.post("/api/v1/images/upload")
async def api_image_upload(file: UploadFile = File(...), visibility: str = Form("public"), storage_policy: str = Form("auto"), generate_link: bool = Form(False),
                           key=Depends(require_api_key(["images:upload"]))):
    return await save_upload(file, key["user_id"], visibility, None, storage_policy, "image", generate_link=generate_link)


@app.post("/api/v1/files/{file_id}/link/generate")
def api_file_link_generate(file_id: str, key=Depends(require_api_key(["files:link:create"]))):
    with connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE id=? AND status='active'", (key["user_id"],)).fetchone()
    return generate_direct_link(file_id, user)


@app.post("/api/v1/files/{file_id}/link/revoke")
def api_file_link_revoke(file_id: str, key=Depends(require_api_key(["files:link:revoke"]))):
    with connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE id=? AND status='active'", (key["user_id"],)).fetchone()
    return revoke_direct_link(file_id, user)


@app.post("/api/internal/files/upload")
async def api_internal_upload(request: Request, file: UploadFile = File(...), visibility: str = Form("unlisted"), ttl_seconds: int | None = Form(None),
                              storage_policy: str = Form("auto"), purpose: str = Form("api"), generate_link: bool = Form(False),
                              ):
    require_internal_token(request)
    return await save_upload(file, None, visibility, ttl_seconds, storage_policy, purpose, generate_link=generate_link)


@app.get("/api/internal/files")
def api_internal_files(request: Request):
    require_internal_token(request)
    with connect() as conn:
        rows = conn.execute("SELECT file_id, original_filename, size_bytes, mime_type, storage_backend, visibility, purpose, status, uploaded_at, expires_at, link_enabled, public_url FROM files ORDER BY uploaded_at DESC LIMIT 100").fetchall()
    return {"files": [dict(row) for row in rows]}


@app.get("/api/internal/files/{file_id}")
def api_internal_file_detail(file_id: str, request: Request):
    require_internal_token(request)
    with connect() as conn:
        row = conn.execute("SELECT file_id, original_filename, size_bytes, mime_type, storage_backend, visibility, purpose, status, uploaded_at, expires_at, link_enabled, public_url, access_count, last_accessed_at FROM files WHERE file_id=?", (file_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "File not found."})
    return dict(row)


@app.delete("/api/internal/files/{file_id}")
def api_internal_file_delete(file_id: str, request: Request):
    require_internal_token(request)
    if not delete_file(file_id):
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "File not found."})
    return {"ok": True}


@app.get("/api/internal/storage/stats")
def api_internal_storage_stats(request: Request):
    require_internal_token(request)
    with connect() as conn:
        rows = conn.execute("SELECT name, type, enabled, bucket_name, max_capacity_bytes, used_bytes_cache, updated_at FROM storage_backends ORDER BY priority").fetchall()
    return {"backends": [dict(row) for row in rows]}


@app.get("/api/internal/metrics")
def api_internal_metrics(request: Request):
    require_internal_token(request)
    with connect() as conn:
        return {
            "files": conn.execute("SELECT COUNT(*) AS c FROM files WHERE status='active'").fetchone()["c"],
            "bytes": conn.execute("SELECT COALESCE(SUM(size_bytes),0) AS c FROM files WHERE status='active'").fetchone()["c"],
            "public_links": conn.execute("SELECT COUNT(*) AS c FROM files WHERE status='active' AND link_enabled=1").fetchone()["c"],
        }


@app.get("/api/v1/files")
def api_files(key=Depends(require_api_key(["files:list"]))):
    with connect() as conn:
        rows = conn.execute("SELECT file_id, original_filename, size_bytes, mime_type, storage_backend, visibility, purpose, status, uploaded_at, expires_at, link_enabled, public_url FROM files WHERE owner_id=? ORDER BY uploaded_at DESC LIMIT 100", (key["user_id"],)).fetchall()
    return {"files": [dict(row) for row in rows]}


@app.post("/files/{file_id}/link/generate")
def file_link_generate(file_id: str, request: Request, user=Depends(require_user)):
    generate_direct_link(file_id, user)
    audit("generate_link", request=request, user=user, target_type="file", target_id=file_id)
    target = request.headers.get("referer") or "/images"
    return RedirectResponse(target, status_code=303)


@app.post("/files/{file_id}/link/revoke")
def file_link_revoke(file_id: str, request: Request, user=Depends(require_user)):
    revoke_direct_link(file_id, user)
    audit("revoke_link", request=request, user=user, target_type="file", target_id=file_id)
    target = request.headers.get("referer") or "/images"
    return RedirectResponse(target, status_code=303)


@app.get("/api/v1/files/{file_id}")
def api_file_detail(file_id: str, key=Depends(require_api_key(["files:read"]))):
    with connect() as conn:
        row = conn.execute("SELECT * FROM files WHERE file_id=? AND owner_id=?", (file_id, key["user_id"])).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "File not found."})
    return dict(row)


@app.delete("/api/v1/files/{file_id}")
def api_file_delete(file_id: str, key=Depends(require_api_key(["files:delete"]))):
    with connect() as conn:
        row = conn.execute("SELECT * FROM files WHERE file_id=? AND owner_id=?", (file_id, key["user_id"])).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "File not found."})
    delete_file(file_id)
    return {"ok": True}


@app.get("/api/v1/storage/stats")
def api_storage_stats(key=Depends(require_api_key(["admin:read"]))):
    with connect() as conn:
        rows = conn.execute("SELECT name, type, enabled, bucket_name, max_capacity_bytes, used_bytes_cache FROM storage_backends ORDER BY priority").fetchall()
    return {"backends": [dict(row) for row in rows]}


@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, user=Depends(require_admin)):
    with connect() as conn:
        files = conn.execute("SELECT * FROM files ORDER BY uploaded_at DESC LIMIT 10").fetchall()
        backends = conn.execute("SELECT * FROM storage_backends ORDER BY priority").fetchall()
        jobs = conn.execute("SELECT * FROM cleanup_jobs ORDER BY started_at DESC LIMIT 5").fetchall()
    return templates.TemplateResponse("admin.html", {"request": request, "user": user, "files": files, "backends": backends, "jobs": jobs})


@app.head("/admin")
def admin_head(user=Depends(require_admin)) -> Response:
    return Response(status_code=200, media_type="text/html")


TYPE_CONDITIONS = {
    "image":   "mime_type LIKE 'image/%'",
    "document": "mime_type LIKE 'text/%' OR mime_type IN ('application/pdf','application/msword','application/vnd.openxmlformats-officedocument.wordprocessingml.document','application/vnd.ms-excel','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','application/vnd.oasis.opendocument.text','application/vnd.oasis.opendocument.spreadsheet')",
    "archive":  "(mime_type LIKE '%zip%' OR mime_type LIKE '%tar%' OR mime_type LIKE '%rar%' OR mime_type LIKE '%gzip%' OR mime_type LIKE '%compress%' OR mime_type LIKE '%x-7z%' OR extension IN ('.zip','.tar','.gz','.bz2','.xz','.7z','.rar','.tgz'))",
    "audio":    "mime_type LIKE 'audio/%'",
    "video":    "mime_type LIKE 'video/%'",
    "code":     "(mime_type IN ('application/javascript','text/javascript','application/json','text/xml','text/x-python','text/x-java') OR extension IN ('.py','.js','.ts','.jsx','.tsx','.html','.css','.json','.xml','.yaml','.yml','.sh','.bash','.sql','.md','.java','.c','.cpp','.h','.hpp','.php','.rb','.go','.rs','.swift','.kt','.toml','.ini','.cfg'))",
}

SORT_MAP = {
    "created_desc": "f.uploaded_at DESC",
    "created_asc":  "f.uploaded_at ASC",
    "size_desc":    "f.size_bytes DESC",
    "size_asc":     "f.size_bytes ASC",
    "name_asc":     "f.original_filename ASC",
}

TIME_MAP = {
    "today": "date('now')",
    "7d":    "date('now', '-7 days')",
    "30d":   "date('now', '-30 days')",
}

SIZE_MAP = {
    "small":  (None, 1048576),
    "medium": (1048576, 10485760),
    "large":  (10485760, None),
}

@app.get("/admin/files", response_class=HTMLResponse)
def admin_files(
    request: Request,
    q: str = "",
    type: str = "",
    visibility: str = "",
    time: str = "",
    size: str = "",
    sort: str = "created_desc",
    page: int = 1,
    page_size: int = 50,
    user=Depends(require_admin),
):
    sort_order = SORT_MAP.get(sort, "f.uploaded_at DESC")
    page = max(1, page)
    page_size = min(200, max(10, page_size))
    clauses = ["f.status='active'"]
    args = []

    if q:
        clauses.append("(f.original_filename LIKE ? OR f.notes LIKE ? OR f.object_key LIKE ? OR f.file_id LIKE ?)")
        like = f"%{q}%"
        args.extend([like, like, like, like])

    if type and type in TYPE_CONDITIONS:
        if type == "other":
            others = [v for k, v in TYPE_CONDITIONS.items() if k != "other"]
            clauses.append("NOT (" + " OR ".join(others) + ")")
        else:
            clauses.append(TYPE_CONDITIONS[type])

    if visibility:
        clauses.append("f.visibility=?")
        args.append(visibility)

    if time and time in TIME_MAP:
        clauses.append(f"date(f.uploaded_at) >= {TIME_MAP[time]}")

    if size and size in SIZE_MAP:
        lo, hi = SIZE_MAP[size]
        if lo is not None:
            clauses.append("f.size_bytes >= ?")
            args.append(lo)
        if hi is not None:
            clauses.append("f.size_bytes < ?")
            args.append(hi)

    where = " AND ".join(clauses)
    offset = (page - 1) * page_size

    with connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM files f WHERE {where}", args
        ).fetchone()["c"]
        files = conn.execute(
            f"SELECT f.*, u.username AS owner_username FROM files f LEFT JOIN users u ON u.id = f.owner_id WHERE {where} ORDER BY {sort_order} LIMIT ? OFFSET ?",
            args + [page_size, offset],
        ).fetchall()

    import urllib.parse
    query_parts = []
    for k, v in [("q", q), ("type", type), ("visibility", visibility), ("time", time), ("size", size), ("sort", sort)]:
        if v:
            query_parts.append(f"{k}={urllib.parse.quote(str(v))}")
    query_string = "&".join(query_parts)

    total_pages = max(1, (total + page_size - 1) // page_size)

    return templates.TemplateResponse("admin_files.html", {
        "request": request, "user": user, "files": files,
        "q": q, "type": type, "visibility": visibility,
        "time": time, "size": size, "sort": sort,
        "page": page, "page_size": page_size,
        "total": total, "total_pages": total_pages,
        "query_string": query_string,
    })


@app.get("/admin/files/{file_id}", response_class=HTMLResponse)
def admin_file_detail(file_id: str, request: Request, user=Depends(require_admin)):
    with connect() as conn:
        row = conn.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="File not found")
    return templates.TemplateResponse("admin_file_detail.html", {"request": request, "user": user, "file": row})


@app.post("/admin/files/{file_id}/delete")
def admin_file_delete(file_id: str, request: Request, user=Depends(require_admin)):
    delete_file(file_id)
    audit("delete_file", request=request, user=user, target_type="file", target_id=file_id)
    return RedirectResponse("/admin/files", status_code=303)


@app.post("/admin/files/{file_id}/visibility")
def admin_file_visibility(file_id: str, request: Request, visibility: str = Form("unlisted"), user=Depends(require_admin)):
    with connect() as conn:
        conn.execute("UPDATE files SET visibility=?, updated_at=? WHERE file_id=?", (visibility, now_iso(), file_id))
        conn.commit()
    audit("change_visibility", request=request, user=user, target_type="file", target_id=file_id, detail=visibility)
    return RedirectResponse(f"/admin/files/{file_id}", status_code=303)


@app.get("/admin/storage", response_class=HTMLResponse)
def admin_storage(request: Request, user=Depends(require_admin)):
    with connect() as conn:
        rows = conn.execute("SELECT * FROM storage_backends ORDER BY priority").fetchall()
    return templates.TemplateResponse("admin_storage.html", {"request": request, "user": user, "backends": rows})


@app.post("/admin/cleanup/run")
def admin_cleanup_run(request: Request, user=Depends(require_admin)):
    cleanup_once()
    audit("cleanup_run", request=request, user=user, target_type="cleanup", target_id="manual")
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/api-keys", response_class=HTMLResponse)
def admin_api_keys(request: Request, created_key: str | None = None, user=Depends(require_admin)):
    with connect() as conn:
        keys = conn.execute("SELECT k.*, u.username FROM api_keys k JOIN users u ON u.id=k.user_id ORDER BY k.created_at DESC").fetchall()
        users = conn.execute("SELECT * FROM users WHERE status='active' ORDER BY username").fetchall()
    return templates.TemplateResponse("admin_api_keys.html", {"request": request, "user": user, "keys": keys, "users": users, "created_key": created_key})


@app.post("/admin/api-keys/create")
def admin_api_key_create(user_id: int = Form(...), name: str = Form(...), scopes: str = Form("files:upload,files:read,files:delete,files:list,temp:upload,images:upload,files:link:create,files:link:revoke"), user=Depends(require_admin)):
    token = generate_api_key()
    now = now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO api_keys(user_id, name, key_prefix, key_hash, scopes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, name.strip(), token[:12], hash_token(token), scopes, now),
        )
        conn.commit()
    audit("create_api_key", user=user, target_type="api_key", target_id=name.strip(), detail=scopes)
    return RedirectResponse(f"/admin/api-keys?created_key={token}", status_code=303)


@app.post("/admin/api-keys/{key_id}/revoke")
def admin_api_key_revoke(key_id: int, request: Request, user=Depends(require_admin)):
    with connect() as conn:
        conn.execute("UPDATE api_keys SET status='revoked', revoked_at=? WHERE id=?", (now_iso(), key_id))
        conn.commit()
    audit("revoke_api_key", request=request, user=user, target_type="api_key", target_id=str(key_id))
    return RedirectResponse("/admin/api-keys", status_code=303)


@app.get("/developer", response_class=HTMLResponse)
def developer_home(request: Request, user=Depends(require_user)):
    return templates.TemplateResponse("developer.html", {"request": request, "user": user})


@app.get("/developer/docs", response_class=HTMLResponse)
def developer_docs(request: Request, user=Depends(require_user)):
    return templates.TemplateResponse("developer_docs.html", {"request": request, "user": user, "base_url": settings.app_base_url})
