"""Admin HTTP handlers; behavior preserved from gallery-t001."""
from datetime import datetime, timezone
from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from app.audit import audit
from app.auth import generate_api_key, require_admin
from app.cleanup import cleanup_once
from app.db import connect
from app.files import delete_file
from app.security import hash_token, now_iso
from app.subscriptions import SubscriptionCreateConflict, enqueue_subscription_link, list_subscription_links, prepare_subscription_create, reconcile_subscription_identities, revoke_subscription_link, start_subscription_create_worker, subscription_files, subscription_identity_health
from app.subscription_formats import FORMAT_OPTIONS
from app.web.deps import wants_json
from app.routes.public import health
from fastapi import APIRouter
from app.web.deps import templates, render_template

router = APIRouter()


@router.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, user=Depends(require_admin)):
    with connect() as conn:
        files = conn.execute("SELECT * FROM files ORDER BY uploaded_at DESC LIMIT 10").fetchall()
        backends = conn.execute("SELECT * FROM storage_backends ORDER BY priority").fetchall()
        jobs = conn.execute("SELECT * FROM cleanup_jobs ORDER BY started_at DESC LIMIT 5").fetchall()
    return render_template("admin.html", {"request": request, "user": user, "files": files, "backends": backends, "jobs": jobs})



@router.head("/admin")
def admin_head(user=Depends(require_admin)) -> Response:
    return Response(status_code=200, media_type="text/html")



def subscription_management_links() -> list[dict]:
    links = list_subscription_links(include_display_urls=True)
    with connect() as conn:
        requests = conn.execute(
            "SELECT token_id, status FROM subscription_create_requests WHERE token_id IS NOT NULL"
        ).fetchall()
    task_states = {row["token_id"]: row["status"] for row in requests}
    for link in links:
        link["create_status"] = task_states.get(link["id"])
        status = link["status"]
        if status == "active" and link["expires_at"] and datetime.fromisoformat(link["expires_at"]) <= datetime.now(timezone.utc):
            status = "expired"
        if status == "provisioning":
            status = "processing" if link["create_status"] == "processing" else "queued"
        link["display_status"] = status
        link["status_label"] = {
            "active": "可用", "queued": "排队中", "processing": "同步中", "provision_failed": "创建失败",
            "revoked": "已吊销", "revoke_pending": "吊销同步中", "expired": "已过期", "expired_pending": "过期清理中",
        }.get(status, "待确认")
    return links



def subscription_management_counts(links: list[dict]) -> dict:
    return {
        "total": len(links),
        "active": sum(link["display_status"] == "active" for link in links),
        "pending": sum(link["display_status"] in {"queued", "processing"} for link in links),
        "failed": sum(link["display_status"] == "provision_failed" for link in links),
    }



def subscription_management_error(exc: Exception) -> str:
    if isinstance(exc, SubscriptionCreateConflict):
        if "different parameters" in str(exc):
            return "这次请求已经提交过，参数不能更改。请先查看原请求结果，再开始新的创建。"
        return "创建请求已失效。请重新打开订阅管理页后再试。"
    message = str(exc)
    if "label is required" in message:
        return "请填写接收方标签。"
    if "Expiry days" in message:
        return "有效期需为 0 到 3650 天；0 表示不过期。"
    if "does not exist" in message:
        return "所选订阅文件已不可用，请选择现有文件。"
    if "server-mounted" in message:
        return "此文件所在存储暂不支持受保护订阅，请选择服务器可读取的文件。"
    return "暂时无法创建链接。请检查订阅文件后重试。"



