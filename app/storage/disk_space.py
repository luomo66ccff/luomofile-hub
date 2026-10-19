"""Keep a real filesystem reserve for private media and upload staging."""

from pathlib import Path
import shutil

from fastapi import HTTPException

from app.config import settings


def ensure_disk_space(path: Path, additional_bytes: int = 0) -> None:
    probe = Path(path).resolve()
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    reserve = max(0, int(getattr(settings, "media_disk_reserve_bytes", 512 * 1024**2)))
    required = max(0, int(additional_bytes))
    if shutil.disk_usage(probe).free < reserve + required:
        raise HTTPException(status_code=507, detail={
            "error": "disk_reserve_required", "message": "服务器可用空间不足，已暂停写入以保留运行空间。"
        })
