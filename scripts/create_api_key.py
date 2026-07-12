import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth import generate_api_key
from app.db import connect, init_db
from app.security import hash_token, now_iso


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", default="luomo")
    parser.add_argument("--name", default="default")
    parser.add_argument("--scopes", default="files:upload,files:read,files:delete,files:list,temp:upload,images:upload,admin:read")
    args = parser.parse_args()
    init_db()
    token = generate_api_key()
    with connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE username=?", (args.username,)).fetchone()
        if not user:
            raise SystemExit("user not found")
        conn.execute(
            "INSERT INTO api_keys(user_id, name, key_prefix, key_hash, scopes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user["id"], args.name, token[:12], hash_token(token), args.scopes, now_iso()),
        )
        conn.commit()
    print(token)
