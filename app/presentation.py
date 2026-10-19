"""Shared presentation helpers; no private storage information in page contexts."""
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

from fastapi import Request
from fastapi.templating import Jinja2Templates


def human_size(value):
    size = max(0, int(value or 0))
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return f'{size:.1f} {unit}' if unit != 'B' else f'{int(size)} B'
        size /= 1024


def human_date(value):
    if not value:
        return '—'
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
    except (ValueError, TypeError):
        return str(value)


def purpose_label(value):
    return {'image': '图片', 'temp': '临时分享', 'api': 'API 文件', 'backup': '备份', 'other': '文件'}.get(value, value or '文件')


def visibility_label(value):
    return {'private': '仅自己', 'unlisted': '允许链接分享', 'public': '允许公开链接',
            'restricted': '指定成员', 'authenticated': '登录可见'}.get(value, value or '—')


def status_label(value):
    return {'active': '正常', 'pending': '待确认', 'deleted': '已删除', 'disabled': '已停用',
            'blocked': '已阻止', 'revoked': '已撤销', 'accepted': '已通过', 'rejected': '未通过',
            'withdrawn': '已撤回', 'processing': '处理中', 'ready': '已就绪', 'failed': '处理失败',
            'declined': '已拒绝', 'archived': '已归档', 'expired': '已过期'}.get(value, value or '—')


def _csrf_context(request: Request):
    from app.security import form_csrf_token
    return {'csrf_token': form_csrf_token(request)}


def make_templates():
    templates = Jinja2Templates(directory='app/web/templates', context_processors=[_csrf_context])
    templates.env.globals.update(human_size=human_size, human_date=human_date,
                                 purpose_label=purpose_label, visibility_label=visibility_label,
                                 status_label=status_label)
    return templates


def is_html(request: Request):
    accept = request.headers.get('accept', '')
    return ('text/html' in accept and 'application/json' not in accept
            and request.headers.get('x-requested-with') != 'XMLHttpRequest'
            and not request.url.path.startswith(('/api/', '/media/', '/i/', '/f/', '/t/', '/sub/')))


def safe_next(value: str | None, default='/albums'):
    value = str(value or '')
    if not value.startswith('/') or value.startswith('//') or '\\' in value or any(ord(c) < 32 for c in value):
        return default
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.path in {'/login', '/logout', '/register'}:
        return default
    return value


def login_location(request: Request):
    # Never carry secret query parameters into the login URL.
    path = safe_next(request.url.path)
    return '/login?next=' + quote(path, safe='')


def pagination(result):
    total = int(result.get('total', 0))
    size = max(1, int(result.get('page_size', 24)))
    return {'page': int(result.get('page', 1)), 'page_size': size,
            'pages': max(1, (total + size - 1) // size), 'total': total,
            'has_prev': bool(result.get('has_prev')), 'has_next': bool(result.get('has_next'))}


def error_response(request, status_code, *, user=None, message='', return_to=None, return_label=None):
    from app.web.deps import render_template
    title = {400: '请求需要调整', 401: '请先登录', 403: '暂时无法访问', 404: '这里没有内容',
             409: '内容状态已变化', 413: '文件超过大小限制', 422: '请检查填写内容',
             429: '操作有点频繁', 500: '暂时遇到问题', 503: '服务正在恢复'}.get(status_code, '操作未完成')
    message = message or {403: '当前账户没有这项操作的权限。', 404: '内容可能已移除，或者没有向你开放。',
                          409: '请刷新页面确认最新状态后再试。', 500: '操作结果暂时无法确认，请先查看当前状态。'}.get(status_code, '请检查当前内容后重试。')
    return render_template('error.html', {
        'request': request, 'user': user, 'status_code': status_code, 'title': title,
        'message': message, 'return_to': return_to or ('/albums' if user else '/gallery'),
        'return_label': return_label or ('返回我的相册' if user else '浏览公共相册'),
    }, status_code=status_code, headers={'Cache-Control': 'private, no-store', 'X-Robots-Tag': 'noindex'})
