import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.auth import generate_api_key
from app.db import connect, init_db
from app.security import hash_token, now_iso

BASE = os.getenv("SMOKE_BASE_URL", "http://127.0.0.1:8791")
DB = os.getenv("DATABASE_PATH", "/app/data/luomofile.db")


def main() -> None:
    with httpx.Client(follow_redirects=False, timeout=10) as client:
        assert client.get(f"{BASE}/health").json() == {"status": "ok"}
        assert client.get(f"{BASE}/admin").status_code != 200
        with tempfile.NamedTemporaryFile("wb", delete=False) as handle:
            handle.write(b"x" * (1024 * 1024 + 1))
            big_path = handle.name
        with open(big_path, "rb") as fh:
            denied = client.post(f"{BASE}/upload", files={"file": ("big.bin", fh, "application/octet-stream")})
        assert denied.status_code in {401, 403, 413}
        token = _create_smoke_key()
        with tempfile.NamedTemporaryFile("wb", delete=False) as handle:
            handle.write(b"hello luomofile")
            path = handle.name
        with open(path, "rb") as fh:
            resp = client.post(
                f"{BASE}/api/v1/files/upload",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": ("hello.txt", fh, "text/plain")},
                data={"visibility": "unlisted", "storage_policy": "local_temp", "generate_link": "true"},
            )
        assert resp.status_code == 200
        file_url = resp.json()["public_url"].replace("https://file.luomo.moe", BASE)
        got = client.get(file_url)
        assert got.status_code == 200 and got.text == "hello luomofile"
    conn = sqlite3.connect(DB)
    assert not any(row[0].startswith("lfk_") for row in conn.execute("SELECT key_hash FROM api_keys").fetchall())
    print("smoke ok")


def _create_smoke_key() -> str:
    init_db()
    token = generate_api_key()
    with connect() as conn:
        user = conn.execute("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
        conn.execute(
            "INSERT INTO api_keys(user_id, name, key_prefix, key_hash, scopes, created_at) VALUES (?, 'smoke', ?, ?, ?, ?)",
            (user["id"], token[:12], hash_token(token), "files:upload,files:read,files:list,files:delete,temp:upload,images:upload", now_iso()),
        )
        conn.commit()
    return token


if __name__ == "__main__":
    main()
