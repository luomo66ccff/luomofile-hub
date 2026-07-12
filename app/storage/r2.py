from pathlib import Path

import boto3

from app.config import settings
from app.storage.base import StorageBackend


class R2Backend(StorageBackend):
    name = "r2"

    def __init__(self):
        self.client = None
        if settings.r2_account_id and settings.r2_access_key_id and settings.r2_secret_access_key:
            self.client = boto3.client(
                "s3",
                endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
                aws_access_key_id=settings.r2_access_key_id,
                aws_secret_access_key=settings.r2_secret_access_key,
                region_name="auto",
            )

    def put_file(self, local_path: Path, object_key: str, content_type: str | None) -> None:
        if not self.client or not settings.r2_bucket:
            raise RuntimeError("R2 backend is not configured")
        extra = {"ContentType": content_type} if content_type else {}
        self.client.upload_file(str(local_path), settings.r2_bucket, object_key, ExtraArgs=extra)

    def delete_file(self, object_key: str) -> None:
        if self.client and settings.r2_bucket:
            self.client.delete_object(Bucket=settings.r2_bucket, Key=object_key)

    def get_public_url(self, object_key: str) -> str | None:
        return f"{settings.r2_public_base_url}/{object_key}" if settings.r2_public_base_url else None

    def exists(self, object_key: str) -> bool:
        if not self.client or not settings.r2_bucket:
            return False
        try:
            self.client.head_object(Bucket=settings.r2_bucket, Key=object_key)
            return True
        except Exception:
            return False

    def stat(self, object_key: str) -> dict:
        if not self.client or not settings.r2_bucket:
            return {"size": 0}
        item = self.client.head_object(Bucket=settings.r2_bucket, Key=object_key)
        return {"size": item.get("ContentLength", 0)}
