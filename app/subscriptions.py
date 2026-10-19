import hashlib
import json
import logging
import re
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows development hosts
    fcntl = None

from app.config import settings
from app.db import connect
from app.identity_sync import IdentitySyncError, sync_identity_records
from app.security import hash_token, now_iso
from app.subscription_identity import derive_subscription_identity
from app.subscription_token_vault import decrypt_subscription_token, encrypt_subscription_token


TOKEN_PREFIX = "lsub_"
MAX_EXPIRY_DAYS = 3650
IDENTITY_VERSION = 2
_sync_lock = threading.Lock()
_create_worker_lock = threading.Lock()
_create_worker_wake = threading.Event()
_create_worker_started = False
_CREATE_KEY_PATTERN = re.compile(r"^scr_[A-Za-z0-9_-]{20,80}$")
_logger = logging.getLogger(__name__)


class SubscriptionIdentityUnavailable(RuntimeError):
    pass


class SubscriptionCreateConflict(ValueError):
    pass


@contextmanager
def _identity_operation_lock():
    lock_path = settings.database_path.parent / ".subscription-identity.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _sync_lock:
        with lock_path.open("a+b") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
            else:
                import msvcrt

                lock_file.seek(0, 2)
                if lock_file.tell() == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
                else:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def _clean_label(label: str) -> str:
    value = " ".join((label or "").split())[:80]
    if not value:
        raise ValueError("A link label is required.")
    return value


def _expires_at(expires_in_days: int) -> str | None:
    if expires_in_days < 0 or expires_in_days > MAX_EXPIRY_DAYS:
        raise ValueError(f"Expiry days must be between 0 and {MAX_EXPIRY_DAYS}.")
    if expires_in_days == 0:
        return None
    return (datetime.now(timezone.utc) + timedelta(days=expires_in_days)).isoformat(timespec="seconds")


def _is_expired(value: str | None) -> bool:
    if not value:
        return False
    try:
        return datetime.fromisoformat(value) <= datetime.now(timezone.utc)
    except ValueError:
        return True


def subscription_files() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT file_id, original_filename, stored_filename, sha256, size_bytes,
                   storage_backend, visibility, link_enabled, status, updated_at
            FROM files
            WHERE status='active'
              AND (lower(original_filename) IN ('subscription.yml', 'subscription.yaml')
                   OR lower(stored_filename) IN ('subscription.yml', 'subscription.yaml'))
            ORDER BY updated_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def protect_subscription_file(file_id: str) -> bool:
    now = now_iso()
    with connect() as conn:
        row = conn.execute(
            "SELECT file_id FROM files WHERE file_id=? AND status='active'",
            (file_id,),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            """
            UPDATE files
            SET visibility='private', link_enabled=0, public_url=NULL,
                link_revoked_at=?, updated_at=?
            WHERE file_id=?
            """,
            (now, now, file_id),
        )
        conn.commit()
    return True


def _desired_identity_records(extra_token_hash: str | None = None) -> list[dict]:
    now = now_iso()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT token_hash
            FROM subscription_tokens
            WHERE status='active' AND identity_version=?
              AND (expires_at IS NULL OR expires_at>?)
            ORDER BY id
            """,
            (IDENTITY_VERSION, now),
        ).fetchall()
    hashes = [row["token_hash"] for row in rows]
    if extra_token_hash and extra_token_hash not in hashes:
        hashes.append(extra_token_hash)
    return [derive_subscription_identity(value).as_record() for value in hashes]


def _record_identity_health(
    *,
    success: bool,
    target_count: int = 0,
    identity_count: int = 0,
    error: str | None = None,
) -> None:
    attempted_at = now_iso()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO subscription_identity_health(
                id, last_attempt_at, last_success_at, last_error, target_count, identity_count
            ) VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                last_attempt_at=excluded.last_attempt_at,
                last_success_at=CASE
                    WHEN excluded.last_error IS NULL THEN excluded.last_success_at
                    ELSE subscription_identity_health.last_success_at
                END,
                last_error=excluded.last_error,
                target_count=excluded.target_count,
                identity_count=excluded.identity_count
            """,
            (
                attempted_at,
                attempted_at if success else None,
                None if success else (error or "Node identity attestation failed.")[:500],
                target_count,
                identity_count,
            ),
        )
        conn.commit()


def _identity_health_is_fresh(health: dict) -> bool:
    if health.get("last_error") or not health.get("last_success_at"):
        return False
    try:
        success_at = datetime.fromisoformat(health["last_success_at"])
    except (TypeError, ValueError):
        return False
    max_age = max(30, settings.subscription_identity_attestation_max_age_seconds)
    return (datetime.now(timezone.utc) - success_at).total_seconds() <= max_age


