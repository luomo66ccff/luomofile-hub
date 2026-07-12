import hashlib
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
from app.storage.router import choose_backend, get_backend


VALID_VISIBILITY = {"public", "private", "unlisted"}
VALID_PURPOSES = {"image", "temp", "api", "backup", "other"}
VALID_POLICIES = {"auto", "r2", "cos", "cos_sv", "local_temp"}


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
    temp_dir = settings.database_path.parent / "uploads"
    temp_dir.mkdir(parents=True, exist_ok=True)
    file_id = new_file_id()
    original = upload.filename or "file"
    safe_name = sanitize_filename(original)
    temp_path = temp_dir / f"{file_id}.upload"
    sha = hashlib.sha256()
    size = 0
    with temp_path.open("wb") as handle:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            sha.update(chunk)
            handle.write(chunk)
            validate_upload(size, upload.content_type, purpose, anonymous=anonymous)
    validate_upload(size, upload.content_type, purpose, anonymous=anonymous)
    backend_info = choose_backend(size, upload.content_type, purpose, storage_policy)
    key = object_key(purpose, file_id, safe_name)
    backend = get_backend(backend_info.name)
    backend.put_file(temp_path, key, upload.content_type)
    temp_path.unlink(missing_ok=True)
    now = now_iso()
    expires_at = None
    if ttl_seconds:
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=int(ttl_seconds))).isoformat(timespec="seconds")
    link_enabled = bool(generate_link and visibility != "private")
    public_url = public_link(file_id, safe_name, purpose) if link_enabled else None
    link_created_at = now if link_enabled else None
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO files(file_id, owner_id, original_filename, stored_filename, extension, mime_type,
            size_bytes, sha256, storage_backend, bucket_name, object_key, public_url, link_enabled, link_created_at,
            visibility, purpose, status, expires_at, uploaded_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (
                file_id,
                owner_id,
                original,
                safe_name,
                Path(safe_name).suffix.lower(),
                upload.content_type or "application/octet-stream",
                size,
                sha.hexdigest(),
                backend_info.name,
                backend_info.bucket_name,
                key,
                public_url,
                1 if link_enabled else 0,
                link_created_at,
                visibility,
                purpose,
                expires_at,
                now,
                now,
            ),
        )
        conn.execute(
            "UPDATE storage_backends SET used_bytes_cache = used_bytes_cache + ?, updated_at = ? WHERE name = ?",
            (size, now, backend_info.name),
        )
        conn.commit()
    return {
        "file_id": file_id,
        "filename": original,
        "size": size,
        "mime_type": upload.content_type or "application/octet-stream",
        "storage_backend": backend_info.name,
        "link_enabled": link_enabled,
        "public_url": public_url,
        "expires_at": expires_at,
    }


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
    backend = get_backend(row["storage_backend"])
    if row["storage_backend"] == "local_temp":
        path = LocalTempBackend()._path(row["object_key"])
        response = FileResponse(
            path,
            media_type=row["mime_type"] or "application/octet-stream",
            filename=row["stored_filename"],
            headers={"Content-Disposition": content_disposition(row["mime_type"], row["stored_filename"])},
        )
    elif row["storage_backend"] in {"cos", "cos_sv"} and isinstance(backend, CosBackend) and backend.use_mount:
        path = backend._path(row["object_key"])
        response = FileResponse(
            path,
            media_type=row["mime_type"] or "application/octet-stream",
            filename=row["stored_filename"],
            headers={"Content-Disposition": content_disposition(row["mime_type"], row["stored_filename"])},
        )
    else:
        url = backend.get_public_url(row["object_key"])
        if url:
            response = RedirectResponse(url, status_code=302)
        else:
            raise HTTPException(status_code=500, detail={"error": "internal_error", "message": "Backend has no public URL."})
    log_access(row, request, 200, int(row["size_bytes"] or 0))
    with connect() as conn:
        conn.execute(
            "UPDATE files SET access_count = access_count + 1, last_accessed_at = ? WHERE file_id = ?",
            (now_iso(), file_id),
        )
        conn.commit()
    return response


def delete_file(file_id: str) -> bool:
    row = get_file_row(file_id)
    if not row:
        return False
    backend = get_backend(row["storage_backend"])
    backend.delete_file(row["object_key"])
    now = now_iso()
    with connect() as conn:
        conn.execute("UPDATE files SET status='deleted', updated_at=? WHERE file_id=?", (now, file_id))
        conn.execute(
            "UPDATE storage_backends SET used_bytes_cache = MAX(0, used_bytes_cache - ?), updated_at=? WHERE name=?",
            (int(row["size_bytes"] or 0), now, row["storage_backend"]),
        )
        conn.commit()
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
