import sqlite3
import secrets
from pathlib import Path

from app.config import ensure_dirs, settings
from app.security import hash_password, now_iso


SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        email TEXT,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'developer',
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_login_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS api_keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        key_prefix TEXT NOT NULL,
        key_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        scopes TEXT NOT NULL,
        rate_limit_per_minute INTEGER DEFAULT 60,
        daily_quota INTEGER DEFAULT 1000,
        monthly_quota INTEGER DEFAULT 30000,
        created_at TEXT NOT NULL,
        last_used_at TEXT,
        revoked_at TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS email_verification_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        purpose TEXT NOT NULL DEFAULT 'register',
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id TEXT NOT NULL UNIQUE,
        owner_id INTEGER,
        original_filename TEXT NOT NULL,
        stored_filename TEXT NOT NULL,
        extension TEXT,
        mime_type TEXT,
        size_bytes INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        storage_backend TEXT NOT NULL,
        bucket_name TEXT,
        object_key TEXT NOT NULL,
        public_url TEXT,
        link_enabled INTEGER NOT NULL DEFAULT 0,
        link_created_at TEXT,
        link_revoked_at TEXT,
        visibility TEXT NOT NULL DEFAULT 'unlisted',
        purpose TEXT NOT NULL DEFAULT 'other',
        status TEXT NOT NULL DEFAULT 'active',
        expires_at TEXT,
        uploaded_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_accessed_at TEXT,
        access_count INTEGER NOT NULL DEFAULT 0,
        delete_after_access INTEGER DEFAULT 0,
        tags TEXT,
        notes TEXT,
        FOREIGN KEY(owner_id) REFERENCES users(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS file_access_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id TEXT NOT NULL,
        accessed_at TEXT NOT NULL,
        client_ip_hash TEXT,
        user_agent_preview TEXT,
        referer_preview TEXT,
        status_code INTEGER,
        bytes_sent INTEGER,
        FOREIGN KEY(file_id) REFERENCES files(file_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS api_usage_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        user_id INTEGER,
        api_key_id INTEGER,
        method TEXT,
        path TEXT,
        status_code INTEGER,
        success INTEGER,
        response_time_ms INTEGER,
        error_summary TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_backends (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        type TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        bucket_name TEXT,
        public_base_url TEXT,
        max_capacity_bytes INTEGER,
        used_bytes_cache INTEGER DEFAULT 0,
        reserved_bytes INTEGER DEFAULT 0,
        priority INTEGER DEFAULT 100,
        config_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cleanup_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_type TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        scanned_count INTEGER DEFAULT 0,
        deleted_count INTEGER DEFAULT 0,
        freed_bytes INTEGER DEFAULT 0,
        error_summary TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        actor_user_id INTEGER,
        action TEXT NOT NULL,
        target_type TEXT,
        target_id TEXT,
        status TEXT NOT NULL DEFAULT 'ok',
        detail TEXT,
        client_ip_hash TEXT,
        user_agent_preview TEXT
    )
    """,
]


def connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    ensure_dirs()
    with connect() as conn:
        for statement in SCHEMA:
            conn.execute(statement)
        _migrate_files_link_policy(conn)
        _seed_admin(conn)
        _seed_backends(conn)
        conn.commit()


def _migrate_files_link_policy(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(files)").fetchall()}
    if "link_enabled" not in columns:
        conn.execute("ALTER TABLE files ADD COLUMN link_enabled INTEGER NOT NULL DEFAULT 0")
    if "link_created_at" not in columns:
        conn.execute("ALTER TABLE files ADD COLUMN link_created_at TEXT")
    if "link_revoked_at" not in columns:
        conn.execute("ALTER TABLE files ADD COLUMN link_revoked_at TEXT")
    if "tags" not in columns:
        conn.execute("ALTER TABLE files ADD COLUMN tags TEXT")
    now = now_iso()
    conn.execute(
        """
        UPDATE files
        SET link_enabled=1, link_created_at=COALESCE(link_created_at, uploaded_at, ?)
        WHERE status='active'
          AND visibility IN ('public', 'unlisted')
          AND public_url IS NOT NULL
          AND public_url != ''
          AND link_enabled=0
        """,
        (now,),
    )


def _seed_admin(conn: sqlite3.Connection) -> None:
    now = now_iso()
    existing = conn.execute("SELECT id FROM users WHERE username = ?", (settings.admin_username,)).fetchone()
    if existing:
        return
    password_hash = settings.admin_password_hash or _initial_admin_password_hash()
    conn.execute(
        """
        INSERT INTO users(username, password_hash, role, status, created_at, updated_at)
        VALUES (?, ?, 'admin', 'active', ?, ?)
        """,
        (settings.admin_username, password_hash, now, now),
    )


def _initial_admin_password_hash() -> str:
    password_file = settings.database_path.parent / "admin_initial_password.txt"
    if password_file.exists():
        password = password_file.read_text().strip()
    else:
        password = secrets.token_urlsafe(24)
        password_file.write_text(password + "\n")
        password_file.chmod(0o600)
    return hash_password(password)


def _seed_backends(conn: sqlite3.Connection) -> None:
    now = now_iso()
    rows = [
        ("r2", "r2", 1 if settings.r2_enabled else 0, settings.r2_bucket, settings.r2_public_base_url, settings.r2_max_capacity_bytes, 10),
        ("cos_sv", "cos", 1 if settings.cos_sv_enabled else 0, "cos-sv-mounted", settings.cos_sv_public_base_url, settings.cos_sv_max_capacity_bytes, 20),
        ("cos", "cos", 1 if settings.cos_enabled else 0, settings.cos_bucket or "cos-mounted", settings.cos_public_base_url, settings.cos_max_capacity_bytes, 30),
        ("local_temp", "local_temp", 1 if settings.local_temp_enabled else 0, "local", "", settings.local_temp_max_capacity_bytes, 100),
    ]
    for row in rows:
        conn.execute(
            """
            INSERT INTO storage_backends(name, type, enabled, bucket_name, public_base_url, max_capacity_bytes, priority, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled, bucket_name=excluded.bucket_name,
                public_base_url=excluded.public_base_url, max_capacity_bytes=excluded.max_capacity_bytes,
                priority=excluded.priority, updated_at=excluded.updated_at
            """,
            (*row, now, now),
        )
