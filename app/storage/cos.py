import shutil
from pathlib import Path

import boto3

from app.config import settings
from app.storage.base import StorageBackend


class CosBackend(StorageBackend):
    name = "cos"

    def __init__(self, name: str = "cos", mount_path: Path | None = None, public_base_url: str | None = None):
        self.name = name
        self.mount_path = mount_path or settings.cos_mount_path
        self.public_base_url = public_base_url if public_base_url is not None else settings.cos_public_base_url
        self.use_mount = self.mount_path.exists()
        self.client = None
        if not self.use_mount and settings.cos_secret_id and settings.cos_secret_key and settings.cos_region:
            endpoint = f"https://cos.{settings.cos_region}.myqcloud.com"
            self.client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=settings.cos_secret_id,
                aws_secret_access_key=settings.cos_secret_key,
                region_name=settings.cos_region,
            )

    def _path(self, object_key: str) -> Path:
        target = (self.mount_path / object_key).resolve()
        if not str(target).startswith(str(self.mount_path.resolve())):
            raise ValueError("Invalid object key")
        return target

    def put_file(self, local_path: Path, object_key: str, content_type: str | None) -> None:
        if self.use_mount:
            target = self._path(object_key)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(local_path, target)
            return
        if not self.client or not settings.cos_bucket:
            raise RuntimeError("COS backend is not configured")
        extra = {"ContentType": content_type} if content_type else {}
        self.client.upload_file(str(local_path), settings.cos_bucket, object_key, ExtraArgs=extra)

    def delete_file(self, object_key: str) -> None:
        if self.use_mount:
            path = self._path(object_key)
            if path.exists():
                path.unlink()
            return
        if self.client and settings.cos_bucket:
            self.client.delete_object(Bucket=settings.cos_bucket, Key=object_key)

    def get_public_url(self, object_key: str) -> str | None:
        return f"{self.public_base_url}/{object_key}" if self.public_base_url else None

    def exists(self, object_key: str) -> bool:
        if self.use_mount:
            return self._path(object_key).exists()
        if not self.client or not settings.cos_bucket:
            return False
        try:
            self.client.head_object(Bucket=settings.cos_bucket, Key=object_key)
            return True
        except Exception:
            return False

    def stat(self, object_key: str) -> dict:
        if self.use_mount:
            path = self._path(object_key)
            return {"size": path.stat().st_size if path.exists() else 0}
        if not self.client or not settings.cos_bucket:
            return {"size": 0}
        item = self.client.head_object(Bucket=settings.cos_bucket, Key=object_key)
        return {"size": item.get("ContentLength", 0)}


class CosSvBackend(CosBackend):
    name = "cos_sv"

    def __init__(self):
        super().__init__(
            name="cos_sv",
            mount_path=settings.cos_sv_mount_path,
            public_base_url=settings.cos_sv_public_base_url,
        )
