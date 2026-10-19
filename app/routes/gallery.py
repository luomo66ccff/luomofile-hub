"""Album pages and session-authenticated API, separate from original file links."""
import asyncio
import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from app import album_service as albums
from app.auth import current_user, require_user
from app.db import connect
from app.files import can_manage_file, save_upload
from app.image_media import image_response, original_response, prepare_image
from app.presentation import pagination
from app.web.deps import templates, render_template

router = APIRouter()
logger = logging.getLogger('luomofile.gallery.upload')


def json_request(request):
    return request.url.path.startswith('/api/') or request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.headers.get('accept', '')


def normalize_photo(value):
    photo = dict(value)
    photo.setdefault('original_filename', photo.get('filename', '图片'))
    photo.setdefault('title', photo['original_filename'])
    photo.setdefault('asset_status', photo.get('media_status', 'pending'))
    photo.setdefault('album_count', len(photo.get('associations', [])))
    photo.setdefault('added_at', photo.get('created_at') or photo.get('uploaded_at'))
    photo.setdefault('thumb_url', '/media/images/' + photo['file_id'] + '/thumb')
    photo.setdefault('preview_url', '/media/images/' + photo['file_id'] + '/preview')
    return photo


def page_response(name, request, user, **context):
    base = {'request': request, 'user': user, 'message': request.query_params.get('message', ''), 'error': ''}
    base.update(context)
    return render_template(name, base, headers={'Cache-Control': 'private, no-store'})


def done(request, result, destination, message='操作已保存。'):
    if json_request(request):
        return JSONResponse({'ok': True, 'result': result, 'redirect_url': destination, 'message': message})
    return RedirectResponse(destination + ('&' if '?' in destination else '?') + urlencode({'message': message}), status_code=303)


def consent_required(consent):
    if not consent:
        raise HTTPException(422, detail={'error': 'consent_required', 'message': '请确认图片在相册中的展示范围后提交。'})


@router.get('/gallery', response_class=HTMLResponse)
def gallery(request: Request, q: str = '', kind: str = '', page: int = 1, user=Depends(current_user)):
    result = albums.list_albums(user, scope='public', query=q, kind=kind or None, page=page)
    return page_response('gallery.html', request, user, albums=result['albums'], pagination=pagination(result), filters={'q': q, 'kind': kind, 'scope': 'public'})


@router.get('/albums', response_class=HTMLResponse)
def album_list(request: Request, scope: str = 'mine', q: str = '', page: int = 1, user=Depends(require_user)):
    scope = scope if scope in {'mine', 'joined'} else 'mine'
    result = albums.list_albums(user, scope=scope, query=q, page=page)
    invitations = albums.list_invitations(user)
    stats = {'own': albums.list_albums(user, scope='mine', page_size=1)['total'],
             'joined': albums.list_albums(user, scope='joined', page_size=1)['total'], 'invitations': len(invitations)}
    return page_response('albums.html', request, user, albums=result['albums'], pagination=pagination(result), filters={'q': q, 'kind': '', 'scope': scope}, scope=scope, invitations=invitations, stats=stats)


@router.get('/albums/new', response_class=HTMLResponse)
def album_new(request: Request, user=Depends(require_user)):
    return page_response('album_form.html', request, user, album=None, form={})


@router.post('/albums/create')
def album_create(request: Request, title: str = Form(...), description: str = Form(''), kind: str = Form('personal'),
                 view_policy: str = Form('restricted'), contribute_policy: str = Form('restricted'), listed: bool = Form(False),
                 review_required: bool = Form(False), user=Depends(require_user)):
    form = dict(title=title, description=description, kind=kind, view_policy=view_policy,
                contribute_policy=contribute_policy, listed=listed, review_required=review_required)
    try:
        album = albums.create_album(user, title, **{key: value for key, value in form.items() if key != 'title'})
    except HTTPException as exc:
        if json_request(request):
            raise
        response = page_response('album_form.html', request, user, album=None, form=form,
                                 error=exc.detail.get('message', '请检查相册设置。') if isinstance(exc.detail, dict) else str(exc.detail))
        response.status_code = exc.status_code
        return response
    return done(request, album, '/albums/' + album['album_id'], '相册已创建，可以开始添加图片了。')


