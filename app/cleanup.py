from datetime import datetime, timezone

from app.db import connect
from app.files import delete_file
from app.security import now_iso


def cleanup_once() -> dict:
    started = now_iso()
    scanned = 0
    deleted = 0
    freed = 0
    error = ""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM files
            WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at < ?
            ORDER BY expires_at ASC
            """,
            (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
        ).fetchall()
    for row in rows:
        scanned += 1
        try:
            if delete_file(row["file_id"]):
                deleted += 1
                freed += int(row["size_bytes"] or 0)
        except Exception as exc:
            error = str(exc)[:300]
    finished = now_iso()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO cleanup_jobs(job_type, status, started_at, finished_at, scanned_count, deleted_count, freed_bytes, error_summary)
            VALUES ('expired_files', ?, ?, ?, ?, ?, ?, ?)
            """,
            ("failed" if error else "success", started, finished, scanned, deleted, freed, error),
        )
        conn.commit()
    return {"scanned": scanned, "deleted": deleted, "freed_bytes": freed, "error": error}
