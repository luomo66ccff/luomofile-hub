"""Verify admin-only fragments, form recovery, and queue states using isolated fixtures."""
from html.parser import HTMLParser
from pathlib import Path
import re
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scripts.smoke_test_subscription_create_idempotency as fixture
from fastapi.testclient import TestClient
from unittest.mock import patch
from app.identity_sync import IdentitySyncError
import app.routes.admin as main


class Controls(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []
        self.formats = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and "data-subscription-url" in attrs:
            self.urls.append(attrs["value"])
        if tag == "option" and "data-description" in attrs:
            self.formats.append(attrs["value"])


def main_test():
    fixture.init_db()
    owner = fixture.seed_file()
    started, release = threading.Event(), threading.Event()

    def controlled_sync(_records):
        started.set()
        assert release.wait(5), "Controlled synchronization was never released."
        return fixture.three_region_sync(_records)

    fixture.subscriptions.sync_identity_records = controlled_sync
    with TestClient(fixture.app, follow_redirects=False) as client:
        assert client.get("/admin/subscriptions/status").status_code == 401
        client.cookies.set(fixture.SESSION_COOKIE, fixture.sign_session("luomo"))
        page = client.get("/admin/subscriptions")
        assert page.status_code == 200 and page.headers["referrer-policy"] == "same-origin"
        assert "data-subscription-manager-form" in page.text and "data-subscription-create-pending" not in page.text
        key = re.search(r'name="idempotency_key" value="(scr_[A-Za-z0-9_-]+)"', page.text).group(1)
        form = {"file_id": fixture.FILE_ID, "label": "ui-test <script>test</script>", "expires_in_days": "1", "idempotency_key": key}
        headers = {"origin": "https://file.example.test", "accept": "application/json"}
        invalid_form = dict(form, file_id="missing-file")
        invalid = client.post("/admin/subscriptions/create", data=invalid_form, headers=headers)
        assert invalid.status_code == 400 and invalid.json()["error"] == "invalid_subscription"
        recovery = client.post("/admin/subscriptions/create", data=invalid_form, headers={"origin": "https://file.example.test"})
        assert recovery.status_code == 400 and key in recovery.text and "ui-test &lt;script&gt;test&lt;/script&gt;" in recovery.text

        # The worker is held after claiming: status must say processing, with no URL.
        accepted = client.post("/admin/subscriptions/create", data=form, headers=headers)
        assert accepted.status_code == 202 and accepted.json()["accepted"]
        task_id = accepted.json()["request"]["id"]
        assert "lsub_" not in accepted.text and "url" not in accepted.json()
        assert started.wait(2)
        with fixture.connect() as conn:
            requests_before = conn.execute("SELECT COUNT(*) FROM subscription_create_requests").fetchone()[0]
        for _ in range(3):
            progress = client.get("/admin/subscriptions/status")
            assert progress.status_code == 200 and "no-store" in progress.headers["cache-control"]
            assert progress.json()["tasks"] == [{"id": task_id, "status": "processing"}]
            assert 'data-link-status="processing"' in progress.json()["html"] and "/sub/lsub_" not in progress.json()["html"]
        with fixture.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM subscription_create_requests").fetchone()[0] == requests_before
        replay = client.post("/admin/subscriptions/create", data=form, headers=headers)
        assert replay.status_code == 202 and replay.json()["request"]["id"] == task_id
        conflict = client.post("/admin/subscriptions/create", data=dict(form, label="changed"), headers=headers)
        assert conflict.status_code == 409 and conflict.json()["error"] == "create_conflict"
        release.set()
        active = fixture.wait_for_token(form["label"], "active")
        ready = client.get("/admin/subscriptions/status").json()
        controls = Controls()
        controls.feed(ready["html"])
        assert len(controls.urls) == 1 and "?format=" not in controls.urls[0]
        assert set(controls.formats) == {"mihomo", "clash", "base64", "uri", "sing-box"}
        assert "<script>test</script>" not in ready["html"] and "&lt;script&gt;test&lt;/script&gt;" in ready["html"]
        assert ready["counts"] == {"total": 1, "active": 1, "pending": 0, "failed": 0}
        native_format = client.get("/admin/subscriptions?format=clash")
        native_controls = Controls()
        native_controls.feed(native_format.text)
        assert len(native_controls.urls) == 1 and native_controls.urls[0].endswith("?format=clash")

        with patch.object(main, "reconcile_subscription_identities", side_effect=RuntimeError("private diagnostic")):
            failed_sync = client.post("/admin/subscriptions/reconcile", headers=headers)
            assert failed_sync.status_code == 503 and "private diagnostic" not in failed_sync.text
            assert failed_sync.json()["error"] == "sync_failed"
        revoked = client.post(f"/admin/subscriptions/{active['id']}/revoke", headers=headers)
        assert revoked.status_code == 200
        closed = client.get("/admin/subscriptions/status").json()
        assert "/sub/lsub_" not in closed["html"] and closed["counts"]["active"] == 0
        client.cookies.clear()
        unauthorized = client.get("/admin/subscriptions/status")
        assert unauthorized.status_code == 401 and "/sub/lsub_" not in unauthorized.text
    fixture.temporary.cleanup()
    print("SUBSCRIPTION_MANAGEMENT_UI_SMOKE=PASS (admin-only status, no new keys, recovery, progress, formats, revoke)")


if __name__ == "__main__":
    main_test()