@router.post('/albums/invitations/{invitation_id}/accept')
def invitation_accept(invitation_id: str, request: Request, user=Depends(require_user)):
    return done(request, albums.respond_invitation(invitation_id, user, accept=True), '/albums?scope=joined', '已接受邀请。')


@router.post('/albums/invitations/{invitation_id}/decline')
def invitation_decline(invitation_id: str, request: Request, user=Depends(require_user)):
    return done(request, albums.respond_invitation(invitation_id, user, accept=False), '/albums', '已拒绝邀请。')


@router.get('/albums/{album_id}', response_class=HTMLResponse)
def album_detail(album_id: str, request: Request, page: int = 1, user=Depends(current_user)):
    album = albums.get_album(album_id, user)
    result = albums.list_album_items(album_id, user, page=page)
    return page_response('album_detail.html', request, user, album=album, capabilities=album['capabilities'], items=[normalize_photo(item) for item in result['items']], pagination=pagination(result))


@router.get('/albums/{album_id}/contribute', response_class=HTMLResponse)
def album_contribute(album_id: str, request: Request, q: str = '', page: int = 1, user=Depends(require_user)):
    album = albums.get_album(album_id, user, for_contribution=True)
    result = albums.list_own_images(user, query=q, page=page)
    return page_response('album_contribute.html', request, user, album=album, capabilities=album['capabilities'], images=[normalize_photo(item) for item in result['images']], pagination=pagination(result), filters={'q': q}, form={})


@router.get('/albums/{album_id}/manage', response_class=HTMLResponse)
def album_manage(album_id: str, request: Request, user=Depends(require_user)):
    album = albums.get_album(album_id, user, for_management=True)
    caps = album['capabilities']
    pending = albums.list_album_items(album_id, user, status='pending', page_size=100)['items'] if caps['can_review'] else []
    accepted = albums.list_album_items(album_id, user, page_size=100)['items'] if caps['can_view'] else []
    return page_response('album_manage.html', request, user, album=album, capabilities=caps,
                         members=albums.list_members(album_id, user) if caps['can_invite'] else [],
                         invitations=albums.list_album_invitations(album_id, user) if caps['can_invite'] else [],
                         pending_items=[normalize_photo(item) for item in pending], items=[normalize_photo(item) for item in accepted])


@router.post('/albums/{album_id}/settings')
def album_settings(album_id: str, request: Request, title: str = Form(...), description: str = Form(''),
                   view_policy: str = Form('restricted'), contribute_policy: str = Form('restricted'), listed: bool = Form(False),
                   review_required: bool = Form(False), expected_version: int | None = Form(None), user=Depends(require_user)):
    caps = albums.album_capabilities(album_id, user)
    values = dict(title=title, description=description, expected_version=expected_version)
    if caps['can_edit_access']:
        values.update(view_policy=view_policy, contribute_policy=contribute_policy, listed=listed, review_required=review_required)
    result = albums.update_album(album_id, user, **values)
    return done(request, result, f'/albums/{album_id}/manage', '相册设置已保存，图片仍按各自授权的范围展示。')


@router.post('/albums/{album_id}/delete')
def album_delete(album_id: str, request: Request, user=Depends(require_user)):
    return done(request, albums.delete_album(album_id, user), '/albums', '相册已删除，原图片和直链已保留。')


@router.post('/albums/{album_id}/cover')
def album_cover(album_id: str, request: Request, file_id: str = Form(''), user=Depends(require_user)):
    return done(request, albums.set_cover(album_id, user, file_id or None), f'/albums/{album_id}/manage', '相册封面已更新。')


