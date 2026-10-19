"""Developer HTTP handlers; behavior preserved from gallery-t001."""
from fastapi import Depends, Request
from fastapi.responses import HTMLResponse
from app.auth import require_user
from app.config import settings
from fastapi import APIRouter
from app.web.deps import templates, render_template

router = APIRouter()


@router.get("/developer", response_class=HTMLResponse)
def developer_home(request: Request, user=Depends(require_user)):
    return render_template("developer.html", {"request": request, "user": user})



@router.get("/developer/docs", response_class=HTMLResponse)
def developer_docs(request: Request, user=Depends(require_user)):
    return render_template("developer_docs.html", {"request": request, "user": user, "base_url": settings.app_base_url})



