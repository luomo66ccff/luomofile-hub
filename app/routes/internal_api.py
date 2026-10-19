"""Internal Api HTTP handlers; behavior preserved from gallery-t001."""
from fastapi import File, Form, HTTPException, Request, UploadFile
from app.db import connect
from app.files import delete_file, save_upload
from app.web.deps import require_internal_token
from fastapi import APIRouter
from app.web.deps import templates, render_template

router = APIRouter()


@router.post("/api/internal/files/upload")
async def api_internal_upload(request: Request, file: UploadFile = File(...), visibility: str = Form("unlisted"), ttl_seconds: int | None = Form(None),
                              storage_policy: str = Form("auto"), purpose: str = Form("api"), generate_link: bool = Form(False),
                              ):
    require_internal_token(request)
    return await save_upload(file, None, visibility, ttl_seconds, storage_policy, purpose, generate_link=generate_link)



@router.get("/api/internal/files")
def api_internal_files(request: Request):
    require_internal_token(request)
    with connect() as conn:
        rows = conn.execute("SELECT file_id, original_filename, size_bytes, mime_type, storage_backend, visibility, purpose, status, uploaded_at, expires_at, link_enabled, public_url FROM files ORDER BY uploaded_at DESC LIMIT 100").fetchall()
    return {"files": [dict(row) for row in rows]}



@router.get("/api/internal/files/{file_id}")
def api_internal_file_detail(file_id: str, request: Request):
    require_internal_token(request)
    with connect() as conn:
        row = conn.execute("SELECT file_id, original_filename, size_bytes, mime_type, storage_backend, visibility, purpose, status, uploaded_at, expires_at, link_enabled, public_url, access_count, last_accessed_at FROM files WHERE file_id=?", (file_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "File not found."})
    return dict(row)



@router.delete("/api/internal/files/{file_id}")
def api_internal_file_delete(file_id: str, request: Request):
    require_internal_token(request)
    if not delete_file(file_id):
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "File not found."})
    return {"ok": True}



@router.get("/api/internal/storage/stats")
def api_internal_storage_stats(request: Request):
    require_internal_token(request)
    with connect() as conn:
        rows = conn.execute("SELECT name, type, enabled, bucket_name, max_capacity_bytes, used_bytes_cache, updated_at FROM storage_backends ORDER BY priority").fetchall()
    return {"backends": [dict(row) for row in rows]}



@router.get("/api/internal/metrics")
def api_internal_metrics(request: Request):
    require_internal_token(request)
    with connect() as conn:
        return {
            "files": conn.execute("SELECT COUNT(*) AS c FROM files WHERE status='active'").fetchone()["c"],
            "bytes": conn.execute("SELECT COALESCE(SUM(size_bytes),0) AS c FROM files WHERE status='active'").fetchone()["c"],
            "public_links": conn.execute("SELECT COUNT(*) AS c FROM files WHERE status='active' AND link_enabled=1").fetchone()["c"],
        }