@router.post('/albums/{album_id}/items')
def album_add(album_id: str, request: Request, file_ids: list[str] = Form([]), consent: bool = Form(False),
              audience_cap: str = Form('restricted'), allow_original: bool = Form(False), caption: str = Form('', max_length=2000), user=Depends(require_user)):
    consent_required(consent)
    result = albums.add_items(album_id, user, file_ids, audience_cap=audience_cap, allow_original=allow_original)
    if caption.strip():
        result['items'] = [albums.update_item(album_id, item['file_id'], user, caption=caption) for item in result['items']]
    return done(request, result, f'/albums/{album_id}/contribute', f'已加入 {result["added_count"]} 张图片；需要审核的投稿会在通过后展示。')


async def upload_photos(request, user, uploads, *, album_id=None, audience_cap='restricted', allow_original=False, visibility='unlisted', generate_link=False, caption=''):
    if not uploads or len(uploads) > 20:
        raise HTTPException(422, detail={'error': 'invalid_batch', 'message': '每次请选择 1 至 20 张图片。'})
    if album_id:
        await asyncio.to_thread(albums.get_album, album_id, user, for_contribution=True)
    results = []
    failures = []
    for upload in uploads:
        try:
            file = await save_upload(upload, user['id'], visibility, None, 'private_media', 'image', generate_link=generate_link)
            record = {'file': file, 'joined': False}
            results.append(record)
            if album_id:
                try:
                    associated = await asyncio.to_thread(albums.add_items, album_id, user, [file['file_id']], audience_cap=audience_cap, allow_original=allow_original)
                    record.update(joined=True, association=associated)
                    if caption.strip():
                        try:
                            await asyncio.to_thread(albums.update_item, album_id, file['file_id'], user, caption=caption)
                        except Exception as exc:
                            logger.error('Caption update failed (%s) after image association', type(exc).__name__)
                            record['caption_error'] = '图片已加入相册，说明暂未保存，请稍后补充。'
                except HTTPException as exc:
                    record['association_error'] = exc.detail.get('message', '加入相册未完成，图片已保留在我的图片。') if isinstance(exc.detail, dict) else str(exc.detail)
                except Exception as exc:
                    logger.error('Image association failed (%s); original already saved', type(exc).__name__)
                    record['association_error'] = '图片已保存，加入相册暂未完成，请稍后在我的图片中重试。'
        except HTTPException as exc:
            failures.append({'filename': (upload.filename or '图片')[:180], 'message': exc.detail.get('message', '图片上传失败。') if isinstance(exc.detail, dict) else str(exc.detail), 'status_code': exc.status_code})
        except Exception as exc:
            logger.error('Image upload failed (%s); preserving completed batch results', type(exc).__name__)
            failures.append({'filename': (upload.filename or '图片')[:180], 'message': '图片上传未完成，请稍后重试。', 'status_code': 503})
        finally:
            await upload.close()
    joined = sum(bool(record['joined']) for record in results)
    message = f'已上传 {len(results)} 张图片' + (f'，已加入相册 {joined} 张' if album_id else '') + '。'
    if failures:
        message += f' {len(failures)} 张未完成：' + '；'.join(item['filename'] + '：' + item['message'] for item in failures)
    if album_id and joined < len(results):
        message += ' 部分图片未加入相册，原图已保留，可在我的图片中重新整理。'
    if any(record.get('caption_error') for record in results):
        message += ' 部分图片说明暂未保存，可稍后补充。'
    destination = f'/albums/{album_id}/contribute' if album_id else '/images'
    if json_request(request):
        failed_status = failures[0]['status_code'] if len(uploads) == 1 and failures else 503 if any(item.get('status_code') == 503 for item in failures) else 422
        return JSONResponse({'ok': bool(results), 'uploaded': results, 'errors': failures,
                             'message': message, 'redirect_url': destination}, status_code=200 if results else failed_status)
    if not results:
        failed_status = failures[0]['status_code'] if len(uploads) == 1 and failures else 503 if any(item.get('status_code') == 503 for item in failures) else 422
        raise HTTPException(failed_status, detail={'error': 'upload_failed', 'message': message})
    return done(request, {'uploaded': results, 'errors': failures}, destination, message)


