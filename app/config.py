import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _bool(name: str, default: bool = False) -> bool:
    return _env(name, "true" if default else "false").strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)) or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    app_name: str = _env("APP_NAME", "LuomoFile Hub")
    app_env: str = _env("APP_ENV", "production")
    app_base_url: str = (_env("APP_BASE_URL", "https://file.luomo.moe") or "https://file.luomo.moe").rstrip("/")
    database_path: Path = Path(_env("DATABASE_PATH", "/app/data/luomofile.db"))
    session_secret: str = _env("SESSION_SECRET", "")
    public_link_secret: str = _env("PUBLIC_LINK_SECRET", "")
    admin_username: str = _env("ADMIN_USERNAME", "luomo")
    admin_password_hash: str = _env("ADMIN_PASSWORD_HASH", "")
    smtp_host: str = _env("SMTP_HOST", "")
    smtp_port: int = _int("SMTP_PORT", 465)
    smtp_username: str = _env("SMTP_USERNAME", "")
    smtp_password: str = _env("SMTP_PASSWORD", "")
    smtp_from: str = _env("SMTP_FROM", _env("SMTP_USERNAME", ""))
    smtp_use_ssl: bool = _bool("SMTP_USE_SSL", True)
    email_verification_ttl_seconds: int = _int("EMAIL_VERIFICATION_TTL_SECONDS", 86400)
    internal_token: str = _env("LUOMOFILE_INTERNAL_TOKEN", "")
    app_version: str = _env("APP_VERSION", "0.1.0")

    max_upload_bytes: int = _int("MAX_UPLOAD_BYTES", 524288000)
    max_image_bytes: int = _int("MAX_IMAGE_BYTES", 52428800)
    max_temp_bytes: int = _int("MAX_TEMP_BYTES", 209715200)
    allow_anonymous_upload: bool = _bool("ALLOW_ANONYMOUS_UPLOAD", True)
    anonymous_max_upload_bytes: int = _int("ANONYMOUS_MAX_UPLOAD_BYTES", 10485760)
    default_temp_ttl_seconds: int = _int("DEFAULT_TEMP_TTL_SECONDS", 86400)

    r2_enabled: bool = _bool("R2_ENABLED", False)
    r2_account_id: str = _env("R2_ACCOUNT_ID")
    r2_access_key_id: str = _env("R2_ACCESS_KEY_ID")
    r2_secret_access_key: str = _env("R2_SECRET_ACCESS_KEY")
    r2_bucket: str = _env("R2_BUCKET")
    r2_public_base_url: str = _env("R2_PUBLIC_BASE_URL", "https://file-r2.luomo.moe").rstrip("/")
    r2_max_capacity_bytes: int = _int("R2_MAX_CAPACITY_BYTES", 10737418240)

    cos_enabled: bool = _bool("COS_ENABLED", True)
    cos_secret_id: str = _env("COS_SECRET_ID")
    cos_secret_key: str = _env("COS_SECRET_KEY")
    cos_region: str = _env("COS_REGION")
    cos_bucket: str = _env("COS_BUCKET")
    cos_public_base_url: str = _env("COS_PUBLIC_BASE_URL", "https://cos-file.luomo.moe").rstrip("/")
    cos_max_capacity_bytes: int = _int("COS_MAX_CAPACITY_BYTES", 53687091200)
    cos_mount_path: Path = Path(_env("COS_MOUNT_PATH", "/mnt/cos"))

    cos_sv_enabled: bool = _bool("COS_SV_ENABLED", False)
    cos_sv_public_base_url: str = _env("COS_SV_PUBLIC_BASE_URL", "https://cos-file.luomo.moe").rstrip("/")
    cos_sv_max_capacity_bytes: int = _int("COS_SV_MAX_CAPACITY_BYTES", 53687091200)
    cos_sv_mount_path: Path = Path(_env("COS_SV_MOUNT_PATH", "/mnt/cos_sv"))

    local_temp_enabled: bool = _bool("LOCAL_TEMP_ENABLED", True)
    local_temp_path: Path = Path(_env("LOCAL_TEMP_PATH", "/app/data/tmp"))
    local_temp_max_capacity_bytes: int = _int("LOCAL_TEMP_MAX_CAPACITY_BYTES", 1073741824)
    cleanup_interval_seconds: int = _int("CLEANUP_INTERVAL_SECONDS", 300)
    storage_refresh_interval_seconds: int = _int("STORAGE_REFRESH_INTERVAL_SECONDS", 600)


settings = Settings()


def ensure_dirs() -> None:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    settings.local_temp_path.mkdir(parents=True, exist_ok=True)
    (settings.database_path.parent / "uploads").mkdir(parents=True, exist_ok=True)
