"""Private, persistent image originals, never mounted under a public URL."""

from app.config import settings
from app.storage.local_temp import LocalTempBackend
from app.storage.disk_space import ensure_disk_space


class PrivateMediaBackend(LocalTempBackend):
    name = "private_media"

    def __init__(self):
        super().__init__(root=settings.database_path.parent / "media-originals")

    def put_file(self, local_path, object_key, content_type):
        ensure_disk_space(self.root, local_path.stat().st_size)
        super().put_file(local_path, object_key, content_type)
