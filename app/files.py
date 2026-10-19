import asyncio
from contextlib import closing
import hashlib
import logging
import re
import secrets
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response

from app.config import settings
from app.db import connect
from app.security import content_disposition, now_iso
from app.storage.local_temp import LocalTempBackend
from app.storage.cos import CosBackend
from app.storage.router import backend_infos, choose_backend, get_backend
from app.storage.disk_space import ensure_disk_space
from app.subscription_identity import SubscriptionIdentity, render_subscription_payload
from app.subscription_formats import FORMAT_OPTIONS, convert


VALID_VISIBILITY = {"public", "private", "unlisted"}
VALID_PURPOSES = {"image", "temp", "api", "backup", "other"}
VALID_POLICIES = {"auto", "r2", "cos", "cos_sv", "local_temp", "private_media"}


async def _blocking(function, *args, runner=None):
    """Finish file writes before cleaning their staging paths on cancellation."""
    task = asyncio.create_task(runner(function, *args) if runner else asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            pass
        raise


def _reserve_private_upload(file_id, key, size):
    backend = get_backend("private_media")
    ensure_disk_space(backend.root, size)
    now = now_iso()
    expiry = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(timespec="seconds")
    with closing(connect()) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        # Reclaim only expired, recorded, unpublished private uploads by their exact key.
        stale = conn.execute("SELECT * FROM gallery_upload_reservations WHERE expires_at<? ORDER BY expires_at LIMIT 20", (now,)).fetchall()
        for pending in stale:
            published = conn.execute("SELECT 1 FROM files WHERE file_id=?", (pending["file_id"],)).fetchone()
            if not published:
                if not pending["object_key"].startswith("images/") or f"/{pending['file_id']}/" not in pending["object_key"]:
                    continue
                try:
                    backend.delete_file(pending["object_key"])
                except OSError:
                    continue
            conn.execute("DELETE FROM gallery_upload_reservations WHERE file_id=?", (pending["file_id"],))
        reserved = conn.execute("SELECT COALESCE(SUM(size_bytes),0) FROM gallery_upload_reservations").fetchone()[0]
        used = conn.execute("SELECT COALESCE(SUM(size_bytes),0) FROM files WHERE storage_backend='private_media' AND status!='deleted'").fetchone()[0]
        info = conn.execute("SELECT enabled,max_capacity_bytes FROM storage_backends WHERE name='private_media'").fetchone()
        if info is None or not info["enabled"] or used + reserved + size > int(info["max_capacity_bytes"] or 0):
            conn.rollback()
            raise HTTPException(507, detail={"error": "private_storage_full", "message": "私有图片存储容量不足。"})
        conn.execute("INSERT INTO gallery_upload_reservations(file_id,object_key,size_bytes,created_at,expires_at) VALUES (?,?,?,?,?)", (file_id, key, size, now, expiry))
        conn.execute("UPDATE storage_backends SET used_bytes_cache=?,reserved_bytes=?,updated_at=? WHERE name='private_media'", (used, reserved + size, now))
        conn.commit()


def _release_private_upload(file_id, *, remove_object=False):
    with closing(connect()) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM gallery_upload_reservations WHERE file_id=?", (file_id,)).fetchone()
        if row is None:
            return
        if remove_object:
            get_backend("private_media").delete_file(row["object_key"])
        conn.execute("DELETE FROM gallery_upload_reservations WHERE file_id=?", (file_id,))
        total = conn.execute("SELECT COALESCE(SUM(size_bytes),0) FROM gallery_upload_reservations").fetchone()[0]
        conn.execute("UPDATE storage_backends SET reserved_bytes=?,updated_at=? WHERE name='private_media'", (total, now_iso()))
        conn.commit()


def sanitize_filename(filename: str) -> str:
    name = Path(filename or "file").name
    name = re.sub(r"[\x00-\x1f\x7f]+", "_", name)
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    return name[:180] or "file"


def new_file_id() -> str:
    return "lf_" + secrets.token_urlsafe(16)


def object_key(purpose: str, file_id: str, filename: str) -> str:
    now = datetime.now(timezone.utc)
    bucket = "images" if purpose == "image" else purpose
    return f"{bucket}/{now:%Y/%m/%d}/{file_id}/{sanitize_filename(filename)}"


def validate_upload(size: int, mime_type: str | None, purpose: str, anonymous: bool = False) -> None:
    if size > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail={"error": "file_too_large", "message": "File exceeds maximum upload size."})
    if anonymous and (not settings.allow_anonymous_upload or size > settings.anonymous_max_upload_bytes):
        raise HTTPException(status_code=403, detail={"error": "forbidden", "message": "Anonymous upload is not allowed for this file."})
    if purpose == "image" and size > settings.max_image_bytes:
        raise HTTPException(status_code=413, detail={"error": "file_too_large", "message": "Image exceeds maximum upload size."})
    if purpose == "temp" and size > settings.max_temp_bytes:
        raise HTTPException(status_code=413, detail={"error": "file_too_large", "message": "Temporary file exceeds maximum upload size."})


