"""Temp Share HTTP handlers; behavior preserved from gallery-t001."""
from urllib.parse import urlencode
from fastapi import Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from app.auth import current_user
from app.config import settings
from app.web.deps import wants_json, ensure_upload_user
from app.routes.files import save_web_batch
from fastapi import APIRouter
from app.web.deps import templates, render_template

router = APIRouter()


@router.get("/temp", response_class=HTMLResponse)
def temp_page(request: Request, user=Depends(current_user)):
    return render_template("upload.html", {"request": request, "user": user, "mode": "temp", "anonymous_upload": settings.allow_anonymous_upload})



@router.head("/temp")
def temp_head() -> Response:
    return Response(status_code=200, media_type="text/html")



@router.post("/temp")
async def temp_upload(request: Request, file: list[UploadFile] = File(...), ttl_seconds: int = Form(86400), storage_policy: str = Form("auto"), generate_link: bool = Form(False), user=Depends(current_user)):
    ensure_upload_user(user)
    results, errors = await save_web_batch(request, file, user, "unlisted", ttl_seconds, storage_policy, "temp", generate_link)
    result = results[0]
    if len(file) > 1:
        message = f"已创建 {len(results)} 个临时文件。" + (f" {len(errors)} 个未完成。" if errors else "")
        if wants_json(request):
            return JSONResponse({"files": results, "errors": errors, "message": message, "redirect_url": "/files?purpose=temp"})
        return RedirectResponse("/files?purpose=temp&" + urlencode({"message": message}), status_code=303)
    if wants_json(request):
        result["redirect_url"] = result["public_url"] or "/"
        return JSONResponse(result)
    return RedirectResponse(result["public_url"] or "/", status_code=303)



