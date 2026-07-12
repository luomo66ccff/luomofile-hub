import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.db import connect, init_db
from app.security import hash_password, now_iso


if __name__ == "__main__":
    init_db()
    password = getpass.getpass("New admin password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm or len(password) < 10:
        raise SystemExit("Password mismatch or too short.")
    with connect() as conn:
        conn.execute(
            "UPDATE users SET password_hash=?, status='active', role='admin', updated_at=? WHERE username=?",
            (hash_password(password), now_iso(), settings.admin_username),
        )
        conn.commit()
    print("admin password updated")
