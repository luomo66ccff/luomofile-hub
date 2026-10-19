"""Additive gallery schema. Never infer sharing permission from legacy files."""

import hashlib
import sqlite3

from app.security import now_iso


VERSION = "gallery_001"
STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS albums (
        album_id TEXT PRIMARY KEY,
        owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 120),
        description TEXT NOT NULL DEFAULT '' CHECK(length(description) <= 2000),
        kind TEXT NOT NULL CHECK(kind IN ('personal','shared')),
        view_policy TEXT NOT NULL DEFAULT 'restricted' CHECK(view_policy IN ('restricted','authenticated','public')),
        contribute_policy TEXT NOT NULL DEFAULT 'restricted' CHECK(contribute_policy IN ('restricted','authenticated')),
        listed INTEGER NOT NULL DEFAULT 0 CHECK(listed IN (0,1)),
        review_required INTEGER NOT NULL DEFAULT 1 CHECK(review_required IN (0,1)),
        cover_file_id TEXT REFERENCES files(file_id) ON DELETE SET NULL,
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived','deleted')),
        acl_version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK(listed=0 OR view_policy='public'),
        CHECK(kind='shared' OR contribute_policy='restricted')
    )""",
    """CREATE TABLE IF NOT EXISTS album_members (
        album_id TEXT NOT NULL REFERENCES albums(album_id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        can_view INTEGER NOT NULL DEFAULT 0 CHECK(can_view IN (0,1)),
        can_contribute INTEGER NOT NULL DEFAULT 0 CHECK(can_contribute IN (0,1)),
        can_manage INTEGER NOT NULL DEFAULT 0 CHECK(can_manage IN (0,1)),
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','revoked','blocked')),
        granted_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(album_id,user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS album_items (
        album_id TEXT NOT NULL REFERENCES albums(album_id) ON DELETE CASCADE,
        file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE RESTRICT,
        added_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','rejected','withdrawn')),
        audience_cap TEXT NOT NULL DEFAULT 'restricted' CHECK(audience_cap IN ('restricted','authenticated','public')),
        allow_original INTEGER NOT NULL DEFAULT 0 CHECK(allow_original IN (0,1)),
        caption TEXT NOT NULL DEFAULT '' CHECK(length(caption) <= 2000),
        position INTEGER NOT NULL DEFAULT 0,
        reviewed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        reviewed_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(album_id,file_id)
    )""",
    """CREATE TABLE IF NOT EXISTS album_invitations (
        invitation_id TEXT PRIMARY KEY,
        album_id TEXT NOT NULL REFERENCES albums(album_id) ON DELETE CASCADE,
        invitee_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        can_view INTEGER NOT NULL DEFAULT 1 CHECK(can_view IN (0,1)),
        can_contribute INTEGER NOT NULL DEFAULT 0 CHECK(can_contribute IN (0,1)),
        can_manage INTEGER NOT NULL DEFAULT 0 CHECK(can_manage IN (0,1)),
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','declined','revoked')),
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        responded_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS image_assets (
        file_id TEXT PRIMARY KEY REFERENCES files(file_id) ON DELETE CASCADE,
        source_sha256 TEXT NOT NULL,
        detected_mime TEXT,
        source_width INTEGER,
        source_height INTEGER,
        width INTEGER,
        height INTEGER,
        frame_count INTEGER,
        pipeline_version INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','processing','ready','failed')),
        attempts INTEGER NOT NULL DEFAULT 0,
        error_code TEXT,
        retryable INTEGER NOT NULL DEFAULT 1 CHECK(retryable IN (0,1)),
        retry_after TEXT,
        lease_until TEXT,
        lease_token TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS image_variants (
        file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
        source_sha256 TEXT NOT NULL,
        pipeline_version INTEGER NOT NULL,
        variant TEXT NOT NULL CHECK(variant IN ('thumb','preview')),
        mime_type TEXT NOT NULL,
        width INTEGER NOT NULL CHECK(width > 0),
        height INTEGER NOT NULL CHECK(height > 0),
        size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
        sha256 TEXT NOT NULL,
        object_key TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(file_id,source_sha256,pipeline_version,variant)
    )""",
    """CREATE TABLE IF NOT EXISTS gallery_upload_reservations (
        file_id TEXT PRIMARY KEY,
        object_key TEXT NOT NULL UNIQUE,
        size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_gallery_upload_reservation_expiry ON gallery_upload_reservations(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_albums_owner_status ON albums(owner_id,status,updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_albums_public ON albums(view_policy,listed,status,updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_album_members_user ON album_members(user_id,status,album_id)",
    "CREATE INDEX IF NOT EXISTS idx_album_items_order ON album_items(album_id,status,position,file_id)",
    "CREATE INDEX IF NOT EXISTS idx_album_items_file ON album_items(file_id,status,album_id)",
    "CREATE INDEX IF NOT EXISTS idx_album_items_uploader ON album_items(added_by,status,album_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_album_invitation_pending ON album_invitations(album_id,invitee_user_id) WHERE status='pending'",
    "CREATE INDEX IF NOT EXISTS idx_album_invitation_recipient ON album_invitations(invitee_user_id,status,expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_image_assets_state ON image_assets(status,lease_until)",
)


def migrate_gallery(conn: sqlite3.Connection) -> None:
    """Run once in the caller's transaction; no data backfills or public grants."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS gallery_schema_migrations (
            version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL
        )"""
    )
    checksum = hashlib.sha256("\n".join(STATEMENTS).encode("utf-8")).hexdigest()
    existing = conn.execute(
        "SELECT checksum FROM gallery_schema_migrations WHERE version=?", (VERSION,)
    ).fetchone()
    if existing:
        if existing[0] != checksum:
            raise RuntimeError("Gallery migration checksum mismatch; apply a new migration version.")
        return
    for statement in STATEMENTS:
        conn.execute(statement)
    conn.execute(
        "INSERT INTO gallery_schema_migrations(version,checksum,applied_at) VALUES (?,?,?)",
        (VERSION, checksum, now_iso()),
    )
