import asyncio
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.auth import current_user
from app.cleanup import cleanup_once
from app.config import ensure_dirs, settings
from app.db import init_db
from app.security import security_headers
from app.subscriptions import reconcile_subscription_identities, start_subscription_create_worker
from app.error_redirect import error_redirect_middleware
from app.presentation import error_response, is_html, login_location

app = FastAPI(title=settings.app_name, docs_url=None, redoc_url=None)
from app.web.deps import wants_json
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")

app.middleware("http")(error_redirect_middleware)
app.middleware("http")(security_headers)
logger = logging.getLogger("luomofile.mail")
VERIFICATION_REQUEST_MESSAGE = (
    "如果填写的信息可用于注册，我们会请求发送验证邮件。请检查收件箱和垃圾邮件；"
    "邮件未送达时，可以在这里重新请求验证链接。"
)

@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: HTTPException):
    if is_html(request):
        if exc.status_code == 401:
            return RedirectResponse(login_location(request), status_code=303, headers={"Cache-Control": "private, no-store"})
        user = await asyncio.to_thread(current_user, request)
        detail = exc.detail.get("message", "") if isinstance(exc.detail, dict) else str(exc.detail)
        if detail in {"Not Found", "Image not found", "File not found"}:
            detail = "内容不存在、已移除，或者没有向你开放。"
        if detail in {"Email verification required", "Admin required"}:
            detail = "请先验证邮箱。" if detail.startswith("Email") else "此页面仅向管理员开放。"
        return error_response(request, exc.status_code, user=user, message=detail)
    if isinstance(exc.detail, dict) and (request.url.path.startswith("/api/") or wants_json(request)):
        return JSONResponse(exc.detail, status_code=exc.status_code)
    return JSONResponse({"error": "request_failed", "message": str(exc.detail)}, status_code=exc.status_code)

@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    if is_html(request):
        user = await asyncio.to_thread(current_user, request)
        return error_response(request, 422, user=user, message="请检查必填内容和输入格式后重新提交。")
    return JSONResponse({"error": "invalid_input", "message": "请检查必填内容和输入格式。"}, status_code=422)

@app.on_event("startup")
async def startup() -> None:
    ensure_dirs()
    if not settings.session_secret or len(settings.session_secret) < 32:
        raise RuntimeError("SESSION_SECRET must contain at least 32 characters.")
    init_db()
    start_subscription_create_worker()
    asyncio.create_task(cleanup_loop())
    if settings.subscription_identity_sync_enabled:
        asyncio.create_task(subscription_identity_reconcile_loop())

async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(settings.cleanup_interval_seconds)
        try:
            await asyncio.to_thread(cleanup_once)
        except Exception:
            pass

async def subscription_identity_reconcile_loop() -> None:
    await asyncio.sleep(30)
    while True:
        try:
            await asyncio.to_thread(reconcile_subscription_identities)
        except Exception as exc:
            logger.warning("subscription_identity_reconcile_failed error_type=%s", type(exc).__name__)
        await asyncio.sleep(300)

# Album media uses independent authorization; original file-link contracts remain intact.

# Registration is explicit; compatibility imports have no application side effects.
from app.routes.public import router as public_router
app.include_router(public_router)
from app.routes.auth import router as auth_router
app.include_router(auth_router)
from app.routes.files import router as files_router
app.include_router(files_router)
from app.routes.temp_share import router as temp_share_router
app.include_router(temp_share_router)
from app.routes.links import router as links_router
app.include_router(links_router)
from app.routes.api_v1 import router as api_v1_router
app.include_router(api_v1_router)
from app.routes.internal_api import router as internal_api_router
app.include_router(internal_api_router)
from app.routes.admin import router as admin_router
app.include_router(admin_router)
from app.routes.developer import router as developer_router
app.include_router(developer_router)
from app.routes.gallery import router as gallery_router
app.include_router(gallery_router)
from app.routes.session_api import router as session_router
app.include_router(session_router)

# Preserve historic script imports while implementations live in their route modules.
from app.routes.public import favicon, health, health_head, ready, public_status, public_version, public_storage, home, home_head
from app.routes.auth import is_valid_email, diagnostic_request_id, create_email_verification, activate_email_verification, claim_verification_email, login_page, login_head, login_submit, logout, register_page, register_submit, verify_email, verify_email_confirm, resend_verification
from app.routes.files import upload_page, upload_head, upload_form, save_web_batch, files_page, file_detail, file_tags_update, file_delete, selected_manageable_files, files_bulk_delete, files_bulk_revoke_links, files_cleanup_test_confirm, files_cleanup_test_run, file_link_generate, file_link_revoke
from app.routes.temp_share import temp_page, temp_head, temp_upload
from app.routes.links import public_file, image_file, temp_file, protected_subscription_response, protected_subscription, protected_subscription_head
from app.routes.api_v1 import api_upload, api_temp_upload, api_image_upload, api_file_link_generate, api_file_link_revoke, api_files, api_file_detail, api_file_delete, api_storage_stats
from app.routes.internal_api import api_internal_upload, api_internal_files, api_internal_file_detail, api_internal_file_delete, api_internal_storage_stats, api_internal_metrics
from app.routes.admin import admin_home, admin_head, subscription_management_links, subscription_management_counts, subscription_management_error, render_admin_subscriptions, admin_subscriptions, admin_subscriptions_status, admin_subscriptions_head, admin_subscription_create, admin_subscription_revoke, admin_subscription_reconcile, admin_files, admin_file_detail, admin_file_delete, admin_file_visibility, admin_storage, admin_cleanup_run, admin_api_keys, admin_api_key_create, admin_api_key_revoke
from app.routes.developer import developer_home, developer_docs
