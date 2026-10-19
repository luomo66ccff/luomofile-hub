import hashlib
import os
import re
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
temporary = tempfile.TemporaryDirectory(prefix="luomofile-subscription-create-", ignore_cleanup_errors=True)
root = Path(temporary.name)
os.environ["DATABASE_PATH"] = str(root / "luomofile.db")
os.environ["LOCAL_TEMP_PATH"] = str(root / "storage")
os.environ["APP_BASE_URL"] = "https://file.example.test"
os.environ["APP_ENV"] = "test"
os.environ["SESSION_SECRET"] = "smoke-test-session-secret-at-least-32-bytes"
os.environ["SUBSCRIPTION_TOKEN_ENCRYPTION_SECRET"] = "smoke-test-token-encryption-secret-at-least-32-bytes"
os.environ["SUBSCRIPTION_IDENTITY_SECRET"] = "smoke-test-subscription-identity-secret-at-least-32-bytes"
os.environ["SUBSCRIPTION_IDENTITY_SYNC_ENABLED"] = "false"
os.environ["COS_ENABLED"] = "false"
os.environ["LOCAL_TEMP_ENABLED"] = "true"

from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.identity_sync import IdentitySyncError
from app.main import app
from app.security import SESSION_COOKIE, now_iso, sign_session
import app.subscriptions as subscriptions


FILE_ID = "lf_create_idempotency"


def seed_file() -> int:
    payload = b"proxies: []\nproxy-groups: []\nrules: []\n"
    object_key = "other/subscription.yml"
    target = root / "storage" / object_key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    now = now_iso()
    with connect() as conn:
        owner = conn.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()["id"]
        conn.execute(
            """
            INSERT INTO files(
                file_id, owner_id, original_filename, stored_filename, extension, mime_type,
                size_bytes, sha256, storage_backend, bucket_name, object_key, public_url,
                link_enabled, visibility, purpose, status, uploaded_at, updated_at
            ) VALUES (?, ?, 'subscription.yml', 'subscription.yml', '.yml', 'text/yaml',
                      ?, ?, 'local_temp', 'local', ?, NULL, 0, 'private', 'other', 'active', ?, ?)
            """,
            (FILE_ID, owner, len(payload), hashlib.sha256(payload).hexdigest(), object_key, now, now),
        )
        conn.commit()
    return owner


def three_region_sync(_records):
    return [{"target": "region-a"}, {"target": "region-b"}, {"target": "region-c"}]