@router.post('/albums/{album_id}/upload')
async def album_upload(album_id: str, request: Request, files: list[UploadFile] = File(...), consent: bool = Form(False),
                       audience_cap: str = Form('restricted'), allow_original: bool = Form(False), caption: str = Form('', max_length=2000), user=Depends(require_user)):
    consent_required(consent)
    # Reject invalid consent scope before storing a single original.
    if audience_cap not in {'restricted', 'authenticated', 'public'}:
        raise HTTPException(422, detail={'error': 'invalid_scope', 'message': '请选择有效的图片展示范围。'})
    return await upload_photos(request, user, files, album_id=album_id, audience_cap=audience_cap, allow_original=allow_original, caption=caption)


@router.post('/albums/{album_id}/items/{file_id}/edit')
def item_edit(album_id: str, file_id: str, request: Request, caption: str = Form(''), position: int | None = Form(None), user=Depends(require_user)):
    return done(request, albums.update_item(album_id, file_id, user, caption=caption, position=position), f'/albums/{album_id}/manage', '图片说明和顺序已更新。')


@router.post('/albums/{album_id}/items/{file_id}/remove')
def item_remove(album_id: str, file_id: str, request: Request, user=Depends(require_user)):
    result = albums.remove_item(album_id, file_id, user)
    return done(request, result, '/images/' + file_id, '已移出相册，原图片已保留。') if albums.album_capabilities(album_id, user)['can_view'] is False else done(request, result, '/albums/' + album_id, '已移出相册，原图片已保留。')


@router.post('/albums/{album_id}/items/{file_id}/review')
def item_review(album_id: str, file_id: str, request: Request, decision: str = Form(...), user=Depends(require_user)):
    return done(request, albums.update_item(album_id, file_id, user, status=decision), f'/albums/{album_id}/manage', '投稿状态已更新。')


@router.post('/albums/{album_id}/items/{file_id}/consent')
def item_consent(album_id: str, file_id: str, request: Request, audience_cap: str = Form('restricted'), allow_original: bool = Form(False), user=Depends(require_user)):
    return done(request, albums.update_item(album_id, file_id, user, audience_cap=audience_cap, allow_original=allow_original), '/images/' + file_id, '图片在此相册中的展示授权已更新。')


@router.post('/albums/{album_id}/invitations')
def invitation_create(album_id: str, request: Request, username: str = Form(...), can_view: bool = Form(False),
                      can_contribute: bool = Form(False), can_manage: bool = Form(False), user=Depends(require_user)):
    return done(request, albums.invite_member(album_id, user, username, can_view=can_view, can_contribute=can_contribute, can_manage=can_manage), f'/albums/{album_id}/manage', '邀请已发送，对方接受后权限生效。')


@router.post('/albums/{album_id}/invitations/{invitation_id}/revoke')
def invitation_revoke(album_id: str, invitation_id: str, request: Request, user=Depends(require_user)):
    return done(request, albums.revoke_invitation(album_id, invitation_id, user), f'/albums/{album_id}/manage', '邀请已撤销。')


@router.post('/albums/{album_id}/members/{user_id}')
def member_update(album_id: str, user_id: int, request: Request, can_view: bool = Form(False), can_contribute: bool = Form(False),
                  can_manage: bool = Form(False), status: str = Form('active'), user=Depends(require_user)):
    return done(request, albums.update_member(album_id, user_id, user, can_view=can_view, can_contribute=can_contribute, can_manage=can_manage, status=status), f'/albums/{album_id}/manage', '成员权限已更新。')


@router.post('/albums/{album_id}/leave')
def member_leave(album_id: str, request: Request, user=Depends(require_user)):
    return done(request, albums.remove_member(album_id, user['id'], user), '/albums?scope=joined', '已退出相册，你仍可在我的图片中撤回自己的贡献。')


