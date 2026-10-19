"""Image pipeline and private storage regressions, using only isolated temp data."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import gc
import hashlib
import io
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
SANDBOX = tempfile.TemporaryDirectory(prefix="filehub-media-tests-")
os.environ.update(DATABASE_PATH=str(Path(SANDBOX.name) / "bootstrap.sqlite"),
                  LOCAL_TEMP_PATH=str(Path(SANDBOX.name) / "local"),
                  SESSION_SECRET="isolated-media-test-secret-" * 3,
                  COS_ENABLED="false", COS_SV_ENABLED="false", R2_ENABLED="false",
                  ADMIN_PASSWORD_HASH="unused-test-password")

from fastapi import HTTPException, Request, UploadFile
from PIL import Image, features
from starlette.datastructures import Headers
from app import album_service as albums
from app import files
from app import image_media as media
from app.config import settings
from app.db import connect, init_db
from app.security import now_iso
from app.storage.private_media import PrivateMediaBackend


class ImageMedia(unittest.TestCase):
    def setUp(self):
        self.path = Path(SANDBOX.name) / self._testMethodName
        self.path.mkdir()
        limits = dict(database_path=self.path / "gallery.sqlite", local_temp_path=self.path / "local",
                      image_max_pixels=40_000_000, image_max_dimension=16384, image_max_frames=200,
                      image_max_decoded_pixels=100_000_000, max_image_bytes=50 * 1024**2,
                      image_cache_max_bytes=256 * 1024**2, private_media_max_capacity_bytes=512 * 1024**2,
                      media_disk_reserve_bytes=512 * 1024**2)
        for key, value in limits.items():
            object.__setattr__(settings, key, value)
        init_db()
        with closing(connect()) as conn:
            for name in ("alice", "bob"):
                conn.execute("INSERT INTO users(username,password_hash,status,role,created_at,updated_at) VALUES (?,'unused','active','developer',?,?)", (name, now_iso(), now_iso()))
            conn.commit()
            self.alice = dict(conn.execute("SELECT * FROM users WHERE username='alice'").fetchone())
            self.bob = dict(conn.execute("SELECT * FROM users WHERE username='bob'").fetchone())
        self.request = Request({"type": "http", "method": "GET", "path": "/media/test", "headers": [], "query_string": b"", "client": ("127.0.0.1", 1)})

    def tearDown(self):
        self.assertFalse(media._PINS, "FileResponse test must release its cache pin")
        gc.collect()

    @contextmanager
    def setting(self, name, value):
        previous = getattr(settings, name)
        object.__setattr__(settings, name, value)
        try:
            yield
        finally:
            object.__setattr__(settings, name, previous)

    def image(self, fmt="PNG", size=(80, 40), mode="RGB", color="blue", **save_options):
        with Image.new(mode, size, color) as source:
            output = io.BytesIO()
            source.save(output, fmt, **save_options)
            return output.getvalue()

    def upload(self, data=None, *, user=None, filename="image.png", declared="application/octet-stream", **kwargs):
        data = self.image() if data is None else data
        upload = UploadFile(filename=filename, file=io.BytesIO(data), headers=Headers({"content-type": declared}))
        try:
            return asyncio.run(files.save_upload(upload, (user or self.alice)["id"], purpose="image", storage_policy="private_media", **kwargs))
        finally:
            upload.file.close()

    def row(self, file_id):
        with closing(connect()) as conn:
            return dict(conn.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone())

    def asset(self, file_id):
        with closing(connect()) as conn:
            return dict(conn.execute("SELECT * FROM image_assets WHERE file_id=?", (file_id,)).fetchone())

    def variants(self, file_id):
        with closing(connect()) as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM image_variants WHERE file_id=?", (file_id,))]

    def original_path(self, file_id):
        return PrivateMediaBackend()._path(self.row(file_id)["object_key"])

    def denied(self, expected_status, function, *args, **kwargs):
        with self.assertRaises(HTTPException) as caught:
            function(*args, **kwargs)
        self.assertEqual(expected_status, caught.exception.status_code, caught.exception.detail)
        return caught.exception

    def async_denied(self, expected_status, function, *args, **kwargs):
        return self.denied(expected_status, asyncio.run, function(*args, **kwargs))

    def response(self, function, *args, **kwargs):
        response = asyncio.run(function(*args, **kwargs))
        if response.background:
            asyncio.run(response.background())
        return response

    def assert_clean_failure(self):
        with closing(connect()) as conn:
            for table in ("files", "image_assets", "image_variants", "gallery_upload_reservations"):
                self.assertEqual(0, conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], table)
            backend = conn.execute("SELECT * FROM storage_backends WHERE name='private_media'").fetchone()
            self.assertEqual(0, backend["used_bytes_cache"])
            self.assertEqual(0, backend["reserved_bytes"])
        for directory in ("uploads", "media-originals", "media-cache"):
            self.assertEqual([], [p for p in (self.path / directory).rglob("*") if p.is_file()], directory)

    def legacy(self, data=None, *, backend="private_media", expected_hash=None):
        data = self.image() if data is None else data
        file_id = files.new_file_id()
        key = files.object_key("image", file_id, "legacy.png")
        if backend == "private_media":
            path = PrivateMediaBackend()._path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        with closing(connect()) as conn:
            conn.execute("""INSERT INTO files(file_id,owner_id,original_filename,stored_filename,size_bytes,sha256,storage_backend,object_key,
                mime_type,visibility,purpose,status,link_enabled,uploaded_at,updated_at)
                VALUES (?,?,'legacy.png','legacy.png',?,?,?,?,'image/png','unlisted','image','active',0,?,?)""",
                (file_id, self.alice["id"], len(data), expected_hash or hashlib.sha256(data).hexdigest(), backend, key, now_iso(), now_iso()))
            conn.commit()
        return file_id

    def shared_image(self, *, allow_original=False):
        album = albums.create_album(self.alice, "Shared", kind="shared", view_policy="public", contribute_policy="authenticated", review_required=False)
        result = self.upload(user=self.bob)
        albums.add_items(album["album_id"], self.bob, [result["file_id"]], audience_cap="public", allow_original=allow_original)
        return album["album_id"], result["file_id"]

    def test_exif_orientation_gps_removed_but_original_hash_preserved(self):
        exif = Image.Exif()
        exif[274] = 6
        exif[315] = "private artist"
        exif[34853] = {1: "N", 2: (30.0, 20.0, 10.0), 3: "E", 4: (120.0, 10.0, 5.0)}
        data = self.image("JPEG", size=(120, 60), exif=exif)
        result = self.upload(data, filename="orientation.jpg")
        self.assertEqual((60, 120), (result["width"], result["height"]))
        self.assertEqual("image/jpeg", result["mime_type"])
        self.assertEqual(data, self.original_path(result["file_id"]).read_bytes())
        self.assertEqual(hashlib.sha256(data).hexdigest(), self.row(result["file_id"])["sha256"])
        for variant in self.variants(result["file_id"]):
            with Image.open(media._cache_path(variant["object_key"])) as output:
                self.assertEqual((60, 120), output.size)
                self.assertFalse(output.getexif())
                self.assertNotIn("xmp", output.info)

    def test_transparency_and_preview_bounds(self):
        result = self.upload(self.image(size=(2000, 1000), mode="RGBA", color=(255, 30, 90, 80)))
        expected = {"thumb": (480, 240), "preview": (1600, 800)}
        for variant in self.variants(result["file_id"]):
            with Image.open(media._cache_path(variant["object_key"])) as output:
                self.assertEqual(expected[variant["variant"]], output.size)
                self.assertIn("A", output.getbands())
                self.assertEqual(80, output.getpixel((10, 10))[3])

    def test_animation_validates_all_frames_and_uses_static_first_frame(self):
        frames = [Image.new("RGB", (20, 10), color) for color in ("red", "blue", "green")]
        output = io.BytesIO()
        frames[0].save(output, "GIF", save_all=True, append_images=frames[1:], duration=40, loop=0)
        for frame in frames:
            frame.close()
        result = self.upload(output.getvalue(), filename="animated.gif")
        self.assertEqual(3, self.asset(result["file_id"])["frame_count"])
        for variant in self.variants(result["file_id"]):
            with Image.open(media._cache_path(variant["object_key"])) as preview:
                self.assertEqual(1, getattr(preview, "n_frames", 1))
                self.assertGreater(preview.convert("RGB").getpixel((0, 0))[0], 200)

    @unittest.skipUnless(features.check("avif"), "Pillow build has no AVIF codec")
    def test_avif_detected_and_decoded(self):
        result = self.upload(self.image("AVIF"), filename="image.avif", declared="text/plain")
        self.assertEqual("image/avif", result["mime_type"])
        self.assertEqual((80, 40), (result["width"], result["height"]))

    def test_fake_corrupt_and_truncated_images_leave_no_artifacts(self):
        inputs = [b'<svg xmlns="http://www.w3.org/2000/svg"/>', b"not an image", self.image("JPEG")[:100], self.image()[:-20]]
        for payload in inputs:
            with self.subTest(length=len(payload)):
                self.denied(422, self.upload, payload, declared="image/png")
                self.assert_clean_failure()

    def test_dimension_pixel_frame_and_total_decode_budgets(self):
        data = self.image()
        for name, value, code in [("image_max_dimension", 60, "image_dimensions_exceeded"), ("image_max_pixels", 100, "image_pixels_exceeded")]:
            with self.subTest(setting=name), self.setting(name, value):
                error = self.denied(422, self.upload, data)
                self.assertEqual(code, error.detail["error"])
                self.assert_clean_failure()
        one, two = Image.new("RGB", (20, 10), "red"), Image.new("RGB", (20, 10), "blue")
        output = io.BytesIO()
        one.save(output, "GIF", save_all=True, append_images=[two], duration=40)
        one.close()
        two.close()
        for name, value, code in [("image_max_frames", 1, "image_frames_exceeded"), ("image_max_decoded_pixels", 300, "image_decoded_pixels_exceeded")]:
            with self.subTest(setting=name), self.setting(name, value):
                error = self.denied(422, self.upload, output.getvalue())
                self.assertEqual(code, error.detail["error"])
                self.assert_clean_failure()

    def test_byte_limit_is_enforced_while_streaming(self):
        with self.setting("max_image_bytes", 10):
            self.denied(413, self.upload)
        self.assert_clean_failure()

    def test_cache_limit_never_falls_back_to_original(self):
        with self.setting("image_cache_max_bytes", 1):
            error = self.denied(507, self.upload)
            self.assertEqual("media_cache_full", error.detail["error"])
        self.assert_clean_failure()

    def test_real_disk_reserve_blocks_staging_and_original_copy(self):
        with patch("app.storage.disk_space.shutil.disk_usage", return_value=SimpleNamespace(free=0)):
            self.denied(507, self.upload)
        self.assert_clean_failure()

    def test_failed_partial_original_write_releases_reservation_and_cache(self):
        original_put = PrivateMediaBackend.put_file
        def partial(backend, source, key, mime):
            original_put(backend, source, key, mime)
            raise OSError("simulated device failure after copy")
        with patch.object(PrivateMediaBackend, "put_file", partial):
            with self.assertRaises(OSError):
                self.upload()
        self.assert_clean_failure()

    def test_concurrent_uploads_reserve_capacity_atomically(self):
        payload = self.image()
        with closing(connect()) as conn:
            conn.execute("UPDATE storage_backends SET max_capacity_bytes=? WHERE name='private_media'", (len(payload),))
            conn.commit()
        def attempt(_):
            try:
                return self.upload(payload)
            except HTTPException as exc:
                return exc.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(1, sum(isinstance(result, dict) for result in results))
        self.assertIn(507, results)
        with closing(connect()) as conn:
            state = conn.execute("SELECT * FROM storage_backends WHERE name='private_media'").fetchone()
            self.assertEqual(len(payload), state["used_bytes_cache"])
            self.assertEqual(0, state["reserved_bytes"])
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM gallery_upload_reservations").fetchone()[0])
        self.assertEqual(1, len([p for p in (self.path / "media-originals").rglob("*") if p.is_file()]))

    def test_unauthorized_legacy_request_does_not_start_processing(self):
        file_id = self.legacy()
        self.async_denied(404, media.image_response, file_id, "thumb", self.request, self.bob)
        with closing(connect()) as conn:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM image_assets").fetchone()[0])
        response = self.response(media.image_response, file_id, "thumb", self.request, self.alice)
        self.assertEqual(200, response.status_code)
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual("ready", self.asset(file_id)["status"])
        self.assertEqual([], list((self.path / "media-cache" / ".source").glob("*")))

    def test_cloud_legacy_uses_private_sdk_reader_and_no_redirect(self):
        data = self.image()
        file_id = self.legacy(data, backend="cos")
        backend = Mock()
        backend.open_reader.side_effect = lambda _: io.BytesIO(data)
        backend.get_public_url.side_effect = AssertionError("Public URL must not be used")
        with patch.object(media, "get_backend", return_value=backend):
            response = self.response(media.image_response, file_id, "preview", self.request, self.alice)
        self.assertEqual(200, response.status_code)
        self.assertNotIn("location", response.headers)
        backend.open_reader.assert_called_once()
        backend.get_public_url.assert_not_called()

    def test_source_hash_mismatch_is_persisted_and_owner_retry_recovers(self):
        data = self.image()
        file_id = self.legacy(data, expected_hash="0" * 64)
        error = self.async_denied(422, media.prepare_image, file_id, self.alice)
        self.assertEqual("image_source_changed", error.detail["error"])
        self.assertEqual("failed", self.asset(file_id)["status"])
        self.assertEqual(0, self.asset(file_id)["retryable"])
        with closing(connect()) as conn:
            conn.execute("UPDATE files SET sha256=? WHERE file_id=?", (hashlib.sha256(data).hexdigest(), file_id))
            conn.commit()
        self.assertEqual("ready", asyncio.run(media.prepare_image(file_id, self.alice, retry=True))["media_status"])
        self.assertEqual(2, self.asset(file_id)["attempts"])

    def test_live_processing_lease_waits_and_expired_lease_recovers(self):
        result = self.upload()
        file_id = result["file_id"]
        media._safe_remove_tree(media._file_dir(file_id))
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="seconds")
        with closing(connect()) as conn:
            conn.execute("UPDATE image_assets SET status='processing',lease_until=?,lease_token='old-worker' WHERE file_id=?", (future, file_id))
            conn.commit()
        self.async_denied(503, media.prepare_image, file_id, self.alice, retry=True)
        with closing(connect()) as conn:
            conn.execute("UPDATE image_assets SET lease_until='2000-01-01T00:00:00+00:00' WHERE file_id=?", (file_id,))
            conn.commit()
        self.assertEqual("ready", asyncio.run(media.prepare_image(file_id, self.alice))["media_status"])

    def test_original_download_requires_uploader_consent_even_for_album_owner(self):
        album_id, file_id = self.shared_image()
        self.async_denied(403, media.original_response, file_id, self.request, self.alice, album_id=album_id)
        self.async_denied(403, media.original_response, file_id, self.request, album_id=album_id)
        owner_response = self.response(media.original_response, file_id, self.request, self.bob, album_id=album_id)
        self.assertTrue(owner_response.headers["content-disposition"].startswith("attachment"))
        albums.update_item(album_id, file_id, self.bob, allow_original=True)
        shared = self.response(media.original_response, file_id, self.request, album_id=album_id)
        self.assertEqual(200, shared.status_code)
        self.assertIn("no-store", shared.headers["cache-control"])
        albums.update_item(album_id, file_id, self.bob, allow_original=False)
        self.async_denied(403, media.original_response, file_id, self.request, album_id=album_id)

    def test_withdrawal_during_processing_is_rechecked_before_response(self):
        album_id, file_id = self.shared_image()
        ensure = media._ensure_image
        def revoke_after_processing(*args, **kwargs):
            result = ensure(*args, **kwargs)
            albums.remove_item(album_id, file_id, self.bob)
            return result
        with patch.object(media, "_ensure_image", side_effect=revoke_after_processing):
            self.async_denied(404, media.image_response, file_id, "thumb", self.request, album_id=album_id)

    def test_cached_media_rechecks_membership_for_head_range_and_etag(self):
        album = albums.create_album(self.alice, "Restricted", kind="shared")
        invite = albums.invite_member(album["album_id"], self.alice, "bob")
        albums.respond_invitation(invite["invitation_id"], self.bob)
        file_id = self.upload()["file_id"]
        albums.add_items(album["album_id"], self.alice, [file_id])
        response = self.response(media.image_response, file_id, "thumb", self.request, self.bob, album_id=album["album_id"])
        albums.remove_member(album["album_id"], self.bob["id"], self.alice)
        for method, headers in [("GET", []), ("HEAD", []), ("GET", [(b"range", b"bytes=0-9")]), ("GET", [(b"if-none-match", response.headers["etag"].encode())])]:
            request = Request({**self.request.scope, "method": method, "headers": headers})
            self.async_denied(404, media.image_response, file_id, "thumb", request, self.bob, album_id=album["album_id"])

    def test_one_original_multiple_albums_and_delete_invalidation(self):
        first, file_id = self.shared_image()
        second = albums.create_album(self.bob, "Other")
        albums.add_items(second["album_id"], self.bob, [file_id])
        self.assertEqual(1, len([p for p in (self.path / "media-originals").rglob("*") if p.is_file()]))
        albums.delete_album(second["album_id"], self.bob)
        self.assertTrue(self.original_path(file_id).is_file())
        self.assertTrue(files.delete_file(file_id))
        self.assertTrue(files.delete_file(file_id))
        self.async_denied(404, media.image_response, file_id, "thumb", self.request, album_id=first)
        self.assertFalse(media._file_dir(file_id).exists())
        with closing(connect()) as conn:
            self.assertEqual(0, conn.execute("SELECT used_bytes_cache FROM storage_backends WHERE name='private_media'").fetchone()[0])
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM image_variants").fetchone()[0])

    def test_cache_pin_defers_deletion_until_response_finishes(self):
        file_id = self.upload()["file_id"]
        response = asyncio.run(media.image_response(file_id, "thumb", self.request, self.alice))
        path = Path(response.path)
        files.delete_file(file_id)
        self.assertTrue(path.is_file())
        self.async_denied(404, media.image_response, file_id, "thumb", self.request, self.alice)
        asyncio.run(response.background())
        self.assertFalse(path.exists())

    def test_stale_reservation_reaps_only_unpublished_original(self):
        published = self.upload()["file_id"]
        published_path = self.original_path(published)
        orphan_id = "lf_interrupted_upload"
        orphan_key = files.object_key("image", orphan_id, "orphan.png")
        orphan_path = PrivateMediaBackend()._path(orphan_key)
        orphan_path.parent.mkdir(parents=True)
        orphan_path.write_bytes(b"orphan")
        with closing(connect()) as conn:
            for file_id, key in [(published, self.row(published)["object_key"]), (orphan_id, orphan_key)]:
                conn.execute("INSERT INTO gallery_upload_reservations(file_id,object_key,size_bytes,created_at,expires_at) VALUES (?,?,6,?,'2000-01-01T00:00:00+00:00')", (file_id, key, now_iso()))
            conn.commit()
        self.upload()
        self.assertTrue(published_path.is_file())
        self.assertFalse(orphan_path.exists())
        with closing(connect()) as conn:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM gallery_upload_reservations").fetchone()[0])

    def test_private_direct_link_remains_independent_and_no_store(self):
        file_id = self.upload()["file_id"]
        album = albums.create_album(self.alice, "Public", view_policy="public")
        albums.add_items(album["album_id"], self.alice, [file_id], audience_cap="public")
        self.denied(404, files.serve_file, file_id, self.request)
        with closing(connect()) as conn:
            conn.execute("UPDATE files SET link_enabled=1 WHERE file_id=?", (file_id,))
            conn.commit()
        response = files.serve_file(file_id, self.request)
        self.assertIn("no-store", response.headers["cache-control"])
        with closing(connect()) as conn:
            conn.execute("UPDATE files SET link_enabled=0 WHERE file_id=?", (file_id,))
            conn.commit()
        self.denied(404, files.serve_file, file_id, self.request)

    def test_cancelled_remote_original_download_removes_completed_spool(self):
        data = self.image()
        file_id = self.legacy(data, backend="cos")
        started, complete = threading.Event(), threading.Event()
        spool = self.path / "download.source"
        backend = SimpleNamespace(use_mount=False)
        def slow_materialize(_):
            started.set()
            if not complete.wait(5):
                raise RuntimeError("test release not signaled")
            spool.write_bytes(data)
            return spool
        async def cancel_download():
            task = asyncio.create_task(media.original_response(file_id, self.request, self.alice))
            await asyncio.to_thread(started.wait, 5)
            task.cancel()
            complete.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        with patch.object(media, "get_backend", return_value=backend), patch.object(media, "_materialize", side_effect=slow_materialize):
            asyncio.run(cancel_download())
        self.assertFalse(spool.exists())


if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False).result
    gc.collect()
    SANDBOX.cleanup()
    raise SystemExit(0 if result.wasSuccessful() else 1)