def render_admin_subscriptions(request: Request, user, created_url: str | None = None, error: str = "", status_code: int = 200,
                               form_values: dict | None = None, create_id: str | None = None, notice: str = ""):
    links = subscription_management_links()
    selected_format = request.query_params.get("format", "mihomo")
    if selected_format not in FORMAT_OPTIONS:
        selected_format = "mihomo"
    notices = {
        "revoked": "链接已吊销，保存的副本已清理。",
        "revoke_pending": "链接已立即关闭；节点上的凭据正在继续清理。",
        "synced": "节点同步已完成。",
    }
    response = render_template(
        "admin_subscriptions.html",
        {
            "request": request,
            "user": user,
            "subscription_files": subscription_files(),
            "links": links,
            "counts": subscription_management_counts(links),
            "node_health": subscription_identity_health(),
            "created_url": created_url,
            "subscription_create_id": create_id or prepare_subscription_create(user["id"]),
            "subscription_formats": FORMAT_OPTIONS,
            "selected_format": selected_format,
            "form_values": form_values or {},
            "notice": notice or notices.get(request.query_params.get("notice"), ""),
            "error": error,
        },
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response



@router.get("/admin/subscriptions", response_class=HTMLResponse)
def admin_subscriptions(request: Request, user=Depends(require_admin)):
    return render_admin_subscriptions(request, user)



@router.get("/admin/subscriptions/status")
def admin_subscriptions_status(request: Request, user=Depends(require_admin)):
    links = subscription_management_links()
    selected_format = request.query_params.get("format", "mihomo")
    if selected_format not in FORMAT_OPTIONS:
        selected_format = "mihomo"
    counts = subscription_management_counts(links)
    health = subscription_identity_health()
    module = templates.get_template("pages/admin_subscriptions.html").make_module({
        "request": request, "user": user, "links": links, "counts": counts, "node_health": health,
        "form_values": {}, "subscription_files": [], "subscription_formats": FORMAT_OPTIONS, "selected_format": selected_format,
    })
    return JSONResponse(
        {
            "html": str(module.link_list(links, FORMAT_OPTIONS, selected_format)),
            "counts": counts,
            "tasks": [{"id": link["id"], "status": link["display_status"]} for link in links],
            "node_health": health,
        },
        headers={"Cache-Control": "private, no-store, max-age=0", "Pragma": "no-cache"},
    )



@router.head("/admin/subscriptions")
def admin_subscriptions_head(user=Depends(require_admin)) -> Response:
    return Response(status_code=200, media_type="text/html", headers={"Cache-Control": "private, no-store, max-age=0"})



@router.post("/admin/subscriptions/create", response_class=HTMLResponse)
def admin_subscription_create(
    request: Request,
    file_id: str = Form(...),
    label: str = Form(...),
    expires_in_days: int = Form(30),
    idempotency_key: str = Form(...),
    user=Depends(require_admin),
):
    try:
        created = enqueue_subscription_link(file_id, label, user["id"], expires_in_days, idempotency_key)
    except SubscriptionCreateConflict as exc:
        message = subscription_management_error(exc)
        if wants_json(request):
            return JSONResponse({"error": "create_conflict", "message": message}, status_code=409)
        return render_admin_subscriptions(request, user, error=message, status_code=409,
                                          form_values={"file_id": file_id, "label": label, "expires_in_days": expires_in_days})
    except ValueError as exc:
        message = subscription_management_error(exc)
        if wants_json(request):
            return JSONResponse({"error": "invalid_subscription", "message": message}, status_code=400)
        return render_admin_subscriptions(request, user, error=message, status_code=400, create_id=idempotency_key,
                                          form_values={"file_id": file_id, "label": label, "expires_in_days": expires_in_days})
    if created["newly_enqueued"]:
        audit(
            "create_subscription_link",
            request=request,
            user=user,
            target_type="subscription_token",
            target_id=str(created["id"]),
            detail=f"queued:{created['label']}",
        )
    start_subscription_create_worker()
    if wants_json(request):
        return JSONResponse({"accepted": True, "request": {"id": created["id"], "status": created["status"]}}, status_code=202)
    return RedirectResponse("/admin/subscriptions", status_code=303)



@router.post("/admin/subscriptions/{token_id}/revoke")
def admin_subscription_revoke(token_id: int, request: Request, user=Depends(require_admin)):
    result = revoke_subscription_link(token_id)
    audit(
        "revoke_subscription_link",
        request=request,
        user=user,
        target_type="subscription_token",
        target_id=str(token_id),
        detail=result,
    )
    if wants_json(request):
        return JSONResponse({"status": result, "message": "链接已关闭。"})
    notice = "revoke_pending" if result == "revoke_pending" else "revoked"
    return RedirectResponse(f"/admin/subscriptions?notice={notice}", status_code=303)



@router.post("/admin/subscriptions/reconcile")
def admin_subscription_reconcile(request: Request, user=Depends(require_admin)):
    try:
        result = reconcile_subscription_identities()
        detail = f"active={result['active']},revoked={result['revoked']},expired={result['expired']}"
    except Exception as exc:
        detail = f"failed:{type(exc).__name__}"
    audit(
        "reconcile_subscription_identities",
        request=request,
        user=user,
        target_type="subscription_tokens",
        target_id="all",
        detail=detail,
    )
    if detail.startswith("failed:"):
        message = "节点同步暂未完成，链接状态已保留。请稍后重试。"
        if wants_json(request):
            return JSONResponse({"error": "sync_failed", "message": message}, status_code=503)
        return render_admin_subscriptions(request, user, error=message, status_code=503)
    if wants_json(request):
        return JSONResponse({"message": "节点同步已完成。"})
    return RedirectResponse("/admin/subscriptions?notice=synced", status_code=303)



@router.get("/admin/files", response_class=HTMLResponse)
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

    return render_template("admin_files.html", {
        "request": request, "user": user, "files": files,
        "q": q, "type": type, "visibility": visibility,
        "time": time, "size": size, "sort": sort,
        "page": page, "page_size": page_size,
        "total": total, "total_pages": total_pages,
        "query_string": query_string,
    })



@router.get("/admin/files/{file_id}", response_class=HTMLResponse)
def admin_file_detail(file_id: str, request: Request, user=Depends(require_admin)):
    with connect() as conn:
        row = conn.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="File not found")
    return render_template("admin_file_detail.html", {"request": request, "user": user, "file": row})