@router.get('/images', response_class=HTMLResponse)
def image_list(request: Request, q: str = '', page: int = 1, user=Depends(require_user)):
    result = albums.list_own_images(user, query=q, page=page)
    return page_response('images.html', request, user, files=[normalize_photo(item) for item in result['images']], pagination=pagination(result), filters={'q': q}, albums=albums.list_contributable_albums(user))


@router.get('/images/upload', response_class=HTMLResponse)
def image_upload_page(request: Request, user=Depends(require_user)):
    return page_response('upload.html', request, user, mode='image', album=None)


@router.post('/images/upload')
async def image_upload(request: Request, files: list[UploadFile] = File(...), visibility: str = Form('unlisted'),
                       generate_link: bool = Form(False), user=Depends(require_user)):
    if visibility not in {'private', 'unlisted', 'public'}:
        raise HTTPException(422, detail={'error': 'invalid_visibility', 'message': '请选择有效的直链权限。'})
    return await upload_photos(request, user, files, visibility=visibility, generate_link=generate_link)


@router.post('/images/album')
def image_add_album(request: Request, album_id: str = Form(...), file_ids: list[str] = Form([]), consent: bool = Form(False),
                    audience_cap: str = Form('restricted'), allow_original: bool = Form(False), user=Depends(require_user)):
    consent_required(consent)
    return done(request, albums.add_items(album_id, user, file_ids, audience_cap=audience_cap, allow_original=allow_original), '/images', '选中的图片已加入相册，需要审核的投稿将等待确认。')


@router.get('/images/{file_id}', response_class=HTMLResponse)
def image_detail(file_id: str, request: Request, user=Depends(require_user)):
    with connect() as conn:
        row = conn.execute("SELECT * FROM files WHERE file_id=? AND purpose='image'", (file_id,)).fetchone()
        asset = conn.execute('SELECT width,height,status AS media_status FROM image_assets WHERE file_id=?', (file_id,)).fetchone()
    if not row or not can_manage_file(row, user):
        raise HTTPException(404, detail={'error': 'not_found', 'message': '图片不存在或没有向你开放。'})
    photo = dict(row)
    if asset:
        photo.update(dict(asset))
    contributions = albums.list_image_associations(file_id, user)
    photo['associations'] = contributions
    return page_response('image_detail.html', request, user, file=normalize_photo(photo), albums=albums.list_contributable_albums(user), contributions=contributions)


@router.api_route('/media/images/{file_id}/{variant}', methods=['GET', 'HEAD'])
async def own_media(file_id: str, variant: str, request: Request, user=Depends(current_user)):
    return await image_response(file_id, variant, request, user)


@router.api_route('/media/albums/{album_id}/{file_id}/{variant}', methods=['GET', 'HEAD'])
async def album_media(album_id: str, file_id: str, variant: str, request: Request, user=Depends(current_user)):
    return await image_response(file_id, variant, request, user, album_id=album_id)


@router.api_route('/albums/{album_id}/items/{file_id}/original', methods=['GET', 'HEAD'])
async def album_original(album_id: str, file_id: str, request: Request, user=Depends(current_user)):
    return await original_response(file_id, request, user, album_id=album_id)


@router.post('/images/{file_id}/prepare')
async def image_prepare(file_id: str, request: Request, user=Depends(require_user)):
    return done(request, await prepare_image(file_id, user, retry=True), '/images/' + file_id, '图片预览已重新处理。')


class Payload(BaseModel):
    model_config = ConfigDict(extra='forbid')


class AlbumCreate(Payload):
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default='', max_length=2000)
    kind: str = 'personal'
    view_policy: str = 'restricted'
    contribute_policy: str = 'restricted'
    listed: bool = False
    review_required: bool = True


class AlbumUpdate(Payload):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    view_policy: str | None = None
    contribute_policy: str | None = None
    listed: bool | None = None
    review_required: bool | None = None
    expected_version: int | None = None


class ItemsAdd(Payload):
    file_ids: list[str] = Field(min_length=1, max_length=100)
    audience_cap: str = 'restricted'
    allow_original: bool = False
    consent: bool = False