def subscription_identity_health() -> dict:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT last_attempt_at, last_success_at, last_error, target_count, identity_count
            FROM subscription_identity_health WHERE id=1
            """
        ).fetchone()
    result = dict(row) if row else {
        "last_attempt_at": None,
        "last_success_at": None,
        "last_error": "Node identity attestation has not run.",
        "target_count": 0,
        "identity_count": 0,
    }
    result["fresh"] = _identity_health_is_fresh(result)
    return result


def _sync_desired_attested(extra_token_hash: str | None = None) -> list[dict]:
    records = _desired_identity_records(extra_token_hash)
    try:
        results = sync_identity_records(records)
    except (IdentitySyncError, RuntimeError, ValueError) as exc:
        _record_identity_health(success=False, identity_count=len(records), error=str(exc))
        raise
    _record_identity_health(
        success=True,
        target_count=len(results),
        identity_count=len(records),
    )
    return results


def ensure_subscription_identities_ready() -> dict:
    with _identity_operation_lock():
        try:
            _sync_desired_attested()
        except (IdentitySyncError, RuntimeError, ValueError) as exc:
            raise SubscriptionIdentityUnavailable(
                "Proxy node credentials are not currently attested."
            ) from exc
        return subscription_identity_health()


def _subscription_create_fingerprint(
    file_id: str,
    label: str,
    created_by: int | None,
    expires_in_days: int,
) -> str:
    payload = json.dumps(
        {
            "created_by": created_by,
            "expires_in_days": expires_in_days,
            "file_id": file_id,
            "label": label,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def prepare_subscription_create(created_by: int | None) -> str:
    idempotency_key = "scr_" + secrets.token_urlsafe(24)
    now = now_iso()
    stale_before = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute(
            "DELETE FROM subscription_create_requests WHERE status='ready' AND created_at<?",
            (stale_before,),
        )
        conn.execute(
            """
            INSERT INTO subscription_create_requests(
                idempotency_key, created_by, status, created_at, updated_at
            ) VALUES (?, ?, 'ready', ?, ?)
            """,
            (idempotency_key, created_by, now, now),
        )
        conn.commit()
    return idempotency_key


def _subscription_create_summary(row, *, newly_enqueued: bool = False) -> dict:
    return {
        "idempotency_key": row["idempotency_key"],
        "id": row["token_id"],
        "label": row["label"],
        "status": row["status"],
        "newly_enqueued": newly_enqueued,
    }


def enqueue_subscription_link(
    file_id: str,
    label: str,
    created_by: int | None,
    expires_in_days: int,
    idempotency_key: str,
) -> dict:
    if not _CREATE_KEY_PATTERN.fullmatch(idempotency_key or ""):
        raise SubscriptionCreateConflict("The create request is invalid or expired. Reload the page and try again.")
    file_id = (file_id or "").strip()
    label = _clean_label(label)
    expires_in_days = int(expires_in_days)
    expires_at = _expires_at(expires_in_days)
    fingerprint = _subscription_create_fingerprint(file_id, label, created_by, expires_in_days)
    now = now_iso()

    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        request_row = conn.execute(
            "SELECT * FROM subscription_create_requests WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if not request_row or request_row["created_by"] != created_by:
            raise SubscriptionCreateConflict("The create request is invalid or expired. Reload the page and try again.")
        if request_row["request_fingerprint"]:
            if request_row["request_fingerprint"] != fingerprint:
                raise SubscriptionCreateConflict(
                    "This create request was already used with different parameters. Reload the page before trying again."
                )
            return _subscription_create_summary(request_row)
        if request_row["status"] != "ready":
            raise SubscriptionCreateConflict("The create request cannot be reused. Reload the page and try again.")

        file_row = conn.execute(
            """
            SELECT file_id, storage_backend FROM files
            WHERE file_id=? AND status='active'
              AND (lower(original_filename) IN ('subscription.yml', 'subscription.yaml')
                   OR lower(stored_filename) IN ('subscription.yml', 'subscription.yaml'))
            """,
            (file_id,),
        ).fetchone()
        if not file_row:
            raise ValueError("The selected active subscription file does not exist.")
        if file_row["storage_backend"] not in {"local_temp", "cos", "cos_sv"}:
            raise ValueError("Protected subscriptions require a server-mounted storage backend.")

        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        digest = hash_token(token)
        ciphertext = encrypt_subscription_token(token)
        cursor = conn.execute(
            """
            INSERT INTO subscription_tokens(
                file_id, label, token_prefix, token_hash, token_ciphertext, status,
                created_by, created_at, expires_at, identity_version
            ) VALUES (?, ?, ?, ?, ?, 'provisioning', ?, ?, ?, ?)
            """,
            (file_id, label, token[:14], digest, ciphertext, created_by, now, expires_at, IDENTITY_VERSION),
        )
        token_id = cursor.lastrowid
        conn.execute(
            """
            UPDATE subscription_create_requests
            SET request_fingerprint=?, file_id=?, label=?, expires_in_days=?, token_id=?,
                status='queued', error_message=NULL, updated_at=?
            WHERE idempotency_key=? AND status='ready'
            """,
            (fingerprint, file_id, label, expires_in_days, token_id, now, idempotency_key),
        )
        conn.execute(
            """
            UPDATE files
            SET visibility='private', link_enabled=0, public_url=NULL,
                link_revoked_at=COALESCE(link_revoked_at, ?), updated_at=?
            WHERE file_id=?
            """,
            (now, now, file_id),
        )
        conn.commit()

    _create_worker_wake.set()
    return {
        "idempotency_key": idempotency_key,
        "id": token_id,
        "label": label,
        "status": "queued",
        "newly_enqueued": True,
    }


def get_subscription_create_request(idempotency_key: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT r.idempotency_key, r.token_id, r.label, r.status, r.error_message,
                   t.status AS token_status, t.token_hash, t.token_ciphertext, t.expires_at
            FROM subscription_create_requests r
            LEFT JOIN subscription_tokens t ON t.id=r.token_id
            WHERE r.idempotency_key=?
            """,
            (idempotency_key,),
        ).fetchone()
    if not row:
        return None
    result = {
        "idempotency_key": row["idempotency_key"],
        "id": row["token_id"],
        "label": row["label"],
        "status": row["status"],
        "error": row["error_message"],
        "url": None,
        "expires_at": row["expires_at"],
    }
    if (
        row["status"] == "succeeded"
        and row["token_status"] == "active"
        and row["token_ciphertext"]
        and not _is_expired(row["expires_at"])
    ):
        token = decrypt_subscription_token(row["token_ciphertext"])
        if token and hash_token(token) == row["token_hash"]:
            result["url"] = f"{settings.app_base_url}/sub/{token}"
    return result


