"""Api V1 HTTP handlers; behavior preserved from gallery-t001."""
from fastapi import Depends, File, Form, HTTPException, UploadFile
from app.auth import require_api_key
from app.db import connect
from app.files import delete_file, generate_direct_link, revoke_direct_link, save_upload
from fastapi import APIRouter
from app.web.deps import templates, render_template

router = APIRouter()


@router.post("/api/v1/files/upload")
async def api_upload(file: UploadFile = File(...), visibility: str = Form("unlisted"), ttl_seconds: int | None = Form(None),
                     storage_policy: str = Form("auto"), purpose: str = Form("api"), generate_link: bool = Form(False),
                     key=Depends(require_api_key(["files:upload"]))):
    result = await save_upload(file, key["user_id"], visibility, ttl_seconds, storage_policy, purpose, generate_link=generate_link)
    return result



@router.post("/api/v1/temp/upload")
async def api_temp_upload(file: UploadFile = File(...), ttl_seconds: int = Form(86400), storage_policy: str = Form("auto"), generate_link: bool = Form(False),
                          key=Depends(require_api_key(["temp:upload"]))):
    return await save_upload(file, key["user_id"], "unlisted", ttl_seconds, storage_policy, "temp", generate_link=generate_link)



@router.post("/api/v1/images/upload")
async def api_image_upload(file: UploadFile = File(...), visibility: str = Form("public"), storage_policy: str = Form("auto"), generate_link: bool = Form(False),
                           key=Depends(require_api_key(["images:upload"]))):
    return await save_upload(file, key["user_id"], visibility, None, storage_policy, "image", generate_link=generate_link)



@router.post("/api/v1/files/{file_id}/link/generate")
def api_file_link_generate(file_id: str, key=Depends(require_api_key(["files:link:create"]))):
    with connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE id=? AND status='active'", (key["user_id"],)).fetchone()
    return generate_direct_link(file_id, user)



@router.post("/api/v1/files/{file_id}/link/revoke")
def api_file_link_revoke(file_id: str, key=Depends(require_api_key(["files:link:revoke"]))):
    with connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE id=? AND status='active'", (key["user_id"],)).fetchone()
    return revoke_direct_link(file_id, user)



@router.get("/api/v1/files")
def api_files(key=Depends(require_api_key(["files:list"]))):
    with connect() as conn:
        rows = conn.execute("SELECT file_id, original_filename, size_bytes, mime_type, storage_backend, visibility, purpose, status, uploaded_at, expires_at, link_enabled, public_url FROM files WHERE owner_id=? ORDER BY uploaded_at DESC LIMIT 100", (key["user_id"],)).fetchall()
    return {"files": [dict(row) for row in rows]}



@router.get("/api/v1/files/{file_id}")
def api_file_detail(file_id: str, key=Depends(require_api_key(["files:read"]))):
    with connect() as conn:
        row = conn.execute("SELECT * FROM files WHERE file_id=? AND owner_id=?", (file_id, key["user_id"])).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "File not found."})
    return dict(row)



@router.delete("/api/v1/files/{file_id}")
def api_file_delete(file_id: str, key=Depends(require_api_key(["files:delete"]))):
    with connect() as conn:
        row = conn.execute("SELECT * FROM files WHERE file_id=? AND owner_id=?", (file_id, key["user_id"])).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "File not found."})
    delete_file(file_id)
    return {"ok": True}



@router.get("/api/v1/storage/stats")
def api_storage_stats(key=Depends(require_api_key(["admin:read"]))):
    with connect() as conn:
        rows = conn.execute("SELECT name, type, enabled, bucket_name, max_capacity_bytes, used_bytes_cache FROM storage_backends ORDER BY priority").fetchall()
    return {"backends": [dict(row) for row in rows]}



