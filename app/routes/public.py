"""Public HTTP handlers; behavior preserved from gallery-t001."""
from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from app.auth import current_user
from app.config import settings
from app.db import connect
from app.security import now_iso
from app.web.deps import storage_level
from fastapi import APIRouter
from app.web.deps import templates, render_template

router = APIRouter()


@router.get("/favicon.ico", include_in_schema=False)
def favicon():
    return RedirectResponse('/static/favicon.svg', status_code=307)



@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}



@router.head("/health")
def health_head() -> Response:
    return Response(status_code=200, media_type="application/json")



@router.get("/ready")
def ready() -> JSONResponse:
    try:
        with connect() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception:
        return JSONResponse({"status": "not_ready", "checks": {"database": "failed"}}, status_code=503)
    if not settings.session_secret or len(settings.session_secret) < 32:
        return JSONResponse({"status": "not_ready", "checks": {"database": "ok", "session_secret": "missing_or_too_short"}}, status_code=503)
    return JSONResponse({"status": "ready", "checks": {"database": "ok", "session_secret": "ok"}})



@router.get("/api/public/status")
def public_status() -> JSONResponse:
    try:
        with connect() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception:
        return JSONResponse({"status": "degraded", "label": "Degraded", "service": "LuomoFile Hub", "updated_at": now_iso(), "version": settings.app_version}, status_code=503)
    return JSONResponse({"status": "operational", "label": "Operational", "service": "LuomoFile Hub", "updated_at": now_iso(), "version": settings.app_version})



@router.get("/api/public/version")
def public_version() -> dict[str, str]:
    return {"service": "LuomoFile Hub", "version": settings.app_version}



@router.get("/api/public/storage")
def public_storage() -> dict:
    with connect() as conn:
        rows = conn.execute("SELECT name, enabled, max_capacity_bytes, used_bytes_cache FROM storage_backends ORDER BY priority").fetchall()
    available = any(row["enabled"] for row in rows)
    levels = [storage_level(int(row["used_bytes_cache"] or 0), row["max_capacity_bytes"]) for row in rows if row["enabled"]]
    status = "operational" if available and "critical" not in levels and "degraded" not in levels else "degraded"
    return {"status": status, "storage": "available" if available else "unavailable", "backends": [{"name": row["name"], "enabled": bool(row["enabled"]), "level": storage_level(int(row["used_bytes_cache"] or 0), row["max_capacity_bytes"])} for row in rows]}



@router.get("/", response_class=HTMLResponse)
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
    from app.album_service import list_albums, list_own_images
    from app.gallery_routes import normalize_photo
    album_result = list_albums(user, scope="mine" if show_private_dashboard else "public", page_size=6)
    recent_images = []
    if show_private_dashboard:
        joined = list_albums(user, scope="joined", page_size=1)
        stats.update(albums=album_result["total"], joined_albums=joined["total"])
        recent_images = [normalize_photo(item) for item in list_own_images(user, page_size=6)["images"]]
    return render_template("home.html", {"request": request, "user": user, "stats": stats, "backends": backends, "recent": recent, "recent_links": links, "show_private_dashboard": show_private_dashboard, "albums": album_result["albums"], "recent_images": recent_images})



@router.head("/")
def home_head() -> Response:
    return Response(status_code=200, media_type="text/html")



