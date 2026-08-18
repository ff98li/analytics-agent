"""MinIO/S3 blob storage for the gateway."""

from __future__ import annotations

from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from .config import GatewayConfig


class QuotaExceeded(Exception):
    """Upload exceeds the configured blob quota."""


class BlobMissing(Exception):
    """Blob key not present in the bucket."""


class Storage:
    """Thin boto3 wrapper over an S3-compatible store (MinIO).

    Two-partition model (public-read-only vs private-read-write): blob keys
    whose first path segment matches ``cfg.public_bucket`` route to the public
    bucket; every other key routes to the private bucket. The public bucket
    carries an anonymous-download bucket policy; the private bucket requires
    credentials for everything.
    """

    def __init__(self, cfg: GatewayConfig) -> None:
        self._private_bucket = cfg.s3_bucket
        self._public_bucket = cfg.public_bucket
        kwargs: dict[str, Any] = {
            "service_name": "s3",
            "region_name": cfg.s3_region,
            "config": BotoConfig(signature_version="s3v4", retries={"max_attempts": 3}),
        }
        if cfg.s3_endpoint_url:
            kwargs["endpoint_url"] = cfg.s3_endpoint_url
        if cfg.s3_access_key and cfg.s3_secret_key:
            kwargs["aws_access_key_id"] = cfg.s3_access_key
            kwargs["aws_secret_access_key"] = cfg.s3_secret_key
        self._client = boto3.client(**kwargs)

    def bucket_for_key(self, key: str) -> str:
        """Resolve the partition bucket for a blob key."""
        first_segment = key.split("/", 1)[0]
        if first_segment == self._public_bucket:
            return self._public_bucket
        return self._private_bucket

    def put_blob(self, key: str, body: bytes, content_type: str, max_bytes: int) -> None:
        if len(body) > max_bytes:
            raise QuotaExceeded(f"blob {key!r} exceeds quota of {max_bytes} bytes")
        self._client.put_object(
            Bucket=self.bucket_for_key(key),
            Key=key,
            Body=body,
            ContentType=content_type,
        )

    def get_blob(self, key: str) -> tuple[bytes, str]:
        try:
            resp = self._client.get_object(
                Bucket=self.bucket_for_key(key), Key=key
            )
        except ClientError as exc:
            if _is_not_found(exc):
                raise BlobMissing(key) from exc
            raise
        body = resp["Body"].read()
        content_type = resp.get("ContentType") or "application/octet-stream"
        return body, content_type

    def list_blobs(
        self, prefix: str, delimiter: str, limit: int
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return ``(objects, truncated)``.

        ``objects`` entries: ``{"key": str, "size": int | None}``. Folder
        prefixes from a delimited listing are merged in as objects with
        ``size=None`` (matching what Lumilake's client expects). The listing
        is scoped to the partition bucket implied by ``prefix``.
        """
        resp = self._client.list_objects_v2(
            Bucket=self.bucket_for_key(prefix),
            Prefix=prefix,
            Delimiter=delimiter,
            MaxKeys=limit,
        )
        objects: list[dict[str, Any]] = []
        for item in resp.get("Contents") or []:
            key = item.get("Key")
            if not key:
                continue
            objects.append({"key": key, "size": item.get("Size")})
        for item in resp.get("CommonPrefixes") or []:
            key = item.get("Prefix")
            if key:
                objects.append({"key": key, "size": None})
        truncated = bool(resp.get("IsTruncated"))
        return objects, truncated


def _is_not_found(exc: ClientError) -> bool:
    code = exc.response.get("Error", {}).get("Code", "")
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"NoSuchKey", "404", "NotFound"} or status == 404