async def save_upload(
    upload: UploadFile,
    owner_id: int | None,
    visibility: str = "unlisted",
    ttl_seconds: int | None = None,
    storage_policy: str = "auto",
    purpose: str = "other",
    anonymous: bool = False,
    generate_link: bool = False,
) -> dict:
    visibility = visibility if visibility in VALID_VISIBILITY else "unlisted"
    purpose = purpose if purpose in VALID_PURPOSES else "other"
    storage_policy = storage_policy if storage_policy in VALID_POLICIES else "auto"
    # Automatic image uploads share the controlled private gallery storage.
    # Explicit legacy policies remain available to existing API consumers.
    if purpose == "image" and storage_policy == "auto":
        storage_policy = "private_media"
    if storage_policy == "private_media" and purpose != "image":
        raise HTTPException(422, detail={"error": "invalid_private_media_purpose", "message": "私有图片存储仅接受经过验证的图片。"})
    temp_dir = settings.database_path.parent / "uploads"
    temp_dir.mkdir(parents=True, exist_ok=True)
    file_id = new_file_id()
    original = upload.filename or "file"
    safe_name = sanitize_filename(original)
    temp_path = temp_dir / f"{file_id}.upload"
    sha = hashlib.sha256()
    size = 0
    key = object_key(purpose, file_id, safe_name)
    backend = None
    attempted_store = False
    committed = False
    media = None
    mime_type = upload.content_type or "application/octet-stream"
    try:
        with temp_path.open("wb") as handle:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                validate_upload(size, mime_type, purpose, anonymous=anonymous)
                ensure_disk_space(temp_dir, len(chunk))
                sha.update(chunk)
                handle.write(chunk)
        validate_upload(size, mime_type, purpose, anonymous=anonymous)
        if storage_policy == "private_media":
            await _blocking(_reserve_private_upload, file_id, key, size)
            backend_info = backend_infos()["private_media"]
        else:
            backend_info = choose_backend(size, mime_type, purpose, storage_policy)
        if purpose == "image":
            from app.image_media import ImageMediaError, build_image_media, run_image_work
            try:
                media = await _blocking(build_image_media, temp_path, file_id, sha.hexdigest(), runner=run_image_work)
            except ImageMediaError as exc:
                raise HTTPException(exc.status_code, detail={"error": exc.code, "message": exc.message}) from exc
            mime_type = media["detected_mime"]
        backend = get_backend(backend_info.name)
        if isinstance(backend, LocalTempBackend):
            ensure_disk_space(backend.root, size)
        attempted_store = True
        await _blocking(backend.put_file, temp_path, key, mime_type)
        now = now_iso()
        expires_at = None
        if ttl_seconds:
            expires_at = (datetime.now(timezone.utc) + timedelta(seconds=int(ttl_seconds))).isoformat(timespec="seconds")
        link_enabled = bool(generate_link and visibility != "private")
        public_url = public_link(file_id, safe_name, purpose) if link_enabled else None
        link_created_at = now if link_enabled else None
        with closing(connect()) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(
                """INSERT INTO files(file_id, owner_id, original_filename, stored_filename, extension, mime_type,
                size_bytes, sha256, storage_backend, bucket_name, object_key, public_url, link_enabled, link_created_at,
                visibility, purpose, status, expires_at, uploaded_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                (file_id, owner_id, original, safe_name, Path(safe_name).suffix.lower(), mime_type, size, sha.hexdigest(),
                 backend_info.name, backend_info.bucket_name, key, public_url, int(link_enabled), link_created_at,
                 visibility, purpose, expires_at, now, now),
            )
            if media is not None:
                from app.image_media import record_image_media
                record_image_media(conn, file_id, media)
            conn.execute("UPDATE storage_backends SET used_bytes_cache=used_bytes_cache+?,updated_at=? WHERE name=?", (size, now, backend_info.name))
            if storage_policy == "private_media":
                conn.execute("DELETE FROM gallery_upload_reservations WHERE file_id=?", (file_id,))
                outstanding = conn.execute("SELECT COALESCE(SUM(size_bytes),0) FROM gallery_upload_reservations").fetchone()[0]
                conn.execute("UPDATE storage_backends SET reserved_bytes=? WHERE name='private_media'", (outstanding,))
            conn.commit()
            committed = True
        result = {"file_id": file_id, "filename": original, "size": size, "mime_type": mime_type,
                  "storage_backend": backend_info.name, "link_enabled": link_enabled, "public_url": public_url,
                  "expires_at": expires_at}
        if media is not None:
            result.update(width=media["width"], height=media["height"], media_status="ready")
        return result
    finally:
        temp_path.unlink(missing_ok=True)
        if not committed:
            if storage_policy == "private_media":
                try:
                    # Failed/cancelled uploads never leave an untracked private original.
                    _release_private_upload(file_id, remove_object=True)
                except Exception:
                    logging.getLogger(__name__).warning("private_upload_cleanup_pending file_id=%s", file_id)
            elif attempted_store and backend is not None:
                try:
                    backend.delete_file(key)
                except Exception:
                    logging.getLogger(__name__).warning("upload_cleanup_pending file_id=%s", file_id)
            if purpose == "image":
                try:
                    from app.image_media import discard_image_media
                    discard_image_media(file_id)
                except Exception:
                    logging.getLogger(__name__).warning("image_cache_cleanup_pending file_id=%s", file_id)


def public_link(file_id: str, filename: str, purpose: str) -> str:
    if purpose == "image":
        return f"{settings.app_base_url}/i/{file_id}"
    if purpose == "temp":
        return f"{settings.app_base_url}/t/{file_id}"
    return f"{settings.app_base_url}/f/{file_id}/{sanitize_filename(filename)}"


def get_file_row(file_id: str):
    with connect() as conn:
        return conn.execute("SELECT * FROM files WHERE file_id = ?", (file_id,)).fetchone()


def canonical_public_url(row) -> str:
    return public_link(row["file_id"], row["stored_filename"], row["purpose"])


def is_active_user(user) -> bool:
    return bool(user and user["status"] == "active")


def can_manage_file(row, user) -> bool:
    if not row or not is_active_user(user):
        return False
    return user["role"] == "admin" or (row["owner_id"] is not None and row["owner_id"] == user["id"])


def can_access_file(row, user) -> bool:
    if not row or row["status"] != "active" or is_expired(row):
        return False
    if row["visibility"] == "private":
        return can_manage_file(row, user)
    if int(row["link_enabled"] or 0) == 1:
        return True
    return can_manage_file(row, user)


def generate_direct_link(file_id: str, user) -> dict:
    row = get_file_row(file_id)
    if not row or not can_manage_file(row, user):
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "File not found."})
    if row["status"] != "active" or is_expired(row):
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "File not found."})
    if row["visibility"] == "private":
        raise HTTPException(status_code=403, detail={"error": "forbidden", "message": "Private files cannot have public direct links."})
    now = now_iso()
    url = canonical_public_url(row)
    with connect() as conn:
        conn.execute(
            "UPDATE files SET link_enabled=1, public_url=?, link_created_at=COALESCE(link_created_at, ?), link_revoked_at=NULL, updated_at=? WHERE file_id=?",
            (url, now, now, file_id),
        )
        conn.commit()
    return {"file_id": file_id, "link_enabled": True, "public_url": url}


def revoke_direct_link(file_id: str, user) -> dict:
    row = get_file_row(file_id)
    if not row or not can_manage_file(row, user):
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "File not found."})
    now = now_iso()
    with connect() as conn:
        conn.execute(
            "UPDATE files SET link_enabled=0, public_url=NULL, link_revoked_at=?, updated_at=? WHERE file_id=?",
            (now, now, file_id),
        )
        conn.commit()
    return {"file_id": file_id, "link_enabled": False, "public_url": None}


def update_file_tags(file_id: str, tags: str, user) -> None:
    row = get_file_row(file_id)
    if not row or not can_manage_file(row, user):
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "File not found."})
    cleaned = ",".join(part.strip()[:40] for part in tags.split(",") if part.strip())[:300]
    now = now_iso()
    with connect() as conn:
        conn.execute("UPDATE files SET tags=?, updated_at=? WHERE file_id=?", (cleaned, now, file_id))
        conn.commit()


def is_expired(row) -> bool:
    return bool(row["expires_at"] and datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc))


def serve_file(file_id: str, request: Request, filename: str | None = None, user=None) -> Response:
    row = get_file_row(file_id)
    if not row or row["status"] != "active":
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "File not found."})
    if is_expired(row):
        raise HTTPException(status_code=410, detail={"error": "file_expired", "message": "File has expired."})
    if not can_access_file(row, user):
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "File not found."})
    return _serve_file_row(row, request)


def serve_protected_subscription(
    file_id: str,
    request: Request,
    identity: SubscriptionIdentity,
    subscription_format: str = "mihomo",
    include_private: bool = False,
) -> Response:
    row = get_file_row(file_id)
    if not row or row["status"] != "active" or is_expired(row):
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Subscription not found."})
    return _serve_file_row(row, request, protected_subscription=True, subscription_identity=identity,
                           subscription_format=subscription_format, include_private=include_private)


def _serve_file_row(
    row,
    request: Request,
    protected_subscription: bool = False,
    subscription_identity: SubscriptionIdentity | None = None,
    subscription_format: str = "mihomo",
    include_private: bool = False,
) -> Response:
    backend = get_backend(row["storage_backend"])
    response_headers = None
    media_type = row["mime_type"] or "application/octet-stream"
    filename = row["stored_filename"]
    if row["storage_backend"] == "private_media":
        from app.image_media import NO_STORE
        response_headers = {**NO_STORE, "Content-Disposition": content_disposition(row["mime_type"], filename)}
    if protected_subscription:
        format_info = FORMAT_OPTIONS[subscription_format]
        media_type = format_info["media_type"]
        filename = format_info["filename"]
        response_headers = {
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "private, no-store, max-age=0",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow, noarchive",
            "X-Subscription-Format": subscription_format,
            "X-Content-Type-Options": "nosniff",
        }
    mounted_path = None
    if row["storage_backend"] in {"local_temp", "private_media"}:
        path = backend._path(row["object_key"])
        mounted_path = path
    elif row["storage_backend"] in {"cos", "cos_sv"} and isinstance(backend, CosBackend) and backend.use_mount:
        path = backend._path(row["object_key"])
        mounted_path = path

    sent_bytes = int(row["size_bytes"] or 0)
    if protected_subscription:
        if not mounted_path or subscription_identity is None:
            raise HTTPException(status_code=503, detail={"error": "protected_backend_unavailable", "message": "Protected subscription storage is unavailable."})
        try:
            rendered = render_subscription_payload(Path(mounted_path).read_bytes(), subscription_identity)
            if subscription_format != "mihomo":
                rendered, _ = convert(rendered, subscription_format, include_private=include_private)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail={"error": "subscription_render_failed", "message": "Protected subscription could not be rendered."}) from exc
        if request.method == "HEAD":
            response_headers["Content-Length"] = str(len(rendered))
        response = Response(content=b"" if request.method == "HEAD" else rendered, media_type=media_type, headers=response_headers)
        sent_bytes = len(rendered)
    elif row["storage_backend"] in {"local_temp", "private_media"}:
        response = FileResponse(
            path,
            media_type=media_type,
            filename=filename,
            headers=response_headers or {"Content-Disposition": content_disposition(row["mime_type"], row["stored_filename"])},
        )
    elif row["storage_backend"] in {"cos", "cos_sv"} and isinstance(backend, CosBackend) and backend.use_mount:
        response = FileResponse(
            path,
            media_type=media_type,
            filename=filename,
            headers=response_headers or {"Content-Disposition": content_disposition(row["mime_type"], row["stored_filename"])},
        )
    else:
        url = backend.get_public_url(row["object_key"])
        if url:
            response = RedirectResponse(url, status_code=302)
        else:
            raise HTTPException(status_code=500, detail={"error": "internal_error", "message": "Backend has no public URL."})
    log_access(row, request, 200, sent_bytes)
    with connect() as conn:
        conn.execute(
            "UPDATE files SET access_count = access_count + 1, last_accessed_at = ? WHERE file_id = ?",
            (now_iso(), row["file_id"]),
        )
        conn.commit()
    return response


def delete_file(file_id: str) -> bool:
    row = get_file_row(file_id)
    if not row:
        return False
    if row["status"] == "deleted":
        return True
    backend = get_backend(row["storage_backend"])
    backend.delete_file(row["object_key"])
    now = now_iso()
    with connect() as conn:
        changed = conn.execute("UPDATE files SET status='deleted', updated_at=? WHERE file_id=? AND status!='deleted'", (now, file_id)).rowcount
        if changed:
            conn.execute(
                "UPDATE storage_backends SET used_bytes_cache = MAX(0, used_bytes_cache - ?), updated_at=? WHERE name=?",
                (int(row["size_bytes"] or 0), now, row["storage_backend"]),
            )
        conn.commit()
    if row["purpose"] == "image":
        from app.image_media import discard_image_media
        discard_image_media(file_id)
    return True


def log_access(row, request: Request, status_code: int, bytes_sent: int = 0) -> None:
    ua = (request.headers.get("user-agent") or "")[:160]
    ref = (request.headers.get("referer") or "")[:160]
    ip = request.client.host if request.client else ""
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:24] if ip else ""
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO file_access_logs(file_id, accessed_at, client_ip_hash, user_agent_preview, referer_preview, status_code, bytes_sent)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (row["file_id"], now_iso(), ip_hash, ua, ref, status_code, bytes_sent),
        )
        conn.commit()
