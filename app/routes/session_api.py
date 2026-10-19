"""Bounded, session-only search. No link keys, storage paths or other owners' data."""
from collections import OrderedDict, deque
from threading import Lock
from time import monotonic

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from app.auth import require_user
from app.db import connect

router = APIRouter()
_requests = OrderedDict()
_lock = Lock()

@router.get('/api/session/search')
def session_search(q: str = Query('', max_length=100), user=Depends(require_user)):
    now = monotonic()
    with _lock:
        history = _requests.setdefault(user['id'], deque())
        while history and history[0] < now - 60: history.popleft()
        if len(history) >= 40:
            return JSONResponse({'error': 'rate_limited', 'message': '请稍后再搜索。'}, status_code=429, headers={'Retry-After': '60', 'Cache-Control': 'private, no-store'})
        history.append(now)
        _requests.move_to_end(user['id'])
        while len(_requests) > 4096: _requests.popitem(last=False)
    query = q.strip()
    if not query: return JSONResponse({'files': [], 'albums': []}, headers={'Cache-Control': 'private, no-store'})
    pattern = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
    with connect() as conn:
        files = conn.execute("SELECT file_id,original_filename,purpose,size_bytes FROM files WHERE owner_id=? AND status='active' AND original_filename LIKE ? ESCAPE '\\' ORDER BY uploaded_at DESC,id DESC LIMIT 8", (user['id'], pattern)).fetchall()
        albums = conn.execute("SELECT album_id,title FROM albums WHERE owner_id=? AND status='active' AND title LIKE ? ESCAPE '\\' ORDER BY updated_at DESC LIMIT 8", (user['id'], pattern)).fetchall()
    return JSONResponse({'files': [{'title': r['original_filename'], 'size_bytes': r['size_bytes'], 'url': ('/images/' if r['purpose'] == 'image' else '/files/') + r['file_id']} for r in files],
                         'albums': [{'title': r['title'], 'url': '/albums/' + r['album_id']} for r in albums]}, headers={'Cache-Control': 'private, no-store'})