def wait_for_token(label: str, status: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    row = None
    while time.monotonic() < deadline:
        with connect() as conn:
            row = conn.execute(
                "SELECT id, status, token_ciphertext FROM subscription_tokens WHERE label=?",
                (label,),
            ).fetchone()
        if row and row["status"] == status:
            return row
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {label} to become {status}; last row={dict(row) if row else None}")


def test_service_idempotency(owner: int) -> None:
    subscriptions.sync_identity_records = three_region_sync
    create_key = subscriptions.prepare_subscription_create(owner)

    def enqueue_once(_index: int):
        return subscriptions.enqueue_subscription_link(FILE_ID, "concurrent", owner, 30, create_key)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(enqueue_once, range(8)))
    assert len({result["id"] for result in results}) == 1
    assert sum(1 for result in results if result["newly_enqueued"]) == 1
    with connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM subscription_tokens WHERE label='concurrent'"
        ).fetchone()["count"] == 1

    try:
        subscriptions.enqueue_subscription_link(FILE_ID, "different-parameters", owner, 30, create_key)
        raise AssertionError("A reused idempotency key accepted different parameters.")
    except subscriptions.SubscriptionCreateConflict:
        pass

    completed = subscriptions.process_subscription_create_request(create_key)
    assert completed and completed["status"] == "succeeded" and completed["url"]
    replay = subscriptions.enqueue_subscription_link(FILE_ID, "concurrent", owner, 30, create_key)
    assert replay["id"] == completed["id"] and replay["newly_enqueued"] is False

    assert subscriptions.revoke_subscription_link(completed["id"]) == "revoked"
    revoked_replay = subscriptions.enqueue_subscription_link(FILE_ID, "concurrent", owner, 30, create_key)
    assert revoked_replay["id"] == completed["id"]
    assert subscriptions.get_subscription_create_request(create_key)["url"] is None
    with connect() as conn:
        revoked = conn.execute(
            "SELECT status, token_ciphertext FROM subscription_tokens WHERE id=?", (completed["id"],)
        ).fetchone()
    assert revoked["status"] == "revoked" and revoked["token_ciphertext"] is None

    stale_key = "scr_" + "s" * 24
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO subscription_create_requests(
                idempotency_key, created_by, status, created_at, updated_at
            ) VALUES (?, ?, 'ready', '2000-01-01T00:00:00+00:00', '2000-01-01T00:00:00+00:00')
            """,
            (stale_key, owner),
        )
        conn.commit()
    subscriptions.prepare_subscription_create(owner)
    with connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM subscription_create_requests WHERE idempotency_key=?", (stale_key,)
        ).fetchone() is None
        assert conn.execute(
            "SELECT status FROM subscription_create_requests WHERE idempotency_key=?", (create_key,)
        ).fetchone()["status"] == "succeeded"

    reconcile_key = subscriptions.prepare_subscription_create(owner)
    reconcile_queued = subscriptions.enqueue_subscription_link(
        FILE_ID, "reconcile-safe", owner, 1, reconcile_key
    )
    subscriptions.reconcile_subscription_identities()
    with connect() as conn:
        reconcile_token = conn.execute(
            "SELECT status, token_ciphertext FROM subscription_tokens WHERE id=?",
            (reconcile_queued["id"],),
        ).fetchone()
        reconcile_request = conn.execute(
            "SELECT status FROM subscription_create_requests WHERE idempotency_key=?",
            (reconcile_key,),
        ).fetchone()
    assert reconcile_token["status"] == "provisioning" and reconcile_token["token_ciphertext"]
    assert reconcile_request["status"] == "queued"
    reconcile_completed = subscriptions.process_subscription_create_request(reconcile_key)
    assert reconcile_completed and reconcile_completed["status"] == "succeeded"

    failed_key = subscriptions.prepare_subscription_create(owner)
    failed = subscriptions.enqueue_subscription_link(FILE_ID, "controlled-failure", owner, 1, failed_key)

    def fail_sync(_records):
        raise IdentitySyncError("controlled three-region failure")

    subscriptions.sync_identity_records = fail_sync
    failed_result = subscriptions.process_subscription_create_request(failed_key)
    assert failed_result and failed_result["status"] == "failed" and failed_result["url"] is None
    failed_replay = subscriptions.enqueue_subscription_link(FILE_ID, "controlled-failure", owner, 1, failed_key)
    assert failed_replay["id"] == failed["id"] and failed_replay["newly_enqueued"] is False
    with connect() as conn:
        failed_rows = conn.execute(
            "SELECT COUNT(*) AS count FROM subscription_tokens WHERE label='controlled-failure'"
        ).fetchone()["count"]
        failed_token = conn.execute(
            "SELECT status, token_ciphertext FROM subscription_tokens WHERE id=?", (failed["id"],)
        ).fetchone()
    assert failed_rows == 1
    assert failed_token["status"] == "provision_failed" and failed_token["token_ciphertext"] is None

    interrupted_key = subscriptions.prepare_subscription_create(owner)
    subscriptions.sync_identity_records = three_region_sync
    subscriptions.enqueue_subscription_link(FILE_ID, "restart-recovery", owner, 1, interrupted_key)
    with connect() as conn:
        conn.execute(
            "UPDATE subscription_create_requests SET status='processing' WHERE idempotency_key=?",
            (interrupted_key,),
        )
        conn.commit()
    recovered = subscriptions.process_subscription_create_request(interrupted_key)
    assert recovered and recovered["status"] == "succeeded" and recovered["url"]


def test_async_prg(owner: int) -> None:
    sync_started = threading.Event()
    release_sync = threading.Event()
    sync_calls = []

    def slow_three_region_sync(records):
        sync_calls.append(len(records))
        sync_started.set()
        assert release_sync.wait(timeout=5), "The test did not release controlled node synchronization."
        return three_region_sync(records)

    subscriptions.sync_identity_records = slow_three_region_sync
    with TestClient(app, follow_redirects=False) as client:
        client.cookies.set(SESSION_COOKIE, sign_session("luomo"))
        page = client.get("/admin/subscriptions")
        create_key_match = re.search(
            r'name="idempotency_key" value="(scr_[A-Za-z0-9_-]+)"', page.text
        )
        assert page.status_code == 200 and create_key_match
        create_key = create_key_match.group(1)
        form = {
            "file_id": FILE_ID,
            "label": "async-web",
            "expires_in_days": "30",
            "idempotency_key": create_key,
        }

        started_at = time.monotonic()
        accepted = client.post("/admin/subscriptions/create", data=form, headers={"origin": "https://file.example.test"})
        elapsed = time.monotonic() - started_at
        assert accepted.status_code == 303 and elapsed < 1.0, elapsed
        assert accepted.headers["location"] == "/admin/subscriptions"
        assert "lsub_" not in accepted.headers["location"] and create_key not in accepted.headers["location"]
        assert sync_started.wait(timeout=2)

        pending_page = client.get("/admin/subscriptions")
        assert 'data-link-status="processing"' in pending_page.text
        assert "data-subscription-refresh-status" in pending_page.text
        assert "async-web" in pending_page.text
        async_row = re.search(r"<strong>async-web</strong>.*?</div>", pending_page.text, re.DOTALL)
        assert async_row and "/sub/lsub_" not in async_row.group(0)

        replay = client.post("/admin/subscriptions/create", data=form, headers={"origin": "https://file.example.test"})
        assert replay.status_code == 303 and replay.headers["location"] == "/admin/subscriptions"
        with connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) AS count FROM subscription_tokens WHERE label='async-web'"
            ).fetchone()["count"] == 1

        conflicting_form = dict(form)
        conflicting_form["expires_in_days"] = "31"
        conflict = client.post("/admin/subscriptions/create", data=conflicting_form, headers={"origin": "https://file.example.test"})
        assert conflict.status_code == 409
        assert "参数不能更改" in conflict.text

        release_sync.set()
        active = wait_for_token("async-web", "active")
        completed_page = client.get("/admin/subscriptions")
        assert re.search(
            r"<strong>async-web</strong>.*?https://file\.example\.test/sub/lsub_[A-Za-z0-9_-]+",
            completed_page.text,
            re.DOTALL,
        )
        for format_name in ("mihomo", "clash", "uri", "base64", "sing-box"):
            assert f'<option value="{format_name}"' in completed_page.text

        assert subscriptions.revoke_subscription_link(active["id"]) == "revoked"
        revoked_replay = client.post("/admin/subscriptions/create", data=form, headers={"origin": "https://file.example.test"})
        assert revoked_replay.status_code == 303
        with connect() as conn:
            row = conn.execute(
                "SELECT status, token_ciphertext FROM subscription_tokens WHERE id=?", (active["id"],)
            ).fetchone()
            count = conn.execute(
                "SELECT COUNT(*) AS count FROM subscription_tokens WHERE label='async-web'"
            ).fetchone()["count"]
        assert row["status"] == "revoked" and row["token_ciphertext"] is None and count == 1
        assert sync_calls


def main() -> int:
    init_db()
    owner = seed_file()
    test_service_idempotency(owner)
    test_async_prg(owner)
    print("SUBSCRIPTION_CREATE_IDEMPOTENCY_SMOKE=PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        temporary.cleanup()
