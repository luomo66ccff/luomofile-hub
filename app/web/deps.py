"""Deps HTTP handlers; behavior preserved from gallery-t001."""
import hmac
from fastapi import HTTPException, Request
from app.config import settings
from app.presentation import make_templates
from app.security import form_csrf_token
from app.auth import current_user

templates = make_templates()

def render(request, name, **ctx):
    """One presentation entry, also usable by the compatibility response adapter."""
    status_code = ctx.pop('_status_code', 200)
    headers = ctx.pop('_headers', None)
    if not name.startswith(('pages/', 'layouts/', 'partials/', 'macros/')):
        name = 'pages/' + name
    user = ctx.pop('user', None)
    context = dict(user=user, csrf_token=form_csrf_token(request),
                   asset_version=settings.app_version, nav=request.url.path.split('/')[1],
                   message=request.query_params.get('message', ''), error='')
    context.update(ctx)
    context['request'] = request
    return templates.TemplateResponse(name, context, status_code=status_code, headers=headers)

def render_template(name, context, status_code=200, headers=None):
    context = dict(context)
    return render(context.pop('request'), name, _status_code=status_code, _headers=headers, **context)



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



