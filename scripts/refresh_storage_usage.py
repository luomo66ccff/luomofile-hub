import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect, init_db
from app.security import now_iso


if __name__ == "__main__":
    init_db()
    with connect() as conn:
        rows = conn.execute("SELECT storage_backend, COALESCE(SUM(size_bytes),0) AS used FROM files WHERE status='active' GROUP BY storage_backend").fetchall()
        for row in rows:
            conn.execute("UPDATE storage_backends SET used_bytes_cache=?, updated_at=? WHERE name=?", (row["used"], now_iso(), row["storage_backend"]))
        conn.commit()
    print("refreshed")
