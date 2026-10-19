"""Keep errors on site without exporting private paths, invitation IDs or IPs."""
import logging

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from app.config import settings
from app.presentation import error_response, is_html


async def error_redirect_middleware(request: Request, call_next):
    subscription = request.url.path.startswith('/sub/')
    management = request.url.path == '/admin/subscriptions' or request.url.path.startswith('/admin/subscriptions/')
    try:
        response = await call_next(request)
        if management:
            response.headers['Cache-Control'] = 'private, no-store, max-age=0'
        return response
    except Exception as exc:
        if subscription and settings.app_env != 'production':
            raise
        logging.getLogger(__name__).error('Request failed (%s); response kept on site', type(exc).__name__)
        if subscription:
            return Response(status_code=500, media_type='text/plain', headers={'Cache-Control': 'private, no-store'})
        message = '操作结果暂时无法确认，请刷新查看当前状态。'
        if management:
            message = '暂时无法确认操作结果，请先查看订阅创建进度，避免重复签发。'
        if is_html(request):
            return error_response(request, 500, message=message,
                                  return_to='/admin/subscriptions' if management else None,
                                  return_label='返回订阅管理' if management else None)
        return JSONResponse({'error': 'subscription_operation_failed' if management else 'request_failed', 'message': message}, status_code=500,
                            headers={'Cache-Control': 'private, no-store'})