@router.post("/admin/files/{file_id}/delete")
def admin_file_delete(file_id: str, request: Request, user=Depends(require_admin)):
    delete_file(file_id)
    audit("delete_file", request=request, user=user, target_type="file", target_id=file_id)
    return RedirectResponse("/admin/files", status_code=303)



@router.post("/admin/files/{file_id}/visibility")
def admin_file_visibility(file_id: str, request: Request, visibility: str = Form("unlisted"), user=Depends(require_admin)):
    with connect() as conn:
        conn.execute("UPDATE files SET visibility=?, updated_at=? WHERE file_id=?", (visibility, now_iso(), file_id))
        conn.commit()
    audit("change_visibility", request=request, user=user, target_type="file", target_id=file_id, detail=visibility)
    return RedirectResponse(f"/admin/files/{file_id}", status_code=303)



@router.get("/admin/storage", response_class=HTMLResponse)
def admin_storage(request: Request, user=Depends(require_admin)):
    with connect() as conn:
        rows = conn.execute("SELECT * FROM storage_backends ORDER BY priority").fetchall()
    return render_template("admin_storage.html", {"request": request, "user": user, "backends": rows})



@router.post("/admin/cleanup/run")
def admin_cleanup_run(request: Request, user=Depends(require_admin)):
    cleanup_once()
    audit("cleanup_run", request=request, user=user, target_type="cleanup", target_id="manual")
    return RedirectResponse("/admin", status_code=303)



@router.get("/admin/api-keys", response_class=HTMLResponse)
def admin_api_keys(request: Request, created_key: str | None = None, user=Depends(require_admin)):
    with connect() as conn:
        keys = conn.execute("SELECT k.*, u.username FROM api_keys k JOIN users u ON u.id=k.user_id ORDER BY k.created_at DESC").fetchall()
        users = conn.execute("SELECT * FROM users WHERE status='active' ORDER BY username").fetchall()
    return render_template("admin_api_keys.html", {"request": request, "user": user, "keys": keys, "users": users, "created_key": created_key})



@router.post("/admin/api-keys/create")
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



@router.post("/admin/api-keys/{key_id}/revoke")
def admin_api_key_revoke(key_id: int, request: Request, user=Depends(require_admin)):
    with connect() as conn:
        conn.execute("UPDATE api_keys SET status='revoked', revoked_at=? WHERE id=?", (now_iso(), key_id))
        conn.commit()
    audit("revoke_api_key", request=request, user=user, target_type="api_key", target_id=str(key_id))
    return RedirectResponse("/admin/api-keys", status_code=303)




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

