from fastapi import Request
from fastapi.responses import RedirectResponse

async def error_redirect_middleware(request: Request, call_next):
    redirect_codes = {403, 404, 500, 502, 503, 504}
    try:
        response = await call_next(request)
        if response.status_code in redirect_codes:
            source = request.headers.get("host", "-")
            path = request.url.path
            ip = request.client.host if request.client else "-"
            ray = request.headers.get("cf-ray", "-")
            url = f"https://error.luomo.moe/{response.status_code}?status={response.status_code}&source={source}&path={path}&ip={ip}&ray={ray}"
            return RedirectResponse(url=url, status_code=302)
        return response
    except Exception:
        source = request.headers.get("host", "-")
        path = request.url.path
        ip = request.client.host if request.client else "-"
        url = "https://error.luomo.moe/500?status=500&source=" + source + "&path=" + path + "&ip=" + ip + "&ray=app-exception"
        return RedirectResponse(url=url, status_code=302)
