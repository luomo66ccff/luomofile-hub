"""Files HTTP handlers; behavior preserved from gallery-t001."""
from urllib.parse import urlencode
from fastapi import Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from app.audit import audit
from app.auth import current_user, require_admin, require_user
from app.config import settings
from app.db import connect
from app.files import can_manage_file, delete_file, generate_direct_link, revoke_direct_link, save_upload, update_file_tags
from app.security import now_iso
from app.web.deps import wants_json, ensure_upload_user
from fastapi import APIRouter
from app.web.deps import templates, render_template

router = APIRouter()
import logging
logger = logging.getLogger("luomofile.mail")


@router.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request, user=Depends(current_user)):
    return render_template("upload.html", {"request": request, "user": user, "mode": "file", "anonymous_upload": settings.allow_anonymous_upload})



@router.head("/upload")
def upload_head() -> Response:
    return Response(status_code=200, media_type="text/html")



@router.post("/upload")
async def upload_form(
    request: Request,
    file: list[UploadFile] = File(...),
    visibility: str = Form("unlisted"),
    ttl_seconds: int | None = Form(None),
    storage_policy: str = Form("auto"),
    purpose: str = Form("other"),
    generate_link: bool = Form(False),
    user=Depends(current_user),
):
    ensure_upload_user(user)
    if purpose == "image" and storage_policy == "auto":
        storage_policy = "private_media"
    results, errors = await save_web_batch(request, file, user, visibility, ttl_seconds, storage_policy, purpose, generate_link)
    result = results[0]
    destination = "/images" if purpose == "image" else "/files"
    if len(file) > 1:
        message = f"已上传 {len(results)} 个文件。" + (f" {len(errors)} 个未完成：" + "；".join(item["filename"] + "：" + item["message"] for item in errors) if errors else "")
        if wants_json(request):
            return JSONResponse({"files": results, "errors": errors, "message": message, "redirect_url": destination})
        return RedirectResponse(destination + "?" + urlencode({"message": message}), status_code=303)
    if wants_json(request):
        result["redirect_url"] = f"/admin/files/{result['file_id']}" if user and user["role"] == "admin" else "/images" if purpose == "image" else "/"
        return JSONResponse(result)
    return RedirectResponse("/images/" + result["file_id"] if purpose == "image" else "/files/" + result["file_id"], status_code=303)



async def save_web_batch(request, uploads, user, visibility, ttl_seconds, storage_policy, purpose, generate_link):
    if not uploads or len(uploads) > 20:
        raise HTTPException(422, detail={"error": "invalid_batch", "message": "每次请选择 1 至 20 个文件。"})
    results, errors = [], []
    for upload in uploads:
        try:
            result = await save_upload(upload, user["id"], visibility, ttl_seconds, storage_policy, purpose, generate_link=generate_link)
            results.append(result)
            try:
                audit("upload_file", request=request, user=user, target_type="file", target_id=result["file_id"], detail=purpose)
            except Exception as exc:
                logger.error('Upload audit unavailable (%s), file already saved: %s', type(exc).__name__, result['file_id'])
        except HTTPException as exc:
            if len(uploads) == 1:
                raise
            errors.append({"filename": (upload.filename or "文件")[:180], "message": exc.detail.get("message", "上传未完成。") if isinstance(exc.detail, dict) else str(exc.detail)})
        except Exception as exc:
            logger.error('File upload failed (%s); preserving completed batch results', type(exc).__name__)
            if len(uploads) == 1:
                raise HTTPException(503, detail={"error": "upload_unavailable", "message": "上传暂未完成，请稍后重试。"}) from exc
            errors.append({"filename": (upload.filename or "文件")[:180], "message": "上传暂未完成，请稍后重试。"})
        finally:
            await upload.close()
    if not results:
        raise HTTPException(422, detail={"error": "upload_failed", "message": "所有文件均未完成上传。" + "；".join(item["filename"] + "：" + item["message"] for item in errors)})
    return results, errors



