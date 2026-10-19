"""Validated images and private, regenerable presentation media.

Original bytes stay untouched. All decoding runs in an explicitly bounded worker
thread. Persistent leases make an interrupted lazy conversion safely retryable.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextvars
from contextlib import closing
from functools import partial
from datetime import datetime, timedelta, timezone
import hashlib
import io
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import threading
import time
import warnings

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, Response
from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError, features
from starlette.background import BackgroundTask

from app.config import settings
from app.security import content_disposition, now_iso
from app.storage.local_temp import LocalTempBackend
from app.storage.router import get_backend
from app.storage.disk_space import ensure_disk_space


PIPELINE_VERSION = 1
FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif", "AVIF": "image/avif"}
VARIANTS = {"thumb": 480, "preview": 1600}
_WORK = threading.BoundedSemaphore(1)
_IMAGE_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix='gallery-image')
_IMAGE_SUBMISSIONS = threading.BoundedSemaphore(8)
_CACHE_LOCK = threading.RLock()
_PINS: dict[str, int] = {}
_DELETE_AFTER_USE: set[str] = set()
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
_HASH_RE = re.compile(r"[a-f0-9]{64}\Z")
NO_STORE = {"Cache-Control": "private, no-store, max-age=0", "Pragma": "no-cache",
            "Vary": "Cookie, Authorization", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow"}


async def run_image_work(function, *args, **kwargs):
    """Do not occupy the shared I/O pool while waiting for image CPU work.

    Release admission only when the actual thread future finishes/cancels;
    cancellation of an HTTP coroutine cannot release a still-running slot.
    """
    if not _IMAGE_SUBMISSIONS.acquire(blocking=False):
        raise ImageMediaError('media_processing_busy', '图片处理中，请稍后重试。', status_code=503, retryable=True)
    try:
        context = contextvars.copy_context()
        future = _IMAGE_EXECUTOR.submit(context.run, partial(function, *args, **kwargs))
    except BaseException:
        _IMAGE_SUBMISSIONS.release()
        raise
    future.add_done_callback(lambda _: _IMAGE_SUBMISSIONS.release())
    return await asyncio.wrap_future(future)


class ImageMediaError(Exception):
    def __init__(self, code, message, *, status_code=422, retryable=False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable


def _http_error(exc):
    return HTTPException(status_code=exc.status_code, detail={"error": exc.code, "message": exc.message}, headers=NO_STORE)


def _connect():
    from app.album_service import connect_gallery
    return connect_gallery()


def _cache_root():
    root = settings.database_path.parent / "media-cache"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _file_dir(file_id):
    if not isinstance(file_id, str) or not _ID_RE.fullmatch(file_id):
        raise ImageMediaError("invalid_image_id", "图片标识不正确。")
    root = _cache_root()
    target = (root / file_id).resolve()
    if not target.is_relative_to(root) or target == root:
        raise ImageMediaError("invalid_image_id", "图片标识不正确。")
    return target


def _cache_path(object_key):
    root = _cache_root()
    target = (root / object_key).resolve()
    if not target.is_relative_to(root) or target == root:
        raise ImageMediaError("invalid_media_path", "图片缓存路径不正确。", status_code=503, retryable=True)
    return target


def _limit(name, default):
    try:
        return max(1, int(getattr(settings, name, default)))
    except (ValueError, TypeError):
        return default


def _dimensions(width, height):
    if min(width, height) <= 0 or max(width, height) > _limit("image_max_dimension", 16384):
        raise ImageMediaError("image_dimensions_exceeded", "图片边长超过允许范围。")
    if width * height > _limit("image_max_pixels", 40_000_000):
        raise ImageMediaError("image_pixels_exceeded", "图片像素数超过允许范围。")


def _safe_remove_tree(path):
    root = _cache_root()
    path = Path(path).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError("Refusing removal outside media cache")
    if path.exists():
        shutil.rmtree(path)


def _is_pinned(directory):
    directory = Path(directory)
    return any(count and Path(key).is_relative_to(directory) for key, count in _PINS.items())


def _cache_capacity(new_size, target_dir):
    """Called under the cache lock. Evict old regenerable artifacts, never originals."""
    maximum = _limit("image_cache_max_bytes", 256 * 1024**2)
    if new_size > maximum:
        raise ImageMediaError("media_cache_full", "图片预览缓存空间不足。", status_code=507, retryable=True)
    root = _cache_root()
    candidates = []
    total = 0
    replaced = 0
    for file_dir in root.iterdir():
        if not file_dir.is_dir() or file_dir.is_symlink() or file_dir.name.startswith("."):
            continue
        for generation in file_dir.iterdir():
            if not generation.is_dir() or generation.is_symlink():
                continue
            paths = [p for p in generation.iterdir() if p.is_file() and not p.is_symlink()]
            try:
                stats = [p.stat() for p in paths]
            except FileNotFoundError:
                continue
            size = sum(s.st_size for s in stats)
            total += size
            if generation.resolve() == target_dir.resolve():
                replaced = size
            elif not _is_pinned(generation):
                last_use = max((s.st_mtime for s in stats), default=0)
                candidates.append((last_use, generation, size))
    needed = max(0, new_size - replaced)
    for _, directory, size in sorted(candidates, key=lambda item: item[0]):
        if total + needed <= maximum:
            break
        try:
            _safe_remove_tree(directory)
            total -= size
        except OSError:
            continue
    if total + needed > maximum:
        raise ImageMediaError("media_cache_busy", "图片预览正在使用，请稍后重试。", status_code=503, retryable=True)


def _clean_pixels(frame):
    # This is already a private frame copy. Avoid keeping several full-resolution
    # copies alive on the small production host while preserving original bytes.
    ImageOps.exif_transpose(frame, in_place=True)
    alpha = "A" in frame.getbands() or "transparency" in frame.info
    mode = "RGBA" if alpha else "RGB"
    profile = frame.info.get("icc_profile")
    pixels = frame if frame.mode == mode else frame.convert(mode)
    if pixels is not frame:
        frame.close()
    try:
        if isinstance(profile, bytes) and len(profile) <= 1024 * 1024:
            try:
                converted = ImageCms.profileToProfile(pixels, ImageCms.ImageCmsProfile(io.BytesIO(profile)), ImageCms.createProfile("sRGB"), outputMode=mode)
                pixels.close()
                pixels = converted
            except (OSError, ValueError, TypeError, ImageCms.PyCMSError):
                pass
        # Fresh pixels-only output keeps EXIF/GPS/comments/XMP/ICC out of variants.
        clean = Image.new(mode, pixels.size)
        clean.paste(pixels)
        return clean
    finally:
        pixels.close()


def _build_unlocked(source_path, file_id, source_sha256):
    file_root = _file_dir(file_id)
    if not isinstance(source_sha256, str) or not _HASH_RE.fullmatch(source_sha256):
        raise ImageMediaError("invalid_source_hash", "图片校验信息不正确。")
    source_path = Path(source_path)
    max_bytes = int(settings.max_image_bytes)
    try:
        if source_path.stat().st_size <= 0 or source_path.stat().st_size > max_bytes:
            raise ImageMediaError("image_size_exceeded", "图片为空或超过允许大小。", status_code=413)
        digest = hashlib.sha256()
        with source_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != source_sha256:
            raise ImageMediaError("image_source_changed", "原图与保存的校验信息不一致。")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source_path) as probe:
                image_format = probe.format
                if image_format not in FORMATS:
                    raise ImageMediaError("unsupported_image", "请选择 JPEG、PNG、WebP、GIF 或可解码的 AVIF 图片。")
                source_width, source_height = probe.size
                _dimensions(source_width, source_height)
                probe.verify()
            with Image.open(source_path) as decoded:
                count = int(getattr(decoded, "n_frames", 1))
                if count < 1 or count > _limit("image_max_frames", 200):
                    raise ImageMediaError("image_frames_exceeded", "动画帧数超过允许范围。")
                if count * source_width * source_height > _limit("image_max_decoded_pixels", 100_000_000):
                    raise ImageMediaError("image_decoded_pixels_exceeded", "动画解码量超过允许范围。")
                first = None
                total_pixels = 0
                for index in range(count):
                    decoded.seek(index)
                    _dimensions(*decoded.size)
                    total_pixels += decoded.width * decoded.height
                    if total_pixels > _limit("image_max_decoded_pixels", 100_000_000):
                        raise ImageMediaError("image_decoded_pixels_exceeded", "动画解码量超过允许范围。")
                    decoded.load()
                    if index == 0:
                        first = _clean_pixels(decoded.copy())
                if first is None:
                    raise ImageMediaError("invalid_image", "无法读取这张图片。")
    except ImageMediaError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageMediaError("image_pixels_exceeded", "图片解码规模超过允许范围。") from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError) as exc:
        raise ImageMediaError("invalid_image", "图片损坏或格式不受支持。") from exc

    root = _cache_root()
    display_width, display_height = first.size
    staging_root = root / ".staging"
    staging_root.mkdir(exist_ok=True)
    target_dir = file_root / f"{source_sha256}-p{PIPELINE_VERSION}"
    results = []
    try:
        with tempfile.TemporaryDirectory(prefix="render-", dir=staging_root) as temp_dir:
            staging = Path(temp_dir)
            use_webp = features.check("webp")
            for variant, edge in VARIANTS.items():
                thumbnail = first.copy()
                thumbnail.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                ensure_disk_space(staging, thumbnail.width * thumbnail.height * 4 + 65536)
                if use_webp:
                    extension, mime = "webp", "image/webp"
                    output = staging / f"{variant}.{extension}"
                    thumbnail.save(output, "WEBP", quality=84, method=4)
                elif thumbnail.mode == "RGBA":
                    extension, mime = "png", "image/png"
                    output = staging / f"{variant}.{extension}"
                    thumbnail.save(output, "PNG", optimize=False)
                else:
                    extension, mime = "jpg", "image/jpeg"
                    output = staging / f"{variant}.{extension}"
                    thumbnail.save(output, "JPEG", quality=86, progressive=True)
                results.append({"variant": variant, "mime_type": mime, "width": thumbnail.width,
                                "height": thumbnail.height, "size_bytes": output.stat().st_size,
                                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                                "object_key": (target_dir / output.name).relative_to(root).as_posix()})
            with _CACHE_LOCK:
                _cache_capacity(sum(v["size_bytes"] for v in results), target_dir)
                ensure_disk_space(target_dir)
                target_dir.mkdir(parents=True, exist_ok=True)
                for variant in results:
                    destination = _cache_path(variant["object_key"])
                    os.replace(staging / destination.name, destination)
    except ImageMediaError:
        raise
    except HTTPException as exc:
        raise ImageMediaError("media_disk_full", "服务器预览存储空间不足，请稍后重试。", status_code=507, retryable=True) from exc
    except OSError as exc:
        raise ImageMediaError("media_storage_unavailable", "暂时无法生成图片预览，请稍后重试。", status_code=503, retryable=True) from exc
    finally:
        first.close()
    return {"file_id": file_id, "source_sha256": source_sha256, "pipeline_version": PIPELINE_VERSION,
            "detected_mime": FORMATS[image_format], "source_width": source_width, "source_height": source_height,
            "width": display_width, "height": display_height,
            "frame_count": count, "media_status": "ready", "variants": results}


def build_image_media(source_path: Path, file_id: str, source_sha256: str) -> dict:
    if not _WORK.acquire(timeout=45):
        raise ImageMediaError("media_processing_busy", "图片处理中，请稍后重试。", status_code=503, retryable=True)
    try:
        result = _build_unlocked(source_path, file_id, source_sha256)
        return result
    finally:
        _WORK.release()


def record_image_media(conn, file_id: str, result: dict, *, lease_token=None) -> None:
    row = conn.execute("SELECT sha256,status FROM files WHERE file_id=?", (file_id,)).fetchone()
    if row is None or row["status"] != "active" or row["sha256"] != result["source_sha256"]:
        raise ImageMediaError("image_unavailable", "原图已经不可用。", status_code=404)
    if lease_token is not None:
        asset = conn.execute("SELECT lease_token FROM image_assets WHERE file_id=?", (file_id,)).fetchone()
        if asset is None or asset["lease_token"] != lease_token:
            raise ImageMediaError("media_processing_busy", "图片处理状态已经变化，请重试。", status_code=503, retryable=True)
    now = now_iso()
    width = result["width"]
    height = result["height"]
    conn.execute(
        """INSERT INTO image_assets(file_id,source_sha256,detected_mime,source_width,source_height,width,height,frame_count,pipeline_version,status,attempts,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,'ready',1,?,?) ON CONFLICT(file_id) DO UPDATE SET
        source_sha256=excluded.source_sha256,detected_mime=excluded.detected_mime,source_width=excluded.source_width,
        source_height=excluded.source_height,width=excluded.width,height=excluded.height,frame_count=excluded.frame_count,
        pipeline_version=excluded.pipeline_version,status='ready',error_code=NULL,retryable=1,retry_after=NULL,
        lease_until=NULL,lease_token=NULL,updated_at=excluded.updated_at""",
        (file_id, result["source_sha256"], result["detected_mime"], result["source_width"], result["source_height"],
         width, height, result["frame_count"], PIPELINE_VERSION, now, now),
    )
    conn.execute("DELETE FROM image_variants WHERE file_id=?", (file_id,))
    conn.executemany(
        "INSERT INTO image_variants(file_id,source_sha256,pipeline_version,variant,mime_type,width,height,size_bytes,sha256,object_key,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(file_id, result["source_sha256"], PIPELINE_VERSION, v["variant"], v["mime_type"], v["width"], v["height"], v["size_bytes"], v["sha256"], v["object_key"], now) for v in result["variants"]],
    )


def _metadata(asset):
    return {"file_id": asset["file_id"], "width": asset["width"], "height": asset["height"],
            "mime_type": asset["detected_mime"], "frame_count": asset["frame_count"], "media_status": asset["status"]}


def _ready(conn, row, asset):
    if asset is None or asset["status"] != "ready" or asset["source_sha256"] != row["sha256"] or asset["pipeline_version"] != PIPELINE_VERSION:
        return False
    variants = conn.execute("SELECT * FROM image_variants WHERE file_id=? AND source_sha256=? AND pipeline_version=?", (row["file_id"], row["sha256"], PIPELINE_VERSION)).fetchall()
    if {v["variant"] for v in variants} != set(VARIANTS):
        return False
    for variant in variants:
        try:
            path = _cache_path(variant["object_key"])
            if not path.is_file() or path.stat().st_size != variant["size_bytes"]:
                return False
        except (OSError, ImageMediaError):
            return False
    return True


def _materialize(row):
    source_dir = _cache_root() / ".source"
    source_dir.mkdir(exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="image-", suffix=".source", dir=source_dir)
    output = Path(name)
    maximum = int(settings.max_image_bytes)
    total = 0
    started = time.monotonic()
    try:
        with os.fdopen(descriptor, "wb") as handle, closing(get_backend(row["storage_backend"]).open_reader(row["object_key"])) as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise ImageMediaError("image_size_exceeded", "原图超过当前预览大小限制。", status_code=413)
                if time.monotonic() - started > 90:
                    raise ImageMediaError("image_read_timeout", "读取原图超时，请稍后重试。", status_code=503, retryable=True)
                ensure_disk_space(output.parent, len(chunk))
                handle.write(chunk)
        return output
    except Exception:
        output.unlink(missing_ok=True)
        raise


def _ensure_image(file_id, *, retry=False):
    if not _WORK.acquire(timeout=45):
        raise ImageMediaError("media_processing_busy", "图片处理中，请稍后重试。", status_code=503, retryable=True)
    token = None
    source = None
    try:
        with closing(_connect()) as conn:
            row = conn.execute("SELECT * FROM files WHERE file_id=? AND status='active' AND purpose='image'", (file_id,)).fetchone()
            if row is None:
                raise ImageMediaError("image_unavailable", "找不到这张图片。", status_code=404)
            asset = conn.execute("SELECT * FROM image_assets WHERE file_id=?", (file_id,)).fetchone()
            if _ready(conn, row, asset):
                return _metadata(asset)
            conn.execute("BEGIN IMMEDIATE")
            # Recheck after acquiring the write lease; another process may have finished.
            row = conn.execute("SELECT * FROM files WHERE file_id=? AND status='active' AND purpose='image'", (file_id,)).fetchone()
            if row is None:
                raise ImageMediaError("image_unavailable", "找不到这张图片。", status_code=404)
            asset = conn.execute("SELECT * FROM image_assets WHERE file_id=?", (file_id,)).fetchone()
            if _ready(conn, row, asset):
                conn.commit()
                return _metadata(asset)
            now = now_iso()
            if asset is not None and asset["status"] == "processing" and asset["lease_until"] and asset["lease_until"] > now:
                raise ImageMediaError("media_processing_busy", "图片正在处理中，请稍后重试。", status_code=503, retryable=True)
            if asset is not None and asset["status"] == "failed" and not retry:
                if not asset["retryable"]:
                    raise ImageMediaError(asset["error_code"] or "invalid_image", "这张图片无法生成预览，上传者可以重试。")
                if asset["retry_after"] and asset["retry_after"] > now:
                    raise ImageMediaError("media_retry_later", "预览暂不可用，请稍后重试。", status_code=503, retryable=True)
            token = secrets.token_urlsafe(18)
            lease = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(timespec="seconds")
            conn.execute(
                """INSERT INTO image_assets(file_id,source_sha256,pipeline_version,status,attempts,lease_until,lease_token,created_at,updated_at)
                VALUES (?,?,?,'processing',1,?,?,?,?) ON CONFLICT(file_id) DO UPDATE SET
                source_sha256=excluded.source_sha256,pipeline_version=excluded.pipeline_version,status='processing',
                attempts=image_assets.attempts+1,lease_until=excluded.lease_until,lease_token=excluded.lease_token,error_code=NULL,updated_at=excluded.updated_at""",
                (file_id, row["sha256"], PIPELINE_VERSION, lease, token, now, now),
            )
            conn.commit()
        try:
            source = _materialize(row)
        except ImageMediaError:
            raise
        except HTTPException as exc:
            raise ImageMediaError("media_disk_full", "服务器预览存储空间不足，请稍后重试。", status_code=507, retryable=True) from exc
        except Exception as exc:
            raise ImageMediaError("image_source_unavailable", "暂时无法读取原图，请稍后重试。", status_code=503, retryable=True) from exc
        result = _build_unlocked(source, file_id, row["sha256"])
        with closing(_connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            record_image_media(conn, file_id, result, lease_token=token)
            conn.commit()
            return _metadata(conn.execute("SELECT * FROM image_assets WHERE file_id=?", (file_id,)).fetchone())
    except ImageMediaError as exc:
        if token is not None:
            with closing(_connect()) as conn:
                asset = conn.execute("SELECT attempts FROM image_assets WHERE file_id=? AND lease_token=?", (file_id, token)).fetchone()
                if asset:
                    delay = min(900, 30 * (2 ** min(5, max(0, asset["attempts"] - 1))))
                    retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="seconds") if exc.retryable else None
                    conn.execute("UPDATE image_assets SET status='failed',error_code=?,retryable=?,retry_after=?,lease_until=NULL,lease_token=NULL,updated_at=? WHERE file_id=? AND lease_token=?", (exc.code, int(exc.retryable), retry_at, now_iso(), file_id, token))
                    conn.commit()
        raise
    finally:
        if source is not None:
            source.unlink(missing_ok=True)
        _WORK.release()


def _authorize(file_id, user, album_id=None, *, original=False):
    from app.album_service import get_album_item
    from app.files import can_manage_file, is_expired
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
        actor = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone() if user is not None else None
    if row is None or row["status"] != "active" or row["purpose"] != "image" or is_expired(row):
        raise HTTPException(404, detail={"error": "image_not_found", "message": "找不到这张图片。"}, headers=NO_STORE)
    if album_id is None:
        if not can_manage_file(row, actor):
            raise HTTPException(404, detail={"error": "image_not_found", "message": "找不到这张图片。"}, headers=NO_STORE)
    else:
        item = get_album_item(album_id, file_id, actor)
        if original and not (item["allow_original"] or can_manage_file(row, actor)):
            raise HTTPException(403, detail={"error": "original_not_shared", "message": "上传者未开放原图下载。"}, headers=NO_STORE)
    return row


async def prepare_image(file_id, user, *, retry=False):
    _authorize(file_id, user)
    try:
        result = await run_image_work(_ensure_image, file_id, retry=retry)
    except ImageMediaError as exc:
        raise _http_error(exc) from exc
    _authorize(file_id, user)
    return result


def _release_pin(key):
    with _CACHE_LOCK:
        _PINS[key] = max(0, _PINS.get(key, 0) - 1)
        if not _PINS[key]:
            _PINS.pop(key, None)
        for directory in list(_DELETE_AFTER_USE):
            if not _is_pinned(directory):
                try:
                    _safe_remove_tree(Path(directory))
                    _DELETE_AFTER_USE.remove(directory)
                except OSError:
                    pass


async def image_response(file_id, variant, request: Request, user=None, *, album_id=None):
    if variant not in VARIANTS:
        raise HTTPException(404, detail={"error": "variant_not_found", "message": "找不到这个图片预览。"}, headers=NO_STORE)
    _authorize(file_id, user, album_id)
    try:
        await run_image_work(_ensure_image, file_id)
    except ImageMediaError as exc:
        raise _http_error(exc) from exc
    _authorize(file_id, user, album_id)  # Revocation while decoding must take effect too.
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM image_variants WHERE file_id=? AND pipeline_version=? AND variant=?", (file_id, PIPELINE_VERSION, variant)).fetchone()
    if row is None:
        raise HTTPException(503, detail={"error": "media_unavailable", "message": "预览暂不可用，请重试。"}, headers=NO_STORE)
    path = _cache_path(row["object_key"])
    with _CACHE_LOCK:
        if not path.is_file():
            raise HTTPException(503, detail={"error": "media_evicted", "message": "预览缓存已更新，请重试。"}, headers=NO_STORE)
        key = str(path)
        _PINS[key] = _PINS.get(key, 0) + 1
        try:
            path.touch()
        except OSError:
            pass
    headers = {**NO_STORE, "ETag": '"' + row["sha256"] + '"'}
    return FileResponse(path, media_type=row["mime_type"], headers=headers, background=BackgroundTask(_release_pin, key))


async def original_response(file_id, request: Request, user=None, *, album_id=None):
    row = _authorize(file_id, user, album_id, original=True)
    backend = get_backend(row["storage_backend"])
    cleanup = None
    try:
        if isinstance(backend, LocalTempBackend) or (getattr(backend, "use_mount", False) and hasattr(backend, "_path")):
            path = backend._path(row["object_key"])
        else:
            pending = asyncio.create_task(asyncio.to_thread(_materialize, row))
            try:
                path = await asyncio.shield(pending)
            except asyncio.CancelledError:
                # A disconnected download must not leave a completed SDK spool behind.
                try:
                    orphan = await pending
                    orphan.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
            cleanup = path
        _authorize(file_id, user, album_id, original=True)
        if not path.is_file():
            raise ImageMediaError("image_source_unavailable", "暂时无法读取原图。", status_code=503, retryable=True)
        headers = {**NO_STORE, "Content-Disposition": content_disposition(row["mime_type"], row["stored_filename"], force_attachment=True)}
        return FileResponse(path, media_type=row["mime_type"] or "application/octet-stream", headers=headers,
                            background=BackgroundTask(path.unlink, missing_ok=True) if cleanup else None)
    except HTTPException:
        if cleanup:
            cleanup.unlink(missing_ok=True)
        raise
    except Exception as exc:
        if cleanup:
            cleanup.unlink(missing_ok=True)
        error = exc if isinstance(exc, ImageMediaError) else ImageMediaError("image_source_unavailable", "暂时无法读取原图，请稍后重试。", status_code=503, retryable=True)
        raise _http_error(error) from exc


def discard_image_media(file_id: str) -> None:
    directory = _file_dir(file_id)
    with _CACHE_LOCK:
        if _is_pinned(directory):
            _DELETE_AFTER_USE.add(str(directory))
        else:
            try:
                _safe_remove_tree(directory)
            except OSError:
                _DELETE_AFTER_USE.add(str(directory))
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM image_variants WHERE file_id=?", (file_id,))
        conn.execute("UPDATE image_assets SET status='failed',error_code='image_deleted',retryable=0,lease_until=NULL,lease_token=NULL,updated_at=? WHERE file_id=?", (now_iso(), file_id))
        conn.commit()
