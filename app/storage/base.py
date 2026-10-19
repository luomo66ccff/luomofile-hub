from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


@dataclass
class BackendInfo:
    name: str
    type: str
    enabled: bool
    bucket_name: str | None
    public_base_url: str | None
    max_capacity_bytes: int
    used_bytes_cache: int
    reserved_bytes: int = 0

    @property
    def available_bytes(self) -> int:
        return max(0, int(self.max_capacity_bytes or 0) - int(self.used_bytes_cache or 0) - int(self.reserved_bytes or 0))


class StorageBackend:
    name = "base"

    def put_file(self, local_path: Path, object_key: str, content_type: str | None) -> None:
        raise NotImplementedError

    def delete_file(self, object_key: str) -> None:
        raise NotImplementedError

    def get_public_url(self, object_key: str) -> str | None:
        raise NotImplementedError

    def open_reader(self, object_key: str) -> BinaryIO:
        """Read with server-side credentials, never through a public URL."""
        raise NotImplementedError

    def exists(self, object_key: str) -> bool:
        raise NotImplementedError

    def stat(self, object_key: str) -> dict:
        raise NotImplementedError