def process_subscription_create_request(idempotency_key: str) -> dict | None:
    with _identity_operation_lock():
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT r.status AS request_status, r.token_id,
                       t.status AS token_status, t.token_hash
                FROM subscription_create_requests r
                LEFT JOIN subscription_tokens t ON t.id=r.token_id
                WHERE r.idempotency_key=?
                """,
                (idempotency_key,),
            ).fetchone()
            if not row or row["request_status"] not in {"queued", "processing"}:
                return get_subscription_create_request(idempotency_key)
            if row["token_status"] == "active":
                conn.execute(
                    """
                    UPDATE subscription_create_requests
                    SET status='succeeded', error_message=NULL, updated_at=?, completed_at=COALESCE(completed_at, ?)
                    WHERE idempotency_key=?
                    """,
                    (now_iso(), now_iso(), idempotency_key),
                )
                conn.commit()
                return get_subscription_create_request(idempotency_key)
            if row["token_status"] != "provisioning" or not row["token_hash"]:
                message = "The queued subscription link is no longer available; no new link was issued."
                conn.execute(
                    """
                    UPDATE subscription_create_requests
                    SET status='failed', error_message=?, updated_at=?, completed_at=COALESCE(completed_at, ?)
                    WHERE idempotency_key=?
                    """,
                    (message, now_iso(), now_iso(), idempotency_key),
                )
                conn.commit()
                return get_subscription_create_request(idempotency_key)
            token_id = row["token_id"]
            digest = row["token_hash"]
            conn.execute(
                """
                UPDATE subscription_create_requests
                SET status='processing', error_message=NULL, updated_at=?
                WHERE idempotency_key=?
                """,
                (now_iso(), idempotency_key),
            )
            conn.commit()

        try:
            _sync_desired_attested(digest)
        except (IdentitySyncError, RuntimeError, ValueError) as exc:
            message = "Node credentials could not be provisioned on every region; no link was issued."
            with connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    UPDATE subscription_tokens
                    SET status='provision_failed', token_ciphertext=NULL, sync_error=?
                    WHERE id=? AND status='provisioning'
                    """,
                    (str(exc)[:500], token_id),
                )
                conn.execute(
                    """
                    UPDATE subscription_create_requests
                    SET status='failed', error_message=?, updated_at=?, completed_at=?
                    WHERE idempotency_key=? AND status IN ('queued','processing')
                    """,
                    (message, now_iso(), now_iso(), idempotency_key),
                )
                conn.commit()
            try:
                _sync_desired_attested()
            except Exception:
                pass
            return get_subscription_create_request(idempotency_key)

        completed_at = now_iso()
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE subscription_tokens
                SET status='active', identity_synced_at=?, sync_error=NULL
                WHERE id=? AND status='provisioning'
                """,
                (completed_at, token_id),
            )
            conn.execute(
                """
                UPDATE subscription_create_requests
                SET status='succeeded', error_message=NULL, updated_at=?, completed_at=?
                WHERE idempotency_key=? AND status IN ('queued','processing')
                """,
                (completed_at, completed_at, idempotency_key),
            )
            conn.commit()
    return get_subscription_create_request(idempotency_key)


def _next_subscription_create_request() -> str | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT idempotency_key
            FROM subscription_create_requests
            WHERE status IN ('queued','processing')
            ORDER BY created_at, idempotency_key
            LIMIT 1
            """
        ).fetchone()
    return row["idempotency_key"] if row else None


