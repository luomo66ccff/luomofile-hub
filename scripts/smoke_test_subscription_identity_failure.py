import hashlib
import os
import sys
import tempfile
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
temporary = tempfile.TemporaryDirectory(prefix="luomofile-identity-failure-", ignore_cleanup_errors=True)
root = Path(temporary.name)
os.environ["DATABASE_PATH"] = str(root / "luomofile.db")
os.environ["LOCAL_TEMP_PATH"] = str(root / "storage")
os.environ["APP_BASE_URL"] = "https://file.example.test"
os.environ["APP_ENV"] = "test"
os.environ["SESSION_SECRET"] = "smoke-test-session-secret"
os.environ["SUBSCRIPTION_TOKEN_ENCRYPTION_SECRET"] = "smoke-test-token-encryption-secret-at-least-32-bytes"
os.environ["SUBSCRIPTION_IDENTITY_SECRET"] = "smoke-test-subscription-identity-secret-at-least-32-bytes"
os.environ["SUBSCRIPTION_IDENTITY_SYNC_ENABLED"] = "false"
os.environ["COS_ENABLED"] = "false"
os.environ["LOCAL_TEMP_ENABLED"] = "true"

from app.db import connect, init_db
from app.identity_sync import IdentitySyncError
from app.security import now_iso
import app.subscriptions as subscriptions


def seed_file() -> int:
    payload = b'''proxies:
- name: vmess
  type: vmess
  uuid: old
- name: hy2
  type: hysteria2
  password: old
- name: tuic
  type: tuic
  uuid: old
  password: old
- name: ss
  type: ss
  password: old
proxy-groups: []
'''
    target = root / "storage" / "other/subscription.yml"
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
            ) VALUES ('lf_identity_failure', ?, 'subscription.yml', 'subscription.yml', '.yml',
                      'text/yaml', ?, ?, 'local_temp', 'local', 'other/subscription.yml', NULL,
                      0, 'private', 'other', 'active', ?, ?)
            """,
            (owner, len(payload), hashlib.sha256(payload).hexdigest(), now, now),
        )
        conn.commit()
    return owner


def main() -> int:
    init_db()
    owner = seed_file()
    real_sync = subscriptions.sync_identity_records

    def fail(_records):
        raise IdentitySyncError("controlled failure")

    subscriptions.sync_identity_records = fail
    try:
        subscriptions.create_subscription_link("lf_identity_failure", "must-fail", owner, 1)
        raise AssertionError("Creation unexpectedly succeeded.")
    except ValueError:
        pass
    with connect() as conn:
        failed = conn.execute("SELECT status FROM subscription_tokens WHERE label='must-fail'").fetchone()
    assert failed["status"] == "provision_failed"

    subscriptions.sync_identity_records = real_sync
    created = subscriptions.create_subscription_link("lf_identity_failure", "revoke-retry", owner, 1)
    token = created["url"].rsplit("/", 1)[1]
    subscriptions.sync_identity_records = fail
    assert subscriptions.revoke_subscription_link(created["id"]) == "revoke_pending"
    assert subscriptions.resolve_subscription_token(token) is None
    with connect() as conn:
        pending = conn.execute("SELECT status, sync_error FROM subscription_tokens WHERE id=?", (created["id"],)).fetchone()
    assert pending["status"] == "revoke_pending" and pending["sync_error"]

    subscriptions.sync_identity_records = real_sync
    result = subscriptions.reconcile_subscription_identities()
    assert result["revoked"] == 1
    with connect() as conn:
        final = conn.execute("SELECT status, sync_error FROM subscription_tokens WHERE id=?", (created["id"],)).fetchone()
    assert final["status"] == "revoked" and final["sync_error"] is None
    print("SUBSCRIPTION_IDENTITY_FAILURE_SMOKE=PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        temporary.cleanup()
