import hashlib

from fastapi import Request

from app.db import connect
from app.security import now_iso


def audit(action: str, request: Request | None = None, user=None, target_type: str = "", target_id: str = "", status: str = "ok", detail: str = "") -> None:
    ip_hash = ""
    ua = ""
    if request:
        ip = request.client.host if request.client else ""
        ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:24] if ip else ""
        ua = (request.headers.get("user-agent") or "")[:160]
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO audit_logs(created_at, actor_user_id, action, target_type, target_id, status, detail, client_ip_hash, user_agent_preview)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (now_iso(), user["id"] if user else None, action, target_type, target_id, status, detail[:240], ip_hash, ua),
        )
        conn.commit()