def _subscription_create_worker() -> None:
    while True:
        try:
            idempotency_key = _next_subscription_create_request()
            if not idempotency_key:
                _create_worker_wake.wait(timeout=1.0)
                _create_worker_wake.clear()
                continue
            process_subscription_create_request(idempotency_key)
        except Exception as exc:
            _logger.error("Subscription create worker iteration failed: %s", type(exc).__name__)
            time.sleep(1.0)


def start_subscription_create_worker() -> None:
    global _create_worker_started
    with _create_worker_lock:
        if _create_worker_started:
            _create_worker_wake.set()
            return
        worker = threading.Thread(
            target=_subscription_create_worker,
            name="subscription-create-worker",
            daemon=True,
        )
        worker.start()
        _create_worker_started = True
    _create_worker_wake.set()


def create_subscription_link(
    file_id: str,
    label: str,
    created_by: int | None,
    expires_in_days: int = 30,
) -> dict:
    idempotency_key = prepare_subscription_create(created_by)
    enqueue_subscription_link(file_id, label, created_by, expires_in_days, idempotency_key)
    result = process_subscription_create_request(idempotency_key)
    if not result or result["status"] != "succeeded" or not result["url"]:
        message = result["error"] if result else "Subscription creation did not complete."
        raise ValueError(message)
    return {
        "id": result["id"],
        "label": result["label"],
        "url": result["url"],
        "expires_at": result["expires_at"],
    }


def resolve_subscription_token(token: str, record_use: bool = True) -> dict | None:
    if not token.startswith(TOKEN_PREFIX) or len(token) < 30 or len(token) > 80:
        return None
    digest = hash_token(token)
    with connect() as conn:
        row = conn.execute(
            """
            SELECT t.id AS subscription_token_id, t.file_id, t.status AS token_status,
                   t.expires_at AS token_expires_at, t.identity_version,
                   f.status AS file_status
            FROM subscription_tokens t
            JOIN files f ON f.file_id=t.file_id
            WHERE t.token_hash=?
            """,
            (digest,),
        ).fetchone()
        if (
            not row
            or row["token_status"] != "active"
            or row["file_status"] != "active"
            or row["identity_version"] != IDENTITY_VERSION
        ):
            return None
        if _is_expired(row["token_expires_at"]):
            conn.execute(
                "UPDATE subscription_tokens SET status='expired_pending', token_ciphertext=NULL WHERE id=? AND status='active'",
                (row["subscription_token_id"],),
            )
            conn.commit()
            return None
        if record_use:
            conn.execute(
                "UPDATE subscription_tokens SET last_used_at=?, use_count=use_count+1 WHERE id=?",
                (now_iso(), row["subscription_token_id"]),
            )
            conn.commit()
        result = dict(row)
        result["token_hash"] = digest
        return result


