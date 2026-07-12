import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.auth import generate_api_key
from app.db import connect, init_db
from app.security import SESSION_COOKIE, hash_password, hash_token, now_iso, sign_session

BASE = os.getenv("SMOKE_BASE_URL", "http://127.0.0.1:8791")


def main() -> None:
    init_db()
    admin_token, admin_name = _create_key_for_admin()
    dev_name = _ensure_developer()
    with httpx.Client(follow_redirects=False, timeout=10) as client:
        assert client.get(f"{BASE}/files").status_code in {401, 403}

        private_id = _upload(client, admin_token, "files-page-private.txt", False)
        public_id = _upload(client, admin_token, "files-page-public.txt", True)

        client.cookies.set(SESSION_COOKIE, sign_session(admin_name))
        page = client.get(f"{BASE}/files?page_size=20")
        assert page.status_code == 200
        text = page.text
        assert "File Library" in text
        assert "Name" in text and "Size" in text and "Actions" in text
        assert "18 B" in text or "17 B" in text
        assert text.count(f'value="{private_id}"') == 1
        assert "Generate link" in text
        assert "Copy link" in text and "Revoke link" in text

        paged = client.get(f"{BASE}/files?page_size=1&page=1")
        assert paged.status_code == 200 and "Next" in paged.text
        searched = client.get(f"{BASE}/files?q=files-page-private")
        assert private_id in searched.text and public_id not in searched.text
        backend = client.get(f"{BASE}/files?storage_backend=local_temp")
        assert backend.status_code == 200 and '<select name="storage_backend">' in backend.text
        all_files = client.get(f"{BASE}/files?scope=all")
        assert all_files.status_code == 200

        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, sign_session(dev_name))
        dev_page = client.get(f"{BASE}/files")
        assert dev_page.status_code == 200
        assert private_id not in dev_page.text and public_id not in dev_page.text

    _cleanup([private_id, public_id], admin_token)
    print("files page smoke ok")


def _create_key_for_admin() -> tuple[str, str]:
    token = generate_api_key()
    with connect() as conn:
        user = conn.execute("SELECT id, username FROM users WHERE role='admin' AND status='active' ORDER BY id LIMIT 1").fetchone()
        conn.execute(
            "INSERT INTO api_keys(user_id, name, key_prefix, key_hash, scopes, created_at) VALUES (?, 'files-page-smoke', ?, ?, ?, ?)",
            (user["id"], token[:12], hash_token(token), "files:upload,files:read,files:list,files:delete,files:link:create,files:link:revoke", now_iso()),
        )
        conn.commit()
    return token, user["username"]


def _ensure_developer() -> str:
    username = "files_page_smoke_dev"
    with connect() as conn:
        row = conn.execute("SELECT username FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            now = now_iso()
            conn.execute(
                "INSERT INTO users(username, email, password_hash, role, status, created_at, updated_at) VALUES (?, ?, ?, 'developer', 'active', ?, ?)",
                (username, "files-page-smoke@example.invalid", hash_password("SmokePassword123"), now, now),
            )
            conn.commit()
    return username


def _upload(client: httpx.Client, token: str, filename: str, generate_link: bool) -> str:
    with tempfile.NamedTemporaryFile("wb", delete=False) as handle:
        handle.write(b"files page smoke")
        path = handle.name
    with open(path, "rb") as fh:
        resp = client.post(
            f"{BASE}/api/v1/files/upload",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (filename, fh, "text/plain")},
            data={"visibility": "unlisted", "storage_policy": "local_temp", "generate_link": str(generate_link).lower()},
        )
    assert resp.status_code == 200, resp.text
    return resp.json()["file_id"]


def _cleanup(file_ids: list[str], token: str) -> None:
    with httpx.Client(timeout=10) as client:
        for file_id in file_ids:
            client.delete(f"{BASE}/api/v1/files/{file_id}", headers={"Authorization": f"Bearer {token}"})


if __name__ == "__main__":
    main()