class ItemUpdate(Payload):
    caption: str | None = Field(default=None, max_length=2000)
    audience_cap: str | None = None
    allow_original: bool | None = None
    status: str | None = None
    position: int | None = None


class MemberPayload(Payload):
    can_view: bool = False
    can_contribute: bool = False
    can_manage: bool = False
    status: str = 'active'


class InvitePayload(Payload):
    username: str = Field(min_length=1, max_length=32)
    can_view: bool = True
    can_contribute: bool = False
    can_manage: bool = False


@router.get('/api/albums')
def api_album_list(scope: str = 'public', q: str = '', kind: str = '', page: int = 1, user=Depends(current_user)):
    return albums.list_albums(user, scope=scope, query=q, kind=kind or None, page=page)


@router.post('/api/albums', status_code=201)
def api_album_create(body: AlbumCreate, user=Depends(require_user)):
    values = body.model_dump()
    return albums.create_album(user, values.pop('title'), **values)


@router.get('/api/albums/invitations')
def api_invitations(user=Depends(require_user)):
    return {'invitations': albums.list_invitations(user)}


@router.post('/api/albums/invitations/{invitation_id}/accept')
def api_invitation_accept(invitation_id: str, user=Depends(require_user)):
    return albums.respond_invitation(invitation_id, user, accept=True)


@router.post('/api/albums/invitations/{invitation_id}/decline')
def api_invitation_decline(invitation_id: str, user=Depends(require_user)):
    return albums.respond_invitation(invitation_id, user, accept=False)


@router.get('/api/albums/{album_id}')
def api_album_get(album_id: str, user=Depends(current_user)):
    return albums.get_album(album_id, user)


@router.patch('/api/albums/{album_id}')
def api_album_update(album_id: str, body: AlbumUpdate, user=Depends(require_user)):
    return albums.update_album(album_id, user, **body.model_dump(exclude_unset=True))


@router.delete('/api/albums/{album_id}')
def api_album_delete(album_id: str, user=Depends(require_user)):
    return {'deleted': albums.delete_album(album_id, user)}


@router.get('/api/albums/{album_id}/items')
def api_items_list(album_id: str, page: int = 1, status: str = 'accepted', user=Depends(current_user)):
    return albums.list_album_items(album_id, user, page=page, status=status)


@router.post('/api/albums/{album_id}/items')
def api_items_add(album_id: str, body: ItemsAdd, user=Depends(require_user)):
    consent_required(body.consent)
    return albums.add_items(album_id, user, body.file_ids, audience_cap=body.audience_cap, allow_original=body.allow_original)


@router.patch('/api/albums/{album_id}/items/{file_id}')
def api_item_update(album_id: str, file_id: str, body: ItemUpdate, user=Depends(require_user)):
    return albums.update_item(album_id, file_id, user, **body.model_dump(exclude_unset=True))


@router.delete('/api/albums/{album_id}/items/{file_id}')
def api_item_remove(album_id: str, file_id: str, user=Depends(require_user)):
    return {'removed': albums.remove_item(album_id, file_id, user)}


@router.post('/api/albums/{album_id}/invitations')
def api_invite(album_id: str, body: InvitePayload, user=Depends(require_user)):
    values = body.model_dump()
    return albums.invite_member(album_id, user, values.pop('username'), **values)


@router.delete('/api/albums/{album_id}/invitations/{invitation_id}')
def api_invite_revoke(album_id: str, invitation_id: str, user=Depends(require_user)):
    return {'revoked': albums.revoke_invitation(album_id, invitation_id, user)}


@router.patch('/api/albums/{album_id}/members/{member_user_id}')
def api_member_update(album_id: str, member_user_id: int, body: MemberPayload, user=Depends(require_user)):
    return albums.update_member(album_id, member_user_id, user, **body.model_dump())


@router.delete('/api/albums/{album_id}/members/{member_user_id}')
def api_member_remove(album_id: str, member_user_id: int, user=Depends(require_user)):
    return {'removed': albums.remove_member(album_id, member_user_id, user)}