def list_subscription_links(include_display_urls: bool = False) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT t.id, t.file_id, t.label, t.token_prefix, t.token_hash,
                   t.token_ciphertext, t.status, t.created_at,
                   t.expires_at, t.last_used_at, t.use_count, t.revoked_at,
                   t.identity_version, t.identity_synced_at, t.sync_error,
                   f.original_filename, f.sha256
            FROM subscription_tokens t
            JOIN files f ON f.file_id=t.file_id
            ORDER BY t.created_at DESC, t.id DESC
            """
        ).fetchall()
    links = []
    for row in rows:
        link = dict(row)
        link["url"] = None
        link["display_error"] = None
        if (
            include_display_urls
            and link["status"] == "active"
            and not _is_expired(link["expires_at"])
            and link["token_ciphertext"]
        ):
            token = decrypt_subscription_token(link["token_ciphertext"])
            if token and hash_token(token) == link["token_hash"]:
                link["url"] = f"{settings.app_base_url}/sub/{token}"
            else:
                link["display_error"] = "The stored link could not be decrypted."
        link.pop("token_hash", None)
        link.pop("token_ciphertext", None)
        links.append(link)
    return links


def record_subscription_token_use(token_id: int) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE subscription_tokens
            SET last_used_at=?, use_count=use_count+1
            WHERE id=? AND status='active'
            """,
            (now_iso(), token_id),
        )
        conn.commit()


def revoke_subscription_link(token_id: int) -> str:
    with _identity_operation_lock():
        with connect() as conn:
            row = conn.execute(
                "SELECT status, identity_version FROM subscription_tokens WHERE id=?",
                (token_id,),
            ).fetchone()
            if not row or row["status"] not in {"active", "revoke_pending", "expired_pending"}:
                return "already_inactive"
            if row["identity_version"] != IDENTITY_VERSION:
                conn.execute(
                    "UPDATE subscription_tokens SET status='revoked', token_ciphertext=NULL, revoked_at=? WHERE id=?",
                    (now_iso(), token_id),
                )
                conn.commit()
                return "revoked"
            conn.execute(
                "UPDATE subscription_tokens SET status='revoke_pending', token_ciphertext=NULL, revoked_at=COALESCE(revoked_at, ?), sync_error=NULL WHERE id=?",
                (now_iso(), token_id),
            )
            conn.commit()
        try:
            _sync_desired_attested()
        except (IdentitySyncError, RuntimeError, ValueError) as exc:
            with connect() as conn:
                conn.execute(
                    "UPDATE subscription_tokens SET sync_error=? WHERE id=?",
                    (str(exc)[:500], token_id),
                )
                conn.commit()
            return "revoke_pending"
        with connect() as conn:
            conn.execute(
                "UPDATE subscription_tokens SET status='revoked', identity_synced_at=?, sync_error=NULL WHERE id=?",
                (now_iso(), token_id),
            )
            conn.commit()
        return "revoked"


def reconcile_subscription_identities() -> dict:
    with _identity_operation_lock():
        now = now_iso()
        with connect() as conn:
            conn.execute(
                """
                UPDATE subscription_tokens
                SET status='expired_pending', token_ciphertext=NULL,
                    revoked_at=COALESCE(revoked_at, ?)
                WHERE status='active' AND identity_version=?
                  AND expires_at IS NOT NULL AND expires_at<=?
                """,
                (now, IDENTITY_VERSION, now),
            )
            conn.execute(
                """
                UPDATE subscription_tokens
                SET status='provision_failed', token_ciphertext=NULL,
                    sync_error=COALESCE(sync_error, 'Provisioning interrupted before activation.')
                WHERE status='provisioning' AND identity_version=?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM subscription_create_requests r
                      WHERE r.token_id=subscription_tokens.id
                        AND r.status IN ('queued','processing')
                  )
                """,
                (IDENTITY_VERSION,),
            )
            pending = conn.execute(
                "SELECT COUNT(*) AS count FROM subscription_tokens WHERE status IN ('revoke_pending','expired_pending')"
            ).fetchone()["count"]
            conn.commit()

        _sync_desired_attested()
        synced_at = now_iso()
        with connect() as conn:
            revoked = conn.execute(
                """
                UPDATE subscription_tokens
                SET status='revoked', identity_synced_at=?, sync_error=NULL
                WHERE status='revoke_pending'
                """,
                (synced_at,),
            ).rowcount
            expired = conn.execute(
                """
                UPDATE subscription_tokens
                SET status='expired', identity_synced_at=?, sync_error=NULL
                WHERE status='expired_pending'
                """,
                (synced_at,),
            ).rowcount
            active = conn.execute(
                "SELECT COUNT(*) AS count FROM subscription_tokens WHERE status='active' AND identity_version=?",
                (IDENTITY_VERSION,),
            ).fetchone()["count"]
            conn.commit()
    return {"active": active, "pending_before": pending, "revoked": revoked, "expired": expired}
