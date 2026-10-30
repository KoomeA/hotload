"""Where finished files live: the worker's local disk, or an S3-compatible bucket (AWS S3, Cloudflare R2, MinIO).

With S3, files are uploaded when a job finishes and the local copy is deleted; downloads are short-lived
signed redirects (the API never proxies the bytes) and batch ZIPs stream straight from the bucket.
Expiry: `cleanup_expired` deletes objects older than the TTL. This works on every S3-compatible store; a bucket
lifecycle rule is a good second layer where supported.
"""

import re
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

from .config import Settings


class StorageError(Exception):
    pass


def job_dir(output_dir: Path, job_id: str) -> Path:
    return output_dir / job_id


def cleanup_expired(output_dir: Path, ttl_seconds: int, now: float | None = None) -> int:
    """Delete local job directories older than the TTL. Returns the number removed."""
    if not output_dir.exists():
        return 0
    now = now or time.time()
    removed = 0
    for child in output_dir.iterdir():
        if child.is_dir() and now - child.stat().st_mtime > ttl_seconds:
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^\w .()\[\]-]", "_", name).lstrip(". ")[:180]  # no "." / ".." segments, no hidden names
    return cleaned or "download"


class S3Storage:
    """Blocking boto3 calls: run them in a thread from async code."""

    def __init__(self, settings: Settings, client=None):
        import boto3
        from botocore.config import Config

        self.bucket = settings.s3_bucket or ""
        self.prefix = settings.s3_prefix
        self.presign_seconds = settings.s3_presign_seconds
        self.client = client or boto3.client(
            "s3", endpoint_url=settings.s3_endpoint_url, region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id, aws_secret_access_key=settings.s3_secret_access_key,
            config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"},
                          connect_timeout=10, read_timeout=60),
        )

    @staticmethod
    def enabled(settings: Settings) -> bool:
        return settings.storage_backend == "s3" and bool(settings.s3_bucket)

    def key_for(self, job_id: str, filename: str) -> str:
        return f"{self.prefix}{job_id}/{_safe_name(filename)}"

    def put(self, job_id: str, path: Path) -> str:
        key = self.key_for(job_id, path.name)
        try:
            self.client.upload_file(str(path), self.bucket, key)
        except Exception as exc:  # noqa: BLE001 - botocore raises many types
            raise StorageError("Could not store the finished file") from exc
        return key

    def presigned_url(self, key: str, filename: str) -> str:
        return self.client.generate_presigned_url(
            "get_object", ExpiresIn=self.presign_seconds,
            Params={"Bucket": self.bucket, "Key": key,
                    "ResponseContentDisposition": f'attachment; filename="{_safe_name(filename)}"'},
        )

    def chunks(self, key: str, size: int = 1 << 20) -> Iterator[bytes]:
        body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
        try:
            yield from body.iter_chunks(size)
        finally:
            body.close()

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001
            return False

    def cleanup_expired(self, ttl_seconds: int, now: float | None = None) -> int:
        """Delete objects under the prefix older than the TTL. Returns the number removed."""
        cutoff = (now or time.time()) - ttl_seconds
        removed = 0
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self.prefix):
            stale = [{"Key": o["Key"]} for o in page.get("Contents", []) if o["LastModified"].timestamp() < cutoff]
            if stale:
                self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": stale, "Quiet": True})
                removed += len(stale)
        return removed


def build_remote(settings: Settings) -> "S3Storage | None":
    return S3Storage(settings) if S3Storage.enabled(settings) else None
