import shutil
from pathlib import Path

from app.config import settings
from app.storage.base import StorageBackend


class LocalTempBackend(StorageBackend):
    name = "local_temp"

    def __init__(self, root: Path | None = None):
        self.root = root or settings.local_temp_path
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, object_key: str) -> Path:
        target = (self.root / object_key).resolve()
        if not str(target).startswith(str(self.root.resolve())):
            raise ValueError("Invalid object key")
        return target

    def put_file(self, local_path: Path, object_key: str, content_type: str | None) -> None:
        target = self._path(object_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_path, target)

    def delete_file(self, object_key: str) -> None:
        path = self._path(object_key)
        if path.exists():
            path.unlink()

    def get_public_url(self, object_key: str) -> str | None:
        return None

    def exists(self, object_key: str) -> bool:
        return self._path(object_key).exists()

    def stat(self, object_key: str) -> dict:
        path = self._path(object_key)
        return {"size": path.stat().st_size if path.exists() else 0}
