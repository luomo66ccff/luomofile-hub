"""Album ownership, explicit permissions, and uploader-controlled sharing.

This module never changes a file's owner, original object, or direct-link policy.
All mutations recheck permissions in the same short transaction as their writes.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import secrets
import sqlite3
from urllib.parse import quote

from fastapi import HTTPException

from app.db import connect
from app.security import now_iso


AUDIENCES = {"restricted", "authenticated", "public"}
ITEM_SELECT = """SELECT i.*, f.owner_id AS file_owner_id,
    u.username AS owner_username, f.original_filename AS filename,
    f.status AS file_status, f.expires_at, f.purpose,
    m.width, m.height, COALESCE(m.status,'pending') AS media_status
    FROM album_items i JOIN files f ON f.file_id=i.file_id
    LEFT JOIN users u ON u.id=f.owner_id
    LEFT JOIN image_assets m ON m.file_id=f.file_id"""
ACTIVE_IMAGE_SQL = """f.status='active' AND f.purpose='image'
    AND (f.expires_at IS NULL OR julianday(f.expires_at) > julianday(?))"""


def _error(status: int, code: str, message: str):
    raise HTTPException(status_code=status, detail={"error": code, "message": message})


def connect_gallery() -> sqlite3.Connection:
    conn = connect()
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def _db(*, write=False):
    conn = connect_gallery()
    try:
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if write:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _actor(conn, user):
    if user is None:
        return None
    return conn.execute("SELECT id,username,role,status FROM users WHERE id=?", (user["id"],)).fetchone()


def _active(user):
    return bool(user is not None and user["status"] == "active")


def _require_active(user):
    if user is None:
        _error(401, "login_required", "请先登录。")
    if not _active(user):
        _error(403, "email_verification_required", "请先激活邮箱。")


def _flag(value, name):
    if type(value) is bool or (type(value) is int and value in (0, 1)):
        return int(value)
    _error(422, "invalid_boolean", f"{name} 必须为布尔值。")


def _text(value, name, maximum, *, required=False):
    if not isinstance(value, str):
        _error(422, "invalid_text", f"{name} 必须为文本。")
    value = value.strip()
    if (required and not value) or len(value) > maximum or any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        _error(422, "invalid_text", f"{name} 长度或内容不符合要求。")
    return value


def _audience(value):
    if value not in AUDIENCES:
        _error(422, "invalid_audience", "请选择有效的展示范围。")
    return value


def _paging(page, page_size, maximum=100):
    try:
        return max(1, min(100000, int(page))), max(1, min(maximum, int(page_size)))
    except (ValueError, TypeError):
        _error(422, "invalid_page", "分页参数不正确。")


def _page_result(key, values, total, page, size):
    return {key: values, "page": page, "page_size": size, "total": total,
            "has_prev": page > 1, "has_next": page * size < total}


def _album(conn, album_id):
    row = conn.execute(
        "SELECT a.*,u.username AS owner_username FROM albums a JOIN users u ON u.id=a.owner_id WHERE a.album_id=? AND a.status='active'",
        (album_id,),
    ).fetchone()
    if row is None:
        _error(404, "album_not_found", "找不到这个相册。")
    return row


def _member(conn, album, user):
    if not _active(user):
        return None
    return conn.execute("SELECT * FROM album_members WHERE album_id=? AND user_id=?", (album["album_id"], user["id"])).fetchone()


def _caps(conn, album, user):
    active = _active(user)
    owner = bool(active and album["owner_id"] == user["id"])
    admin = bool(active and user["role"] == "admin")
    member = _member(conn, album, user)
    member_active = bool(member is not None and member["status"] == "active")
    blocked = bool(member is not None and member["status"] == "blocked")
    view = bool(owner or admin or album["view_policy"] == "public"
                or (active and album["view_policy"] == "authenticated")
                or (member_active and member["can_view"]))
    contribute = bool(owner or (active and not blocked and album["kind"] == "shared" and (
        album["contribute_policy"] == "authenticated" or (member_active and member["can_contribute"]))))
    manage = bool(owner or admin or (member_active and member["can_manage"]))
    return {"can_view": view, "can_contribute": contribute, "can_manage": manage,
            "can_review": bool(manage and view), "can_edit_access": owner,
            "can_invite": owner, "can_delete": bool(owner or admin),
            "is_owner": owner, "is_admin": admin, "is_member": member_active}


def _require_cap(caps, key, *, hidden=False):
    if not caps[key]:
        _error(404 if hidden else 403, "album_not_found" if hidden else "album_permission_denied",
               "找不到这个相册。" if hidden else "你没有执行这个操作的相册权限。")


def _restricted_access(conn, album, user):
    if not _active(user):
        return False
    if album["owner_id"] == user["id"] or user["role"] == "admin":
        return True
    member = _member(conn, album, user)
    return bool(member is not None and member["status"] == "active" and member["can_view"])


def _cap_clause(conn, album, user):
    if _restricted_access(conn, album, user):
        return "1=1", []
    if _active(user):
        return "(i.audience_cap IN ('authenticated','public') OR f.owner_id=?)", [user["id"]]
    return "i.audience_cap='public'", []


def _file_is_active(item):
    if item["file_status"] != "active" or item["purpose"] != "image":
        return False
    if not item["expires_at"]:
        return True
    try:
        expiry = datetime.fromisoformat(item["expires_at"])
        return expiry.tzinfo is not None and expiry > datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return False


def _visible_item(conn, album, item, user, caps):
    if not _file_is_active(item) or item["status"] == "withdrawn":
        return False
    own = bool(_active(user) and item["file_owner_id"] == user["id"])
    if own:
        return True  # A contributor can inspect their own submission, not the other items.
    if not caps["can_view"]:
        return False
    if item["status"] != "accepted" and not caps["can_review"]:
        return False
    return bool(item["audience_cap"] == "public"
                or (item["audience_cap"] == "authenticated" and _active(user))
                or _restricted_access(conn, album, user))


def _item(conn, album, file_id):
    row = conn.execute(ITEM_SELECT + " WHERE i.album_id=? AND i.file_id=?", (album["album_id"], file_id)).fetchone()
    if row is None:
        _error(404, "album_item_not_found", "找不到这张相册图片。")
    return row


def _serialize_item(conn, album, item, user, caps):
    own = bool(_active(user) and item["file_owner_id"] == user["id"])
    visible = _visible_item(conn, album, item, user, caps)
    can_review = bool(caps["can_review"] and visible)
    base = f"/media/albums/{quote(album['album_id'], safe='')}/{quote(item['file_id'], safe='')}"
    original_allowed = bool(own or caps["is_admin"] or item["allow_original"])
    return {
        "album_id": album["album_id"], "file_id": item["file_id"], "owner_id": item["file_owner_id"],
        "owner_username": item["owner_username"], "filename": item["filename"], "caption": item["caption"],
        "position": item["position"], "status": item["status"], "audience_cap": item["audience_cap"],
        "allow_original": bool(item["allow_original"]), "created_at": item["created_at"], "updated_at": item["updated_at"],
        "width": item["width"], "height": item["height"], "media_status": item["media_status"],
        "thumb_url": base + "/thumb" if visible else None, "preview_url": base + "/preview" if visible else None,
        "original_url": f"/albums/{quote(album['album_id'], safe='')}/items/{quote(item['file_id'], safe='')}/original" if visible and original_allowed else None,
        "can_edit_caption": bool(own or can_review), "can_withdraw": bool(own or can_review),
        "can_review": can_review, "can_edit_sharing": own,
    }


def _serialize_album(conn, album, user, caps=None):
    caps = caps or _caps(conn, album, user)
    out = dict(album)
    out["listed"] = bool(out["listed"])
    out["review_required"] = bool(out["review_required"])
    out["capabilities"] = caps
    out["item_count"] = 0
    out["cover_url"] = None
    # A contribute-only/manage-only member receives no contents, counts, or cover IDs.
    out["cover_file_id"] = None
    if caps["can_view"]:
        cap_sql, cap_args = _cap_clause(conn, album, user)
        clause = f"i.album_id=? AND i.status='accepted' AND {ACTIVE_IMAGE_SQL} AND ({cap_sql})"
        args = [album["album_id"], now_iso(), *cap_args]
        out["item_count"] = conn.execute("SELECT COUNT(*) FROM album_items i JOIN files f ON f.file_id=i.file_id WHERE " + clause, args).fetchone()[0]
        cover = conn.execute(
            "SELECT i.file_id FROM album_items i JOIN files f ON f.file_id=i.file_id WHERE " + clause
            + " ORDER BY CASE WHEN i.file_id=? THEN 0 ELSE 1 END,i.position,i.file_id LIMIT 1",
            [*args, album["cover_file_id"]],
        ).fetchone()
        if cover:
            out["cover_file_id"] = cover["file_id"]
            out["cover_url"] = f"/media/albums/{quote(album['album_id'], safe='')}/{quote(cover['file_id'], safe='')}/thumb"
    return out


def _bump(conn, album_id):
    conn.execute("UPDATE albums SET acl_version=acl_version+1,updated_at=? WHERE album_id=?", (now_iso(), album_id))


def _audit(conn, user, action, album_id, detail=None):
    conn.execute(
        "INSERT INTO audit_logs(created_at,actor_user_id,action,target_type,target_id,status,detail) VALUES (?,?,?,'album',?,'ok',?)",
        (now_iso(), user["id"] if user else None, action, album_id,
         json.dumps(detail or {}, ensure_ascii=False, separators=(",", ":"))[:1000]),
    )


def album_capabilities(album_id, user=None):
    with _db() as conn:
        return _caps(conn, _album(conn, album_id), _actor(conn, user))


def get_album(album_id, user=None, *, for_contribution=False, for_management=False):
    with _db() as conn:
        actor = _actor(conn, user)
        album = _album(conn, album_id)
        caps = _caps(conn, album, actor)
        required = "can_manage" if for_management else "can_contribute" if for_contribution else "can_view"
        _require_cap(caps, required, hidden=True)
        return _serialize_album(conn, album, actor, caps)


def list_albums(user=None, *, scope="public", kind=None, query="", page=1, page_size=24):
    page, size = _paging(page, page_size)
    query = _text(query, "搜索内容", 200)
    with _db() as conn:
        actor = _actor(conn, user)
        args = []
        clause = "a.status='active'"
        if scope == "public":
            clause += " AND a.view_policy='public' AND a.listed=1"
        elif scope == "mine":
            _require_active(actor)
            clause += " AND a.owner_id=?"
            args.append(actor["id"])
        elif scope == "joined":
            _require_active(actor)
            clause += " AND a.owner_id!=? AND EXISTS(SELECT 1 FROM album_members m WHERE m.album_id=a.album_id AND m.user_id=? AND m.status='active' AND (m.can_view=1 OR m.can_contribute=1 OR m.can_manage=1))"
            args.extend([actor["id"], actor["id"]])
        else:
            _error(422, "invalid_scope", "请选择公共、我的或参与的相册。")
        if kind is not None:
            if kind not in {"personal", "shared"}:
                _error(422, "invalid_album_kind", "相册类型不正确。")
            clause += " AND a.kind=?"
            args.append(kind)
        if query:
            clause += " AND (a.title LIKE ? OR a.description LIKE ?)"
            args.extend([f"%{query}%", f"%{query}%"])
        total = conn.execute("SELECT COUNT(*) FROM albums a WHERE " + clause, args).fetchone()[0]
        rows = conn.execute(
            "SELECT a.*,u.username AS owner_username FROM albums a JOIN users u ON u.id=a.owner_id WHERE " + clause
            + " ORDER BY a.updated_at DESC,a.album_id LIMIT ? OFFSET ?", [*args, size, (page - 1) * size],
        ).fetchall()
        return _page_result("albums", [_serialize_album(conn, a, actor) for a in rows], total, page, size)


def list_contributable_albums(user):
    with _db() as conn:
        actor = _actor(conn, user)
        _require_active(actor)
        rows = conn.execute(
            """SELECT a.*,u.username AS owner_username FROM albums a JOIN users u ON u.id=a.owner_id
            WHERE a.status='active' AND (a.owner_id=? OR (a.kind='shared' AND (
                a.contribute_policy='authenticated' OR EXISTS(SELECT 1 FROM album_members m
                WHERE m.album_id=a.album_id AND m.user_id=? AND m.status='active' AND m.can_contribute=1))))
            ORDER BY a.updated_at DESC,a.album_id""", (actor["id"], actor["id"]),
        ).fetchall()
        result = []
        for album in rows:
            caps = _caps(conn, album, actor)
            if not caps["can_contribute"]:
                continue
            out = _serialize_album(conn, album, actor, caps)
            if not caps["can_view"]:
                out = {key: out[key] for key in ("album_id", "title", "owner_id", "owner_username", "kind", "view_policy", "contribute_policy", "review_required", "capabilities", "item_count", "cover_file_id", "cover_url")}
                out["description"] = ""
            result.append(out)
        return result


def create_album(user, title, *, description="", kind="personal", view_policy="restricted", contribute_policy="restricted", listed=False, review_required=True):
    title = _text(title, "相册名称", 120, required=True)
    description = _text(description, "相册说明", 2000)
    _audience(view_policy)
    if kind not in {"personal", "shared"} or contribute_policy not in {"restricted", "authenticated"}:
        _error(422, "invalid_album_policy", "相册类型或投稿方式不正确。")
    listed = _flag(listed, "listed")
    review_required = _flag(review_required, "review_required")
    if (listed and view_policy != "public") or (kind == "personal" and contribute_policy != "restricted"):
        _error(422, "invalid_album_policy", "只有公开相册可列入公共目录；个人相册只接受所有者投稿。")
    with _db(write=True) as conn:
        actor = _actor(conn, user)
        _require_active(actor)
        album_id = "al_" + secrets.token_urlsafe(16)
        now = now_iso()
        conn.execute(
            "INSERT INTO albums(album_id,owner_id,title,description,kind,view_policy,contribute_policy,listed,review_required,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (album_id, actor["id"], title, description, kind, view_policy, contribute_policy, listed, review_required, now, now),
        )
        _audit(conn, actor, "album_create", album_id, {"kind": kind, "view_policy": view_policy})
        return _serialize_album(conn, _album(conn, album_id), actor)


def update_album(album_id, user, *, title=None, description=None, view_policy=None, contribute_policy=None, listed=None, review_required=None, expected_version=None):
    with _db(write=True) as conn:
        actor = _actor(conn, user)
        _require_active(actor)
        album = _album(conn, album_id)
        caps = _caps(conn, album, actor)
        _require_cap(caps, "can_manage")
        if expected_version is not None and expected_version != album["acl_version"]:
            _error(409, "album_changed", "相册已经更新，请刷新后重试。")
        changes = {}
        if title is not None:
            changes["title"] = _text(title, "相册名称", 120, required=True)
        if description is not None:
            changes["description"] = _text(description, "相册说明", 2000)
        if any(v is not None for v in (view_policy, contribute_policy, listed, review_required)):
            _require_cap(caps, "can_edit_access")
            if view_policy is not None:
                changes["view_policy"] = _audience(view_policy)
            if contribute_policy is not None:
                if contribute_policy not in {"restricted", "authenticated"}:
                    _error(422, "invalid_album_policy", "投稿方式不正确。")
                changes["contribute_policy"] = contribute_policy
            if listed is not None:
                changes["listed"] = _flag(listed, "listed")
            elif view_policy is not None and view_policy != "public":
                changes["listed"] = 0
            if review_required is not None:
                changes["review_required"] = _flag(review_required, "review_required")
        final = {**dict(album), **changes}
        if (final["listed"] and final["view_policy"] != "public") or (album["kind"] == "personal" and final["contribute_policy"] != "restricted"):
            _error(422, "invalid_album_policy", "相册展示与投稿设置不兼容。")
        if changes:
            conn.execute("UPDATE albums SET " + ",".join(f"{key}=?" for key in changes) + " WHERE album_id=?", [*changes.values(), album_id])
            _bump(conn, album_id)
            _audit(conn, actor, "album_update", album_id, {"fields": list(changes)})
        return _serialize_album(conn, _album(conn, album_id), actor)


def delete_album(album_id, user):
    with _db(write=True) as conn:
        actor = _actor(conn, user)
        _require_active(actor)
        album = _album(conn, album_id)
        _require_cap(_caps(conn, album, actor), "can_delete")
        now = now_iso()
        conn.execute("UPDATE albums SET status='deleted',cover_file_id=NULL,acl_version=acl_version+1,updated_at=? WHERE album_id=?", (now, album_id))
        conn.execute("UPDATE album_invitations SET status='revoked',responded_at=? WHERE album_id=? AND status='pending'", (now, album_id))
        _audit(conn, actor, "album_delete", album_id)
        return True


def list_album_items(album_id, user=None, *, page=1, page_size=36, status="accepted"):
    page, size = _paging(page, page_size)
    with _db() as conn:
        actor = _actor(conn, user)
        album = _album(conn, album_id)
        caps = _caps(conn, album, actor)
        args = [album_id, now_iso()]
        clause = f"i.album_id=? AND {ACTIVE_IMAGE_SQL}"
        if status == "mine":
            _require_active(actor)
            if not any(caps[key] for key in ("can_view", "can_contribute", "can_manage")):
                own_submission = conn.execute("SELECT 1 FROM album_items i JOIN files f ON f.file_id=i.file_id WHERE i.album_id=? AND f.owner_id=? AND i.status!='withdrawn' LIMIT 1", (album_id, actor["id"])).fetchone()
                if own_submission is None:
                    _error(404, "album_not_found", "找不到这个相册。")
            clause += " AND f.owner_id=? AND i.status!='withdrawn'"
            args.append(actor["id"])
        else:
            _require_cap(caps, "can_view", hidden=True)
            if status == "accepted":
                clause += " AND i.status='accepted'"
            elif status in {"pending", "all"}:
                _require_cap(caps, "can_review")
                clause += " AND i.status='pending'" if status == "pending" else " AND i.status!='withdrawn'"
            else:
                _error(422, "invalid_item_status", "图片筛选方式不正确。")
            cap_sql, cap_args = _cap_clause(conn, album, actor)
            clause += " AND (" + cap_sql + ")"
            args.extend(cap_args)
        total = conn.execute("SELECT COUNT(*) FROM album_items i JOIN files f ON f.file_id=i.file_id WHERE " + clause, args).fetchone()[0]
        rows = conn.execute(ITEM_SELECT + " WHERE " + clause + " ORDER BY i.position,i.created_at,i.file_id LIMIT ? OFFSET ?", [*args, size, (page - 1) * size]).fetchall()
        return _page_result("items", [_serialize_item(conn, album, row, actor, caps) for row in rows], total, page, size)


def get_album_item(album_id, file_id, user=None):
    with _db() as conn:
        actor = _actor(conn, user)
        album = _album(conn, album_id)
        caps = _caps(conn, album, actor)
        item = _item(conn, album, file_id)
        if not _visible_item(conn, album, item, actor, caps):
            _error(404, "album_item_not_found", "找不到这张相册图片。")
        return _serialize_item(conn, album, item, actor, caps)


def _file_ids(values):
    if not isinstance(values, (list, tuple)) or not values or len(values) > 100 or any(not isinstance(v, str) or not v or len(v) > 100 for v in values):
        _error(422, "invalid_file_ids", "请一次选择 1 至 100 张图片。")
    return list(dict.fromkeys(values))


def add_items(album_id, user, file_ids, *, audience_cap="restricted", allow_original=False):
    ids = _file_ids(file_ids)
    cap = _audience(audience_cap)
    allow = _flag(allow_original, "allow_original")
    with _db(write=True) as conn:
        actor = _actor(conn, user)
        _require_active(actor)
        album = _album(conn, album_id)
        caps = _caps(conn, album, actor)
        _require_cap(caps, "can_contribute")
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(f"SELECT f.file_id FROM files f WHERE f.file_id IN ({placeholders}) AND f.owner_id=? AND {ACTIVE_IMAGE_SQL}", [*ids, actor["id"], now_iso()]).fetchall()
        if len(rows) != len(ids):
            _error(404, "image_not_found", "只能选择自己拥有且未过期的有效图片。")
        status = "accepted" if not album["review_required"] or caps["can_review"] else "pending"
        now = now_iso()
        position = conn.execute("SELECT COALESCE(MAX(position),-1)+1 FROM album_items WHERE album_id=?", (album_id,)).fetchone()[0]
        added = 0
        for file_id in ids:
            existing = conn.execute("SELECT status FROM album_items WHERE album_id=? AND file_id=?", (album_id, file_id)).fetchone()
            if existing is not None and existing["status"] in {"accepted", "pending"}:
                continue  # Retries cannot silently widen an existing item's consent.
            conn.execute(
                """INSERT INTO album_items(album_id,file_id,added_by,status,audience_cap,allow_original,position,reviewed_by,reviewed_at,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(album_id,file_id) DO UPDATE SET
                   status=excluded.status,audience_cap=excluded.audience_cap,allow_original=excluded.allow_original,
                   position=excluded.position,reviewed_by=excluded.reviewed_by,reviewed_at=excluded.reviewed_at,updated_at=excluded.updated_at""",
                (album_id, file_id, actor["id"], status, cap, allow, position,
                 actor["id"] if status == "accepted" and caps["can_review"] else None,
                 now if status == "accepted" else None, now, now),
            )
            added += 1
            position += 1
        if added:
            _bump(conn, album_id)
            _audit(conn, actor, "album_items_add", album_id, {"file_ids": ids, "audience_cap": cap, "count": added})
        return {"items": [_serialize_item(conn, album, _item(conn, album, f), actor, caps) for f in ids], "added_count": added, "existing_count": len(ids) - added}


def update_item(album_id, file_id, user, *, caption=None, audience_cap=None, allow_original=None, status=None, position=None):
    with _db(write=True) as conn:
        actor = _actor(conn, user)
        _require_active(actor)
        album = _album(conn, album_id)
        caps = _caps(conn, album, actor)
        item = _item(conn, album, file_id)
        if not _visible_item(conn, album, item, actor, caps):
            _error(404, "album_item_not_found", "找不到这张相册图片。")
        own = item["file_owner_id"] == actor["id"]
        review = caps["can_review"]
        changes = {}
        if caption is not None:
            if not (own or review):
                _error(403, "album_permission_denied", "不能修改这张图片的说明。")
            changes["caption"] = _text(caption, "图片说明", 2000)
        if audience_cap is not None or allow_original is not None:
            if not own:
                _error(403, "uploader_consent_required", "只有图片上传者可以修改展示范围和原图下载授权。")
            if audience_cap is not None:
                changes["audience_cap"] = _audience(audience_cap)
            if allow_original is not None:
                changes["allow_original"] = _flag(allow_original, "allow_original")
        if status is not None:
            if not review:
                _error(403, "album_permission_denied", "没有审核图片的权限。")
            if status not in {"accepted", "rejected"}:
                _error(422, "invalid_item_status", "审核结果不正确。")
            changes.update(status=status, reviewed_by=actor["id"], reviewed_at=now_iso())
        if position is not None:
            if not review or type(position) is not int or not 0 <= position <= 1_000_000:
                _error(403 if not review else 422, "invalid_position", "没有排序权限或排序值不正确。")
            changes["position"] = position
        if changes:
            changes["updated_at"] = now_iso()
            conn.execute("UPDATE album_items SET " + ",".join(f"{key}=?" for key in changes) + " WHERE album_id=? AND file_id=?", [*changes.values(), album_id, file_id])
            if changes.get("status") == "rejected":
                conn.execute("UPDATE albums SET cover_file_id=NULL WHERE album_id=? AND cover_file_id=?", (album_id, file_id))
            _bump(conn, album_id)
            _audit(conn, actor, "album_item_update", album_id, {"file_id": file_id, "fields": list(changes)})
        return _serialize_item(conn, album, _item(conn, album, file_id), actor, caps)


def remove_item(album_id, file_id, user):
    with _db(write=True) as conn:
        actor = _actor(conn, user)
        _require_active(actor)
        album = _album(conn, album_id)
        caps = _caps(conn, album, actor)
        item = _item(conn, album, file_id)
        own = item["file_owner_id"] == actor["id"]
        if not own and not (caps["can_review"] and _visible_item(conn, album, item, actor, caps)):
            _error(404, "album_item_not_found", "找不到这张相册图片。")
        if item["status"] != "withdrawn":
            conn.execute("UPDATE album_items SET status='withdrawn',updated_at=? WHERE album_id=? AND file_id=?", (now_iso(), album_id, file_id))
            conn.execute("UPDATE albums SET cover_file_id=NULL WHERE album_id=? AND cover_file_id=?", (album_id, file_id))
            _bump(conn, album_id)
            _audit(conn, actor, "album_item_withdraw" if own else "album_item_remove", album_id, {"file_id": file_id})
        return True


def reorder_items(album_id, user, file_ids):
    ids = _file_ids(file_ids)
    with _db(write=True) as conn:
        actor = _actor(conn, user)
        _require_active(actor)
        album = _album(conn, album_id)
        caps = _caps(conn, album, actor)
        _require_cap(caps, "can_review")
        for file_id in ids:
            item = _item(conn, album, file_id)
            if item["status"] != "accepted" or not _visible_item(conn, album, item, actor, caps):
                _error(404, "album_item_not_found", "排序中包含不可管理的图片。")
        # Selected images move to the front in the requested order; others retain relative order.
        old = [r[0] for r in conn.execute("SELECT file_id FROM album_items WHERE album_id=? AND status='accepted' ORDER BY position,created_at,file_id", (album_id,))]
        order = ids + [f for f in old if f not in set(ids)]
        now = now_iso()
        conn.executemany("UPDATE album_items SET position=?,updated_at=? WHERE album_id=? AND file_id=?", [(index, now, album_id, file_id) for index, file_id in enumerate(order)])
        _bump(conn, album_id)
        _audit(conn, actor, "album_items_reorder", album_id, {"file_ids": ids})
        return {"ok": True, "file_ids": ids}


def set_cover(album_id, user, file_id=None):
    with _db(write=True) as conn:
        actor = _actor(conn, user)
        _require_active(actor)
        album = _album(conn, album_id)
        caps = _caps(conn, album, actor)
        _require_cap(caps, "can_review")
        if file_id:
            item = _item(conn, album, file_id)
            if item["status"] != "accepted" or not _visible_item(conn, album, item, actor, caps):
                _error(404, "album_item_not_found", "只能选择相册中可管理的图片作为封面。")
        conn.execute("UPDATE albums SET cover_file_id=? WHERE album_id=?", (file_id or None, album_id))
        _bump(conn, album_id)
        _audit(conn, actor, "album_cover_update", album_id, {"file_id": file_id})
        return _serialize_album(conn, _album(conn, album_id), actor)


def list_own_images(user, *, query="", page=1, page_size=36):
    page, size = _paging(page, page_size)
    query = _text(query, "搜索内容", 200)
    with _db() as conn:
        actor = _actor(conn, user)
        _require_active(actor)
        clause = "f.owner_id=? AND " + ACTIVE_IMAGE_SQL
        args = [actor["id"], now_iso()]
        if query:
            clause += " AND (f.original_filename LIKE ? OR f.tags LIKE ?)"
            args.extend([f"%{query}%", f"%{query}%"])
        total = conn.execute("SELECT COUNT(*) FROM files f WHERE " + clause, args).fetchone()[0]
        rows = conn.execute(
            "SELECT f.file_id,f.original_filename,f.size_bytes,f.uploaded_at,f.visibility,f.link_enabled,f.public_url,m.width,m.height,COALESCE(m.status,'pending') AS media_status "
            "FROM files f LEFT JOIN image_assets m ON m.file_id=f.file_id WHERE " + clause + " ORDER BY f.uploaded_at DESC,f.file_id LIMIT ? OFFSET ?",
            [*args, size, (page - 1) * size],
        ).fetchall()
        images = []
        for row in rows:
            image = dict(row)
            image["filename"] = image["original_filename"]
            image["owner_id"] = actor["id"]
            image["owner_username"] = actor["username"]
            image["link_enabled"] = bool(image["link_enabled"])
            if not image["link_enabled"]:
                image["public_url"] = None
            base = f"/media/images/{quote(row['file_id'], safe='')}"
            image.update(thumb_url=base + "/thumb", preview_url=base + "/preview", associations=[])
            image["associations"] = _associations(conn, row["file_id"], actor)
            images.append(image)
        return _page_result("images", images, total, page, size)


def _associations(conn, file_id, actor):
    rows = conn.execute("SELECT i.album_id,i.status,i.audience_cap,i.allow_original FROM album_items i JOIN albums a ON a.album_id=i.album_id WHERE i.file_id=? AND i.status!='withdrawn' AND a.status='active' ORDER BY i.created_at", (file_id,)).fetchall()
    result = []
    for row in rows:
        linked = dict(row)
        album = _album(conn, linked["album_id"])
        caps = _caps(conn, album, actor)
        linked["title"] = album["title"] if any(caps[k] for k in ("can_view", "can_contribute", "can_manage")) else "已退出的相册"
        linked["can_view"] = caps["can_view"]
        linked["can_withdraw"] = True
        linked["allow_original"] = bool(linked["allow_original"])
        result.append(linked)
    return result


def list_image_associations(file_id, user):
    with _db() as conn:
        actor = _actor(conn, user)
        _require_active(actor)
        row = conn.execute("SELECT owner_id,purpose FROM files WHERE file_id=?", (file_id,)).fetchone()
        if row is None or row["purpose"] != "image" or (row["owner_id"] != actor["id"] and actor["role"] != "admin"):
            _error(404, "image_not_found", "找不到这张图片。")
        return _associations(conn, file_id, actor)


def _member_data(row):
    out = dict(row)
    for key in ("can_view", "can_contribute", "can_manage"):
        if key in out:
            out[key] = bool(out[key])
    return out


def list_members(album_id, user):
    with _db() as conn:
        actor = _actor(conn, user)
        _require_active(actor)
        album = _album(conn, album_id)
        _require_cap(_caps(conn, album, actor), "can_invite")
        rows = conn.execute("SELECT m.*,u.username FROM album_members m JOIN users u ON u.id=m.user_id WHERE m.album_id=? ORDER BY m.created_at,m.user_id", (album_id,)).fetchall()
        return [_member_data(row) for row in rows]


def _invitation(conn, invitation_id):
    row = conn.execute(
        """SELECT i.*,u.username AS username,a.title AS album_title,a.status AS album_status,
        owner.username AS inviter_username FROM album_invitations i
        JOIN users u ON u.id=i.invitee_user_id JOIN albums a ON a.album_id=i.album_id
        JOIN users owner ON owner.id=i.created_by WHERE i.invitation_id=?""", (invitation_id,),
    ).fetchone()
    if row is None:
        _error(404, "invitation_not_found", "找不到这个邀请。")
    return row


def _permission_values(album, can_view, can_contribute, can_manage):
    values = [_flag(can_view, "can_view"), _flag(can_contribute, "can_contribute"), _flag(can_manage, "can_manage")]
    if not any(values):
        _error(422, "empty_permissions", "请至少选择一项权限，或直接移除成员。")
    if album["kind"] == "personal" and values[1]:
        _error(422, "personal_album_contribution", "个人相册只接受所有者投稿。")
    return values


def invite_member(album_id, user, username, *, can_view=True, can_contribute=False, can_manage=False):
    username = _text(username, "用户名", 120, required=True)
    with _db(write=True) as conn:
        actor = _actor(conn, user)
        _require_active(actor)
        album = _album(conn, album_id)
        _require_cap(_caps(conn, album, actor), "can_invite")
        permissions = _permission_values(album, can_view, can_contribute, can_manage)
        target = conn.execute("SELECT id FROM users WHERE username=? AND status IN ('active','pending')", (username,)).fetchone()
        if target is None:
            _error(404, "invitee_not_found", "找不到这个可邀请的账户。")
        if target["id"] == album["owner_id"]:
            _error(422, "owner_is_member", "相册所有者已经拥有管理权限。")
        existing = conn.execute("SELECT status FROM album_members WHERE album_id=? AND user_id=?", (album_id, target["id"])).fetchone()
        if existing is not None and existing["status"] == "active":
            _error(409, "already_member", "这个账户已经是成员，请直接调整其权限。")
        now = now_iso()
        # Reissuing an invitation replaces any pending grant; old IDs cannot be accepted.
        conn.execute("UPDATE album_invitations SET status='revoked',responded_at=? WHERE album_id=? AND invitee_user_id=? AND status='pending'", (now, album_id, target["id"]))
        invitation_id = "ai_" + secrets.token_urlsafe(16)
        expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO album_invitations(invitation_id,album_id,invitee_user_id,created_by,can_view,can_contribute,can_manage,expires_at,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (invitation_id, album_id, target["id"], actor["id"], *permissions, expires, now),
        )
        _audit(conn, actor, "album_invite", album_id, {"invitee_user_id": target["id"], "permissions": permissions})
        return _member_data(_invitation(conn, invitation_id))


def list_invitations(user):
    with _db() as conn:
        actor = _actor(conn, user)
        _require_active(actor)
        rows = conn.execute(
            """SELECT i.*,a.title AS album_title,creator.username AS inviter_username,u.username
            FROM album_invitations i JOIN albums a ON a.album_id=i.album_id
            JOIN users creator ON creator.id=i.created_by JOIN users u ON u.id=i.invitee_user_id
            WHERE i.invitee_user_id=? AND i.status='pending' AND a.status='active' AND julianday(i.expires_at)>julianday(?)
            ORDER BY i.created_at DESC,i.invitation_id""", (actor["id"], now_iso()),
        ).fetchall()
        return [_member_data(row) for row in rows]


def list_album_invitations(album_id, user):
    with _db() as conn:
        actor = _actor(conn, user)
        _require_active(actor)
        album = _album(conn, album_id)
        _require_cap(_caps(conn, album, actor), "can_invite")
        rows = conn.execute("SELECT i.*,u.username,creator.username AS inviter_username FROM album_invitations i JOIN users u ON u.id=i.invitee_user_id JOIN users creator ON creator.id=i.created_by WHERE i.album_id=? ORDER BY i.created_at DESC,i.invitation_id", (album_id,)).fetchall()
        return [_member_data(row) for row in rows]


def respond_invitation(invitation_id, user, *, accept=True):
    accept = _flag(accept, "accept")
    with _db(write=True) as conn:
        actor = _actor(conn, user)
        _require_active(actor)
        invitation = _invitation(conn, invitation_id)
        if invitation["invitee_user_id"] != actor["id"]:
            _error(404, "invitation_not_found", "找不到这个邀请。")
        desired = "accepted" if accept else "declined"
        if invitation["status"] == desired:
            return _member_data(invitation)  # Safe repeat; never recreates revoked membership.
        if invitation["status"] != "pending" or invitation["album_status"] != "active":
            _error(409, "invitation_unavailable", "这个邀请已经处理或撤销。")
        now = now_iso()
        if invitation["expires_at"] <= now:
            _error(410, "invitation_expired", "这个邀请已经过期，请联系相册所有者重新邀请。")
        album = _album(conn, invitation["album_id"])
        if invitation["created_by"] != album["owner_id"]:
            _error(409, "invitation_unavailable", "邀请授权已经失效。")
        if accept:
            conn.execute(
                """INSERT INTO album_members(album_id,user_id,can_view,can_contribute,can_manage,status,granted_by,created_at,updated_at)
                VALUES (?,?,?,?,?,'active',?,?,?) ON CONFLICT(album_id,user_id) DO UPDATE SET
                can_view=excluded.can_view,can_contribute=excluded.can_contribute,can_manage=excluded.can_manage,
                status='active',granted_by=excluded.granted_by,updated_at=excluded.updated_at""",
                (album["album_id"], actor["id"], invitation["can_view"], invitation["can_contribute"], invitation["can_manage"], album["owner_id"], now, now),
            )
            _bump(conn, album["album_id"])
        conn.execute("UPDATE album_invitations SET status=?,responded_at=? WHERE invitation_id=? AND status='pending'", (desired, now, invitation_id))
        _audit(conn, actor, "album_invitation_" + desired, album["album_id"], {"invitation_id": invitation_id})
        return _member_data(_invitation(conn, invitation_id))


def revoke_invitation(album_id, invitation_id, user):
    with _db(write=True) as conn:
        actor = _actor(conn, user)
        _require_active(actor)
        album = _album(conn, album_id)
        _require_cap(_caps(conn, album, actor), "can_invite")
        invitation = _invitation(conn, invitation_id)
        if invitation["album_id"] != album_id:
            _error(404, "invitation_not_found", "找不到这个邀请。")
        if invitation["status"] == "pending":
            conn.execute("UPDATE album_invitations SET status='revoked',responded_at=? WHERE invitation_id=?", (now_iso(), invitation_id))
            _audit(conn, actor, "album_invitation_revoke", album_id, {"invitation_id": invitation_id})
        return True


def update_member(album_id, member_user_id, user, *, can_view, can_contribute, can_manage, status="active"):
    if status not in {"active", "revoked", "blocked"}:
        _error(422, "invalid_member_status", "成员状态不正确。")
    with _db(write=True) as conn:
        actor = _actor(conn, user)
        _require_active(actor)
        album = _album(conn, album_id)
        _require_cap(_caps(conn, album, actor), "can_invite")
        if member_user_id == album["owner_id"]:
            _error(422, "owner_permissions_immutable", "不能修改相册所有者的成员权限。")
        existing = conn.execute("SELECT * FROM album_members WHERE album_id=? AND user_id=?", (album_id, member_user_id)).fetchone()
        if existing is None:
            _error(404, "member_not_found", "找不到这个成员，请先发送邀请。")
        if status == "active":
            if existing["status"] != "active":
                _error(409, "invitation_required", "重新加入相册需要对方接受新的邀请。")
            permissions = _permission_values(album, can_view, can_contribute, can_manage)
        else:
            permissions = [0, 0, 0]
        now = now_iso()
        conn.execute("UPDATE album_members SET can_view=?,can_contribute=?,can_manage=?,status=?,granted_by=?,updated_at=? WHERE album_id=? AND user_id=?", (*permissions, status, actor["id"], now, album_id, member_user_id))
        conn.execute("UPDATE album_invitations SET status='revoked',responded_at=? WHERE album_id=? AND invitee_user_id=? AND status='pending'", (now, album_id, member_user_id))
        _bump(conn, album_id)
        _audit(conn, actor, "album_member_update", album_id, {"member_user_id": member_user_id, "status": status, "permissions": permissions})
        row = conn.execute("SELECT m.*,u.username FROM album_members m JOIN users u ON u.id=m.user_id WHERE m.album_id=? AND m.user_id=?", (album_id, member_user_id)).fetchone()
        return _member_data(row)


def remove_member(album_id, member_user_id, user, *, block=False):
    block = _flag(block, "block")
    with _db(write=True) as conn:
        actor = _actor(conn, user)
        _require_active(actor)
        album = _album(conn, album_id)
        if member_user_id == album["owner_id"]:
            _error(422, "owner_permissions_immutable", "不能移除相册所有者。")
        self_leave = member_user_id == actor["id"] and not block
        if self_leave:
            membership = _member(conn, album, actor)
            if membership is None or membership["status"] != "active":
                _error(404, "member_not_found", "你当前不是这个相册的成员。")
        else:
            _require_cap(_caps(conn, album, actor), "can_invite")
        if conn.execute("SELECT id FROM users WHERE id=?", (member_user_id,)).fetchone() is None:
            _error(404, "member_not_found", "找不到这个账户。")
        now = now_iso()
        status = "blocked" if block else "revoked"
        conn.execute(
            """INSERT INTO album_members(album_id,user_id,can_view,can_contribute,can_manage,status,granted_by,created_at,updated_at)
            VALUES (?,?,0,0,0,?,?,?,?) ON CONFLICT(album_id,user_id) DO UPDATE SET
            can_view=0,can_contribute=0,can_manage=0,status=excluded.status,granted_by=excluded.granted_by,updated_at=excluded.updated_at""",
            (album_id, member_user_id, status, actor["id"], now, now),
        )
        conn.execute("UPDATE album_invitations SET status='revoked',responded_at=? WHERE album_id=? AND invitee_user_id=? AND status='pending'", (now, album_id, member_user_id))
        _bump(conn, album_id)
        _audit(conn, actor, "album_member_leave" if self_leave else "album_member_" + status, album_id, {"member_user_id": member_user_id})
        return True
