"""Isolated album authorization/migration regression tests. Never uses production DB."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import gc
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
SANDBOX = tempfile.TemporaryDirectory(prefix="filehub-album-tests-")
os.environ.update(DATABASE_PATH=str(Path(SANDBOX.name) / "bootstrap.sqlite"),
                  LOCAL_TEMP_PATH=str(Path(SANDBOX.name) / "local"),
                  SESSION_SECRET="isolated-gallery-test-secret-" * 3,
                  COS_ENABLED="false", COS_SV_ENABLED="false", R2_ENABLED="false",
                  ADMIN_PASSWORD_HASH="unused-test-password")

from fastapi import HTTPException
from app import album_service as service
from app.config import settings
from app.db import connect, init_db
from app.security import now_iso


class AlbumPermissions(unittest.TestCase):
    def setUp(self):
        self.path = Path(SANDBOX.name) / self._testMethodName
        self.path.mkdir()
        object.__setattr__(settings, "database_path", self.path / "gallery.sqlite")
        object.__setattr__(settings, "local_temp_path", self.path / "local")
        init_db()
        self.users = {}
        with closing(connect()) as conn:
            for username, status, role in [("alice", "active", "developer"), ("bob", "active", "developer"),
                                           ("carol", "active", "developer"), ("pending", "pending", "developer"),
                                           ("supervisor", "active", "admin")]:
                conn.execute("INSERT INTO users(username,password_hash,status,role,created_at,updated_at) VALUES (?,'unused',?,?,?,?)", (username, status, role, now_iso(), now_iso()))
                self.users[username] = dict(conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone())
            conn.commit()
        self.alice, self.bob, self.carol = [self.users[name] for name in ("alice", "bob", "carol")]

    def tearDown(self):
        gc.collect()

    def denied(self, expected_status, function, *args, **kwargs):
        with self.assertRaises(HTTPException) as caught:
            function(*args, **kwargs)
        self.assertEqual(expected_status, caught.exception.status_code, caught.exception.detail)

    def file(self, user, suffix="one", *, purpose="image", expires_at=None, link_enabled=0, public_url=None):
        file_id = "lf_" + user["username"] + "_" + suffix
        with closing(connect()) as conn:
            conn.execute(
                """INSERT INTO files(file_id,owner_id,original_filename,stored_filename,size_bytes,sha256,storage_backend,
                object_key,mime_type,visibility,purpose,status,link_enabled,public_url,expires_at,uploaded_at,updated_at)
                VALUES (?,?,?,'image.png',123,?,'private_media',?,'image/png','unlisted',?,'active',?,?,?,?,?)""",
                (file_id, user["id"], suffix + ".png", "a" * 64, f"images/test/{file_id}/image.png", purpose,
                 link_enabled, public_url, expires_at, now_iso(), now_iso()),
            )
            conn.commit()
        return file_id

    def shared(self, **kwargs):
        return service.create_album(self.alice, "共同相册", kind="shared", **kwargs)

    def invite(self, album, target, **kwargs):
        invitation = service.invite_member(album["album_id"], self.alice, target["username"], **kwargs)
        service.respond_invitation(invitation["invitation_id"], target)
        return invitation

    def test_migration_is_additive_and_does_not_republish_legacy_link(self):
        file_id = self.file(self.alice, public_url="https://file.example/i/legacy", link_enabled=0)
        with closing(connect()) as conn:
            before = dict(conn.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone())
        init_db()
        init_db()
        with closing(connect()) as conn:
            after = dict(conn.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone())
            self.assertEqual(before, after)
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM gallery_schema_migrations").fetchone()[0])
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM album_items").fetchone()[0])
        with closing(service.connect_gallery()) as conn:
            self.assertEqual(1, conn.execute("PRAGMA foreign_keys").fetchone()[0])

    def test_multiple_personal_albums_are_private_and_kind_filter_precedes_pagination(self):
        first = service.create_album(self.alice, "第一册")
        service.create_album(self.alice, "第二册")
        self.shared()
        self.denied(404, service.get_album, first["album_id"], self.bob)
        self.denied(404, service.get_album, first["album_id"])
        self.assertEqual(2, service.list_albums(self.alice, scope="mine", kind="personal", page_size=1)["total"])
        self.assertEqual(1, service.list_albums(self.alice, scope="mine", kind="shared", page_size=1)["total"])
        self.denied(422, service.invite_member, first["album_id"], self.alice, "bob", can_contribute=True)

    def test_contributor_does_not_implicitly_receive_browse_permission(self):
        album = self.shared()
        self.invite(album, self.bob, can_view=False, can_contribute=True)
        caps = service.album_capabilities(album["album_id"], self.bob)
        self.assertTrue(caps["can_contribute"])
        self.assertFalse(caps["can_view"])
        self.assertFalse(caps["can_manage"])
        image = self.file(self.bob)
        item = service.add_items(album["album_id"], self.bob, [image])["items"][0]
        self.assertEqual("pending", item["status"])
        self.denied(404, service.list_album_items, album["album_id"], self.bob)
        self.assertEqual(image, service.get_album_item(album["album_id"], image, self.bob)["file_id"])
        minimal = service.list_contributable_albums(self.bob)[0]
        self.assertEqual(0, minimal["item_count"])
        self.assertIsNone(minimal["cover_url"])
        self.assertFalse(minimal["capabilities"]["can_view"])

    def test_management_does_not_imply_browse_contribution_or_acl_control(self):
        album = self.shared()
        self.invite(album, self.carol, can_view=False, can_manage=True)
        updated = service.update_album(album["album_id"], self.carol, title="新标题")
        self.assertEqual("新标题", updated["title"])
        self.assertEqual(0, updated["item_count"])
        self.denied(404, service.get_album, album["album_id"], self.carol)
        self.denied(403, service.update_album, album["album_id"], self.carol, view_policy="public")
        self.denied(403, service.invite_member, album["album_id"], self.carol, "bob")
        self.denied(403, service.add_items, album["album_id"], self.carol, [self.file(self.carol)])

    def test_open_contribution_requires_active_account_and_honors_block(self):
        album = self.shared(view_policy="public", contribute_policy="authenticated", review_required=False)
        image = self.file(self.bob)
        self.denied(401, service.add_items, album["album_id"], None, [image])
        self.denied(403, service.add_items, album["album_id"], self.users["pending"], [image])
        self.assertEqual("accepted", service.add_items(album["album_id"], self.bob, [image])["items"][0]["status"])
        service.remove_member(album["album_id"], self.bob["id"], self.alice, block=True)
        self.denied(403, service.add_items, album["album_id"], self.bob, [image])
        self.assertEqual([], service.list_contributable_albums(self.bob))

    def test_only_uploader_can_attach_existing_files_and_batch_is_atomic(self):
        album = self.shared(contribute_policy="authenticated")
        own, others = self.file(self.alice), self.file(self.bob)
        self.denied(404, service.add_items, album["album_id"], self.alice, [own, others])
        self.assertEqual(0, service.list_album_items(album["album_id"], self.alice)["total"])
        admin_album = service.create_album(self.users["supervisor"], "Admin album")
        self.denied(404, service.add_items, admin_album["album_id"], self.users["supervisor"], [others])

    def test_public_upgrade_respects_each_uploaders_consent_and_cover(self):
        album = self.shared()
        self.invite(album, self.bob, can_view=True, can_contribute=True)
        image = self.file(self.bob)
        service.add_items(album["album_id"], self.bob, [image])
        service.update_item(album["album_id"], image, self.alice, status="accepted")
        service.set_cover(album["album_id"], self.alice, image)
        service.update_album(album["album_id"], self.alice, view_policy="public", listed=True)
        guest = service.get_album(album["album_id"])
        self.assertEqual(0, guest["item_count"])
        self.assertIsNone(guest["cover_file_id"])
        self.assertIsNone(guest["cover_url"])
        self.assertEqual(0, service.list_album_items(album["album_id"])["total"])
        self.denied(404, service.get_album_item, album["album_id"], image, self.carol)
        self.denied(403, service.update_item, album["album_id"], image, self.alice, audience_cap="public")
        self.denied(403, service.update_item, album["album_id"], image, self.alice, allow_original=True)
        service.update_item(album["album_id"], image, self.bob, audience_cap="public")
        shown = service.get_album_item(album["album_id"], image)
        self.assertIsNone(shown["original_url"])
        self.assertEqual(1, service.get_album(album["album_id"])["item_count"])
        service.update_item(album["album_id"], image, self.bob, allow_original=True)
        self.assertIsNotNone(service.get_album_item(album["album_id"], image)["original_url"])

    def test_explicit_viewer_can_see_restricted_cap_in_public_album(self):
        album = self.shared(view_policy="public")
        image = self.file(self.alice)
        service.add_items(album["album_id"], self.alice, [image], audience_cap="restricted")
        self.invite(album, self.bob, can_view=True)
        self.assertEqual(1, service.list_album_items(album["album_id"], self.bob)["total"])
        service.remove_member(album["album_id"], self.bob["id"], self.alice)
        self.assertEqual(0, service.list_album_items(album["album_id"], self.bob)["total"])
        self.denied(404, service.get_album_item, album["album_id"], image, self.bob)

    def test_invitation_is_bound_revocable_expiring_and_replay_cannot_restore_access(self):
        album = self.shared()
        invitation = service.invite_member(album["album_id"], self.alice, "bob", can_view=True)
        self.denied(404, service.respond_invitation, invitation["invitation_id"], self.carol)
        service.respond_invitation(invitation["invitation_id"], self.bob)
        service.remove_member(album["album_id"], self.bob["id"], self.alice)
        service.respond_invitation(invitation["invitation_id"], self.bob)
        self.assertFalse(service.album_capabilities(album["album_id"], self.bob)["can_view"])
        revoked = service.invite_member(album["album_id"], self.alice, "bob")
        service.revoke_invitation(album["album_id"], revoked["invitation_id"], self.alice)
        self.denied(409, service.respond_invitation, revoked["invitation_id"], self.bob)
        expired = service.invite_member(album["album_id"], self.alice, "bob")
        with closing(connect()) as conn:
            conn.execute("UPDATE album_invitations SET expires_at='2000-01-01T00:00:00+00:00' WHERE invitation_id=?", (expired["invitation_id"],))
            conn.commit()
        self.denied(410, service.respond_invitation, expired["invitation_id"], self.bob)
        declined = service.invite_member(album["album_id"], self.alice, "carol")
        service.respond_invitation(declined["invitation_id"], self.carol, accept=False)
        self.assertFalse(service.album_capabilities(album["album_id"], self.carol)["can_view"])

    def test_members_cannot_change_owner_or_reinstate_revoked_user_without_consent(self):
        album = self.shared()
        self.invite(album, self.bob, can_view=True)
        self.denied(422, service.remove_member, album["album_id"], self.alice["id"], self.alice)
        service.remove_member(album["album_id"], self.bob["id"], self.alice)
        self.denied(409, service.update_member, album["album_id"], self.bob["id"], self.alice, can_view=True, can_contribute=False, can_manage=False)

    def test_uploader_can_withdraw_after_member_revocation_without_deleting_original(self):
        album = self.shared()
        self.invite(album, self.bob, can_view=True, can_contribute=True)
        image = self.file(self.bob)
        service.add_items(album["album_id"], self.bob, [image])
        service.remove_member(album["album_id"], self.bob["id"], self.alice)
        self.assertTrue(service.list_image_associations(image, self.bob)[0]["can_withdraw"])
        service.remove_item(album["album_id"], image, self.bob)
        self.denied(404, service.get_album_item, album["album_id"], image, self.alice)
        self.assertEqual([], service.list_image_associations(image, self.bob))
        with closing(connect()) as conn:
            self.assertEqual("active", conn.execute("SELECT status FROM files WHERE file_id=?", (image,)).fetchone()[0])

    def test_same_original_multiple_albums_and_soft_delete_preserve_file(self):
        image = self.file(self.alice, link_enabled=1, public_url="https://file.test/i/kept")
        first = service.create_album(self.alice, "A")
        second = service.create_album(self.alice, "B")
        with closing(connect()) as conn:
            before = dict(conn.execute("SELECT * FROM files WHERE file_id=?", (image,)).fetchone())
        for album in (first, second):
            service.add_items(album["album_id"], self.alice, [image])
        self.assertEqual(2, len(service.list_image_associations(image, self.alice)))
        service.delete_album(first["album_id"], self.alice)
        with closing(connect()) as conn:
            self.assertEqual(before, dict(conn.execute("SELECT * FROM files WHERE file_id=?", (image,)).fetchone()))
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
        self.assertEqual(1, len(service.list_image_associations(image, self.alice)))
        self.denied(404, service.get_album, first["album_id"], self.alice)

    def test_retry_is_idempotent_and_never_widens_consent(self):
        album = service.create_album(self.alice, "A", view_policy="public")
        image = self.file(self.alice)
        service.add_items(album["album_id"], self.alice, [image, image])
        retry = service.add_items(album["album_id"], self.alice, [image], audience_cap="public", allow_original=True)
        self.assertEqual(0, retry["added_count"])
        self.assertEqual("restricted", retry["items"][0]["audience_cap"])
        self.assertFalse(retry["items"][0]["allow_original"])

    def test_expired_deleted_and_non_image_files_never_enter_gallery(self):
        album = service.create_album(self.alice, "A")
        expired = self.file(self.alice, "expired", expires_at="2000-01-01T00:00:00+00:00")
        text = self.file(self.alice, "text", purpose="other")
        self.denied(404, service.add_items, album["album_id"], self.alice, [expired])
        self.denied(404, service.add_items, album["album_id"], self.alice, [text])
        live = self.file(self.alice, "live")
        service.add_items(album["album_id"], self.alice, [live])
        with closing(connect()) as conn:
            conn.execute("UPDATE files SET status='deleted' WHERE file_id=?", (live,))
            conn.commit()
        self.assertEqual(0, service.get_album(album["album_id"], self.alice)["item_count"])
        self.denied(404, service.get_album_item, album["album_id"], live, self.alice)

    def test_review_sort_and_cover_are_context_authorized(self):
        album = self.shared(view_policy="public", contribute_policy="authenticated")
        first, second = self.file(self.bob, "one"), self.file(self.bob, "two")
        service.add_items(album["album_id"], self.bob, [first, second], audience_cap="public")
        self.denied(403, service.update_item, album["album_id"], first, self.bob, status="accepted")
        self.assertEqual(2, service.list_album_items(album["album_id"], self.alice, status="pending")["total"])
        for file_id in (first, second):
            service.update_item(album["album_id"], file_id, self.alice, status="accepted")
        service.reorder_items(album["album_id"], self.alice, [second, first])
        self.assertEqual([second, first], [i["file_id"] for i in service.list_album_items(album["album_id"])["items"]])
        service.set_cover(album["album_id"], self.alice, first)
        self.assertEqual(first, service.get_album(album["album_id"])["cover_file_id"])
        service.update_item(album["album_id"], first, self.alice, status="rejected")
        self.assertEqual(second, service.get_album(album["album_id"])["cover_file_id"])

    def test_stale_user_mapping_cannot_bypass_account_deactivation(self):
        album = service.create_album(self.alice, "A")
        with closing(connect()) as conn:
            conn.execute("UPDATE users SET status='disabled' WHERE id=?", (self.alice["id"],))
            conn.commit()
        self.denied(403, service.update_album, album["album_id"], self.alice, title="Denied")
        self.denied(404, service.get_album, album["album_id"], self.alice)

    def test_concurrent_adds_produce_one_association(self):
        album = service.create_album(self.alice, "A")
        image = self.file(self.alice)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: service.add_items(album["album_id"], self.alice, [image]), range(4)))
        self.assertEqual(1, sum(r["added_count"] for r in results))
        self.assertEqual(1, service.list_album_items(album["album_id"], self.alice)["total"])

    def test_member_can_leave_without_owner_acl_and_cannot_clear_a_block(self):
        album = self.shared()
        self.invite(album, self.bob, can_view=True, can_contribute=True)
        self.assertTrue(service.album_capabilities(album["album_id"], self.bob)["is_member"])
        self.denied(403, service.remove_member, album["album_id"], self.bob["id"], self.bob, block=True)
        self.denied(403, service.remove_member, album["album_id"], self.carol["id"], self.bob)
        self.assertTrue(service.remove_member(album["album_id"], self.bob["id"], self.bob))
        self.assertFalse(service.album_capabilities(album["album_id"], self.bob)["is_member"])
        self.assertFalse(service.album_capabilities(album["album_id"], self.bob)["can_view"])
        self.denied(404, service.remove_member, album["album_id"], self.bob["id"], self.bob)
        service.remove_member(album["album_id"], self.bob["id"], self.alice, block=True)
        self.denied(404, service.remove_member, album["album_id"], self.bob["id"], self.bob)
        self.denied(422, service.remove_member, album["album_id"], self.alice["id"], self.alice)

    def test_public_or_open_contribution_does_not_make_an_explicit_member(self):
        album = self.shared(view_policy="public", contribute_policy="authenticated")
        self.assertFalse(service.album_capabilities(album["album_id"])["is_member"])
        self.assertFalse(service.album_capabilities(album["album_id"], self.bob)["is_member"])
        self.assertTrue(service.album_capabilities(album["album_id"], self.bob)["can_contribute"])
        self.denied(404, service.remove_member, album["album_id"], self.bob["id"], self.bob)


if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False).result
    gc.collect()
    SANDBOX.cleanup()
    raise SystemExit(0 if result.wasSuccessful() else 1)
