import hashlib
import os
import re
import sys
import tempfile
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
temporary = tempfile.TemporaryDirectory(prefix="luomofile-subscription-auth-", ignore_cleanup_errors=True)
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
from app.main import app
from app.security import SESSION_COOKIE, hash_token, now_iso, sign_session
from app.subscriptions import create_subscription_link, revoke_subscription_link
from app.subscription_identity import derive_subscription_identity
from app.subscription_token_vault import decrypt_subscription_token


def main() -> int:
    init_db()
    payload = '''proxies:
- name: vmess-smoke
  type: vmess
  server: example.test
  port: 443
  uuid: "legacy-vmess-uuid"
- name: hysteria2-smoke
  type: hysteria2
  server: example.test
  port: 8443
  password: "legacy-hysteria2-password"
- name: Tokyo1-HY2-Public-日本东京1
  type: hysteria2
  server: disabled.example.test
  port: 8443
  password: "disabled-legacy-password"
- name: tuic-smoke
  type: tuic
  server: example.test
  port: 443
  uuid: "legacy-tuic-uuid"
  password: "legacy-tuic-password"
- name: ss-smoke
  type: ss
  server: example.test
  port: 8388
  cipher: aes-256-gcm
  password: "legacy-ss-password"
proxy-groups:
- name: smoke-select
  type: select
  proxies:
    - Tokyo1-HY2-Public-日本东京1
    - vmess-smoke
rules: []
'''.encode("utf-8")
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
                link_enabled, link_created_at, visibility, purpose, status, uploaded_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'public', 'other', 'active', ?, ?)
            """,
            (
                "lf_subscription_smoke", owner, "subscription.yml", "subscription.yml", ".yml",
                "application/octet-stream", len(payload), hashlib.sha256(payload).hexdigest(),
                "local_temp", "local", object_key,
                "https://file.example.test/f/lf_subscription_smoke/subscription.yml", now, now, now,
            ),
        )
        conn.commit()

    created = create_subscription_link("lf_subscription_smoke", "smoke", owner, 1)
    token = created["url"].rsplit("/", 1)[1]
    with connect() as conn:
        file_row = conn.execute("SELECT visibility, link_enabled, public_url FROM files WHERE file_id='lf_subscription_smoke'").fetchone()
        token_row = conn.execute("SELECT id, token_hash, token_ciphertext, status, identity_version, identity_synced_at FROM subscription_tokens WHERE id=?", (created["id"],)).fetchone()
    assert file_row["visibility"] == "private" and file_row["link_enabled"] == 0 and file_row["public_url"] is None
    assert token_row["token_hash"] == hash_token(token) and token not in token_row["token_hash"]
    assert token not in token_row["token_ciphertext"]
    assert decrypt_subscription_token(token_row["token_ciphertext"]) == token
    assert token_row["status"] == "active" and token_row["identity_version"] == 2 and token_row["identity_synced_at"]

    with TestClient(app, follow_redirects=False) as client:
        client.cookies.set(SESSION_COOKIE, sign_session("luomo"))
        admin_page = client.get("/admin/subscriptions")
        assert admin_page.status_code == 200 and "订阅链接" in admin_page.text
        assert admin_page.headers["cache-control"] == "private, no-store, max-age=0"
        assert created["url"] in admin_page.text and 'data-subscription-copy' in admin_page.text
        create_key_match = re.search(r'name="idempotency_key" value="(scr_[A-Za-z0-9_-]+)"', admin_page.text)
        assert create_key_match
        create_key = create_key_match.group(1)
        admin_created = client.post(
            "/admin/subscriptions/create",
            data={
                "file_id": "lf_subscription_smoke",
                "label": "web-smoke",
                "expires_in_days": "1",
                "idempotency_key": create_key,
            },
            headers={"origin": "https://file.example.test"},
        )
        assert admin_created.status_code == 303
        assert admin_created.headers["location"] == "/admin/subscriptions"
        assert "lsub_" not in admin_created.headers["location"] and create_key not in admin_created.headers["location"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with connect() as conn:
                web_status = conn.execute(
                    "SELECT status FROM subscription_tokens WHERE label='web-smoke'"
                ).fetchone()
            if web_status and web_status["status"] == "active":
                break
            time.sleep(0.05)
        assert web_status and web_status["status"] == "active"
        completed_admin_page = client.get("/admin/subscriptions")
        web_match = re.search(
            r"<strong>web-smoke</strong>.*?https://file\.example\.test/sub/(lsub_[A-Za-z0-9_-]+)",
            completed_admin_page.text,
            re.DOTALL,
        )
        assert web_match
        web_token = web_match.group(1)
        assert client.get(f"/sub/{web_token}").status_code == 200
        with connect() as conn:
            web_token_id = conn.execute("SELECT id FROM subscription_tokens WHERE label='web-smoke'").fetchone()["id"]
        admin_revoked = client.post(f"/admin/subscriptions/{web_token_id}/revoke", headers={"origin": "https://file.example.test"})
        assert admin_revoked.status_code == 303
        assert client.get(f"/sub/{web_token}").status_code == 404
        revoked_admin_page = client.get("/admin/subscriptions")
        assert web_token not in revoked_admin_page.text
        with connect() as conn:
            revoked_row = conn.execute("SELECT token_ciphertext FROM subscription_tokens WHERE id=?", (web_token_id,)).fetchone()
        assert revoked_row["token_ciphertext"] is None
        client.cookies.clear()

        protected = client.get(f"/sub/{token}")
        identity = derive_subscription_identity(hash_token(token))
        rendered = protected.text
        assert protected.status_code == 200 and protected.content != payload, (
            protected.status_code,
            protected.headers.get("location"),
            protected.text[:200],
        )
        assert identity.vmess_uuid in rendered
        assert identity.hysteria2_password in rendered
        assert identity.tuic_uuid in rendered and identity.tuic_password in rendered
        assert identity.shadowsocks_password in rendered
        assert "legacy-vmess-uuid" not in rendered and "legacy-ss-password" not in rendered
        assert "Tokyo1-HY2-Public-日本东京1" not in rendered
        assert protected.headers["cache-control"] == "private, no-store, max-age=0"
        assert protected.headers["content-type"].startswith("text/yaml")
        invalid = client.get("/sub/lsub_invalid_invalid_invalid_invalid")
        assert invalid.status_code == 404 and "location" not in invalid.headers
        legacy = client.get("/f/lf_subscription_smoke/subscription.yml")
        assert legacy.status_code == 404 and "location" not in legacy.headers
        assert "legacy-vmess-uuid" not in legacy.text and identity.shadowsocks_password not in legacy.text
        assert revoke_subscription_link(created["id"]) == "revoked"
        revoked = client.get(f"/sub/{token}")
        assert revoked.status_code == 404 and "location" not in revoked.headers

    print("SUBSCRIPTION_AUTH_SMOKE=PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        temporary.cleanup()
