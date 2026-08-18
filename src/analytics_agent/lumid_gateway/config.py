"""Gateway configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class GatewayConfig:
    database_url: str | None = field(
        default_factory=lambda: os.environ.get("LUMID_GATEWAY_DATABASE_URL")
    )
    s3_endpoint_url: str = field(
        default_factory=lambda: os.environ.get("LUMID_GATEWAY_S3_ENDPOINT_URL", "")
    )
    s3_access_key: str = field(
        default_factory=lambda: os.environ.get("LUMID_GATEWAY_S3_ACCESS_KEY", "")
    )
    s3_secret_key: str = field(
        default_factory=lambda: os.environ.get("LUMID_GATEWAY_S3_SECRET_KEY", "")
    )
    s3_bucket: str = field(
        default_factory=lambda: os.environ.get(
            "LUMID_GATEWAY_S3_BUCKET", "lumilake-private"
        )
    )
    #: Public-read-only partition bucket (keys under <public_bucket>/* route
    #: here; anonymous downloads allowed via bucket policy).
    public_bucket: str = field(
        default_factory=lambda: os.environ.get(
            "LUMID_GATEWAY_S3_PUBLIC_BUCKET", "lumilake-public"
        )
    )
    s3_region: str = field(
        default_factory=lambda: os.environ.get("LUMID_GATEWAY_S3_REGION", "us-east-1")
    )
    token: str | None = field(
        default_factory=lambda: os.environ.get("LUMID_GATEWAY_TOKEN") or None
    )
    max_blob_bytes: int = field(
        default_factory=lambda: _int_env("LUMID_GATEWAY_MAX_BLOB_BYTES", 1024**3)
    )
    max_result_bytes: int = field(
        default_factory=lambda: _int_env("LUMID_GATEWAY_MAX_RESULT_BYTES", 512 * 1024**2)
    )
    #: Prefix for materialized query results inside the bucket.
    materialized_prefix: str = "materialized"


def load_config() -> GatewayConfig:
    return GatewayConfig()
