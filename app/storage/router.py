from fastapi import HTTPException

from app.config import settings
from app.db import connect
from app.storage.base import BackendInfo, StorageBackend
from app.storage.cos import CosBackend, CosSvBackend
from app.storage.local_temp import LocalTempBackend
from app.storage.r2 import R2Backend


def backend_infos() -> dict[str, BackendInfo]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM storage_backends ORDER BY priority").fetchall()
    return {
        row["name"]: BackendInfo(
            name=row["name"],
            type=row["type"],
            enabled=bool(row["enabled"]),
            bucket_name=row["bucket_name"],
            public_base_url=row["public_base_url"],
            max_capacity_bytes=int(row["max_capacity_bytes"] or 0),
            used_bytes_cache=int(row["used_bytes_cache"] or 0),
        )
        for row in rows
    }


def get_backend(name: str) -> StorageBackend:
    if name == "r2":
        return R2Backend()
    if name == "cos":
        return CosBackend()
    if name == "cos_sv":
        return CosSvBackend()
    if name == "local_temp":
        return LocalTempBackend()
    raise ValueError(f"Unknown backend {name}")


def choose_backend(file_size: int, mime_type: str | None, purpose: str, requested_policy: str) -> BackendInfo:
    infos = backend_infos()
    candidates: list[str]
    if requested_policy in {"r2", "cos", "cos_sv", "local_temp"}:
        candidates = [requested_policy]
    elif purpose == "image" and (mime_type or "").startswith("image/") and file_size <= settings.max_image_bytes:
        candidates = ["r2", "cos_sv", "cos", "local_temp"]
    elif purpose == "temp":
        candidates = ["r2", "cos_sv", "cos", "local_temp"]
    elif file_size >= 20 * 1024 * 1024:
        candidates = ["cos_sv", "cos", "r2", "local_temp"]
    else:
        candidates = ["r2", "cos_sv", "cos", "local_temp"]
    for name in candidates:
        info = infos.get(name)
        if info and info.enabled and info.available_bytes >= file_size:
            return info
    raise HTTPException(status_code=507, detail={"error": "storage_full", "message": "No storage backend has enough free capacity."})