@router.get("/files", response_class=HTMLResponse)
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
    return render_template("files.html", {"request": request, "user": user, "files": rows, "filters": filters, "page": page, "page_size": page_size, "total": total, "has_prev": page > 1, "has_next": offset + len(rows) < total})



@router.get("/files/{file_id}", response_class=HTMLResponse)
def file_detail(file_id: str, request: Request, user=Depends(require_user)):
    with connect() as conn:
        row = conn.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
    if not row or not can_manage_file(row, user):
        raise HTTPException(status_code=404, detail="File not found")
    return render_template("file_detail.html", {"request": request, "user": user, "file": row})



@router.post("/files/{file_id}/tags")
def file_tags_update(file_id: str, request: Request, tags: str = Form(""), user=Depends(require_user)):
    update_file_tags(file_id, tags, user)
    audit("update_tags", request=request, user=user, target_type="file", target_id=file_id)
    return RedirectResponse(request.headers.get("referer") or f"/files/{file_id}", status_code=303)



@router.post("/files/{file_id}/delete")
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



@router.post("/files/bulk/delete")
def files_bulk_delete(request: Request, file_ids: list[str] = Form([]), user=Depends(require_user)):
    rows = selected_manageable_files(file_ids, user)
    for row in rows:
        delete_file(row["file_id"])
        audit("delete_file", request=request, user=user, target_type="file", target_id=row["file_id"], detail="bulk")
    return RedirectResponse(request.headers.get("referer") or "/files", status_code=303)



@router.post("/files/bulk/revoke-links")
def files_bulk_revoke_links(request: Request, file_ids: list[str] = Form([]), user=Depends(require_user)):
    rows = selected_manageable_files(file_ids, user)
    for row in rows:
        if int(row["link_enabled"] or 0) == 1:
            revoke_direct_link(row["file_id"], user)
            audit("revoke_link", request=request, user=user, target_type="file", target_id=row["file_id"], detail="bulk")
    return RedirectResponse(request.headers.get("referer") or "/files", status_code=303)



@router.get("/files/cleanup/test-files", response_class=HTMLResponse)
def files_cleanup_test_confirm(request: Request, user=Depends(require_admin)):
    names = ("hello.txt", "policy.txt", "luomofile-test.txt")
    with connect() as conn:
        rows = conn.execute("SELECT * FROM files WHERE original_filename IN (?, ?, ?) ORDER BY uploaded_at DESC", names).fetchall()
    return render_template("files_cleanup_confirm.html", {"request": request, "user": user, "files": rows})



@router.post("/files/cleanup/test-files")
def files_cleanup_test_run(request: Request, user=Depends(require_admin)):
    names = ("hello.txt", "policy.txt", "luomofile-test.txt")
    with connect() as conn:
        rows = conn.execute("SELECT file_id FROM files WHERE original_filename IN (?, ?, ?) AND status!='deleted'", names).fetchall()
    for row in rows:
        delete_file(row["file_id"])
        audit("delete_file", request=request, user=user, target_type="file", target_id=row["file_id"], detail="clean_test_files")
    return RedirectResponse("/files?scope=all", status_code=303)



@router.post("/files/{file_id}/link/generate")
def file_link_generate(file_id: str, request: Request, return_to: str = Form(""), user=Depends(require_user)):
    generate_direct_link(file_id, user)
    audit("generate_link", request=request, user=user, target_type="file", target_id=file_id)
    target = return_to if return_to in {"/files", "/admin/files", "/images", f"/files/{file_id}", f"/admin/files/{file_id}", f"/images/{file_id}"} else f"/files/{file_id}"
    return RedirectResponse(target, status_code=303)



@router.post("/files/{file_id}/link/revoke")
def file_link_revoke(file_id: str, request: Request, return_to: str = Form(""), user=Depends(require_user)):
    revoke_direct_link(file_id, user)
    audit("revoke_link", request=request, user=user, target_type="file", target_id=file_id)
    target = return_to if return_to in {"/files", "/admin/files", "/images", f"/files/{file_id}", f"/admin/files/{file_id}", f"/images/{file_id}"} else f"/files/{file_id}"
    return RedirectResponse(target, status_code=303)



