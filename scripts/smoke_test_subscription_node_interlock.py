import hashlib
import os
import sys
import tempfile
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
temporary = tempfile.TemporaryDirectory(prefix="luomofile-node-interlock-", ignore_cleanup_errors=True)
root = Path(temporary.name)
os.environ["DATABASE_PATH"] = str(root / "luomofile.db")
os.environ["LOCAL_TEMP_PATH"] = str(root / "storage")
os.environ["APP_BASE_URL"] = "https://file.example.test"
os.environ["APP_ENV"] = "test"
os.environ["SESSION_SECRET"] = "smoke-test-session-secret-with-more-than-32-bytes"
os.environ["SUBSCRIPTION_TOKEN_ENCRYPTION_SECRET"] = "smoke-test-token-encryption-secret-at-least-32-bytes"
os.environ["SUBSCRIPTION_IDENTITY_SECRET"] = "smoke-test-subscription-identity-secret-at-least-32-bytes"
os.environ["SUBSCRIPTION_IDENTITY_SYNC_ENABLED"] = "false"
os.environ["SUBSCRIPTION_IDENTITY_ATTESTATION_MAX_AGE_SECONDS"] = "30"
os.environ["COS_ENABLED"] = "false"
os.environ["LOCAL_TEMP_ENABLED"] = "true"

from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.identity_sync import IdentitySyncError
from app.main import app
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
            ) VALUES ('lf_interlock', ?, 'subscription.yml', 'subscription.yml', '.yml',
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
    created = subscriptions.create_subscription_link("lf_interlock", "interlock", owner, 1)
    token = created["url"].rsplit("/", 1)[1]
    real_sync = subscriptions.sync_identity_records

    with TestClient(app, follow_redirects=False) as client:
        first = client.get(f"/sub/{token}")
        assert first.status_code == 200
        with connect() as conn:
            first_count = conn.execute(
                "SELECT use_count FROM subscription_tokens WHERE id=?", (created["id"],)
            ).fetchone()["use_count"]
            conn.execute(
                "UPDATE subscription_identity_health SET last_error='controlled stale state' WHERE id=1"
            )
            conn.commit()
        assert first_count == 1

        sync_calls = []

        def fail(_records):
            sync_calls.append(True)
            raise IdentitySyncError("controlled attestation failure")

        subscriptions.sync_identity_records = fail
        stale_read = client.get(f"/sub/{token}")
        stale_head = client.head(f"/sub/{token}")
        assert stale_read.status_code == 200 and stale_head.status_code == 200
        assert not sync_calls
        with connect() as conn:
            stale_count = conn.execute(
                "SELECT use_count FROM subscription_tokens WHERE id=?", (created["id"],)
            ).fetchone()["use_count"]
        assert stale_count == 2

        assert subscriptions.revoke_subscription_link(created["id"]) == "revoke_pending"
        assert len(sync_calls) == 1
        assert client.get(f"/sub/{token}").status_code == 404

        subscriptions.sync_identity_records = real_sync
        assert subscriptions.revoke_subscription_link(created["id"]) == "revoked"
        assert subscriptions.subscription_identity_health()["fresh"] is True
        assert client.get(f"/sub/{token}").status_code == 404

    print("SUBSCRIPTION_NODE_INTERLOCK_SMOKE=PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        temporary.cleanup()
