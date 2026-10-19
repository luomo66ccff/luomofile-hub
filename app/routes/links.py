"""Links HTTP handlers; behavior preserved from gallery-t001."""
from fastapi import Depends, HTTPException, Request
from fastapi.responses import Response
from app.auth import current_user
from app.files import serve_file, serve_protected_subscription
from app.subscriptions import record_subscription_token_use, resolve_subscription_token
from app.subscription_identity import derive_subscription_identity
from app.subscription_formats import ConversionError, parse_format_options
from fastapi import APIRouter
from app.web.deps import templates, render_template

router = APIRouter()


@router.get("/f/{file_id}/{filename}")
@router.head("/f/{file_id}/{filename}")
def public_file(file_id: str, filename: str, request: Request, user=Depends(current_user)):
    return serve_file(file_id, request, filename, user=user)



@router.get("/i/{file_id}")
@router.head("/i/{file_id}")
def image_file(file_id: str, request: Request, user=Depends(current_user)):
    return serve_file(file_id, request, user=user)



@router.get("/t/{file_id}")
@router.head("/t/{file_id}")
def temp_file(file_id: str, request: Request, user=Depends(current_user)):
    return serve_file(file_id, request, user=user)



def protected_subscription_response(token: str, request: Request) -> Response:
    grant = resolve_subscription_token(token, record_use=False)
    if not grant:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Subscription not found."})
    try:
        subscription_format, include_private = parse_format_options(request.query_params)
    except ConversionError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_subscription_format", "message": "Choose format=mihomo, clash, uri, base64 or sing-box; include_private must be 0 or 1."}) from exc
    if request.method == "GET":
        record_subscription_token_use(grant["subscription_token_id"])
    return serve_protected_subscription(
        grant["file_id"],
        request,
        derive_subscription_identity(grant["token_hash"]),
        subscription_format=subscription_format,
        include_private=include_private,
    )



@router.get("/sub/{token}")
def protected_subscription(token: str, request: Request):
    return protected_subscription_response(token, request)



@router.head("/sub/{token}")
def protected_subscription_head(token: str, request: Request):
    return protected_subscription_response(token, request)



