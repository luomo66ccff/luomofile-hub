import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.auth import generate_api_key
from app.db import connect, init_db
from app.files import public_link, sanitize_filename
from app.security import SESSION_COOKIE, hash_token, now_iso, sign_session

BASE = os.getenv("SMOKE_BASE_URL", "http://127.0.0.1:8791")


def main() -> None:
    init_db()
    token, username = _create_smoke_key()
    with httpx.Client(follow_redirects=False, timeout=10) as client:
        assert client.get(f"{BASE}/health").json() == {"status": "ok"}
        with tempfile.NamedTemporaryFile("wb", delete=False) as handle:
            handle.write(b"private by default")
            path = handle.name

        with open(path, "rb") as fh:
            resp = client.post(
                f"{BASE}/api/v1/files/upload",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": ("policy.txt", fh, "text/plain")},
                data={"visibility": "unlisted", "storage_policy": "local_temp"},
            )
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["link_enabled"] is False
        assert payload["public_url"] is None

        file_id = payload["file_id"]
        path_url = f"{BASE}/f/{file_id}/policy.txt"
        assert client.get(path_url).status_code == 404

        client.cookies.set(SESSION_COOKIE, sign_session(username))
        owner_get = client.get(path_url)
        assert owner_get.status_code == 200 and owner_get.text == "private by default"

        gen = client.post(f"{BASE}/files/{file_id}/link/generate")
        assert gen.status_code == 303
        detail = client.get(f"{BASE}/api/v1/files/{file_id}", headers={"Authorization": f"Bearer {token}"})
        assert detail.status_code == 200
        row = detail.json()
        assert row["link_enabled"] == 1
        assert row["public_url"].startswith("https://file.luomo.moe/")
        assert not row["public_url"].startswith("http://file.luomo.moe/")

        client.cookies.clear()
        public_path = row["public_url"].replace("https://file.luomo.moe", BASE)
        assert client.get(public_path).status_code == 200

        client.cookies.set(SESSION_COOKIE, sign_session(username))
        revoked = client.post(f"{BASE}/files/{file_id}/link/revoke")
        assert revoked.status_code == 303
        client.cookies.clear()
        assert client.get(public_path).status_code == 404

        with connect() as conn:
            existing = conn.execute("SELECT COUNT(*) AS c FROM files WHERE status='active'").fetchone()["c"]
        assert existing >= 1
    print("link policy smoke ok")


def _create_smoke_key() -> tuple[str, str]:
    token = generate_api_key()
    now = now_iso()
    with connect() as conn:
        user = conn.execute("SELECT id, username FROM users WHERE role='admin' AND status='active' ORDER BY id LIMIT 1").fetchone()
        conn.execute(
            "INSERT INTO api_keys(user_id, name, key_prefix, key_hash, scopes, created_at) VALUES (?, 'link-policy-smoke', ?, ?, ?, ?)",
            (user["id"], token[:12], hash_token(token), "files:upload,files:read,files:list,files:delete,temp:upload,images:upload", now),
        )
        conn.commit()
        return token, user["username"]


if __name__ == "__main__":
    main()
