"""Offline object receipts for the pinned demo; never uploads or exposes a bucket."""
from __future__ import annotations

import hashlib
import itertools
import posixpath
import re
import struct
import tarfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Iterable

ARCHIVE_SHA256 = "0add591f714587f35c85bfddc81b25f60a464167d9443fdc288b5c4201243e36"
ARCHIVE_BYTES = 681_293_896
KEY_PREFIX = f"intake/zhengyuan-demo/{ARCHIVE_SHA256[:12]}/demo/unstructured/news-images"
PRIVATE_BUCKET = "lumilake-private"


def object_key(news_id: str) -> str:
    """Archive IDs are opaque hexadecimal strings, not necessarily 32 characters."""
    if not isinstance(news_id, str) or not re.fullmatch(r"[0-9a-f]{1,64}", news_id):
        raise ValueError("invalid news identifier")
    return f"{KEY_PREFIX}/{news_id}.png"


def png_structure(body: bytes) -> dict[str, int]:
    """Check bounded encoded PNG chunks; this is not a pixel/model decode claim."""
    if len(body) > 6_000_000 or body[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("invalid PNG signature/size")
    offset, count = 8, 0
    dimensions = None
    has_data = False
    while offset < len(body):
        if offset + 12 > len(body):
            raise ValueError("truncated PNG chunk")
        size = struct.unpack_from(">I", body, offset)[0]
        end = offset + 12 + size
        if end > len(body):
            raise ValueError("truncated PNG payload")
        kind = body[offset + 4:offset + 8]
        payload = body[offset + 8:offset + 8 + size]
        crc = struct.unpack_from(">I", body, offset + 8 + size)[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != crc:
            raise ValueError("PNG CRC mismatch")
        if count == 0 and kind != b"IHDR":
            raise ValueError("PNG must start with IHDR")
        if kind == b"IHDR":
            if dimensions is not None or size != 13:
                raise ValueError("invalid PNG IHDR")
            width, height = struct.unpack_from(">II", payload)
            if not width or not height or width * height > 50_000_000:
                raise ValueError("PNG pixel-size cap")
            dimensions = {"width": width, "height": height}
        if kind == b"IDAT":
            has_data = True
        if kind == b"IEND":
            if size or end != len(body) or not has_data or dimensions is None:
                raise ValueError("invalid PNG termination")
            return dimensions
        offset, count = end, count + 1
    raise ValueError("missing PNG IEND")


def inspect_selected_objects(archive_path: Path, news_ids: Iterable[str]) -> dict:
    """Bind real SELECT-selected IDs to archive bytes and an intended private key.

    Receipts deliberately distinguish encoded-file checks and intended MIME from
    a live S3 GET/Content-Type check. No object-storage client is instantiated.
    """
    if isinstance(news_ids, (str, bytes)):
        raise ValueError("news IDs must be a collection")
    try:
        ids = list(itertools.islice(news_ids, 101))
    except TypeError as exc:
        raise ValueError("news IDs must be iterable") from exc
    for news_id in ids:
        object_key(news_id)
    if not ids or len(ids) > 100 or len(set(ids)) != len(ids):
        raise ValueError("require 1..100 distinct news IDs")
    archive_path = Path(archive_path)
    if archive_path.is_symlink() or not archive_path.is_file():
        raise ValueError("archive must be a regular non-symlink file")
    if archive_path.stat().st_size != ARCHIVE_BYTES:
        raise ValueError("archive size mismatch")
    digest = hashlib.sha256()
    with archive_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024**2), b""):
            digest.update(chunk)
    if digest.hexdigest() != ARCHIVE_SHA256:
        raise ValueError("archive fingerprint mismatch")
    wanted = {f"s3/demo/unstructured/news-images/{news_id}.png": news_id for news_id in ids}
    receipts, seen, total_size = {}, set(), 0
    with tarfile.open(archive_path, "r|gz") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            name = posixpath.normpath(member.name)
            total_size += member.size
            if (path.is_absolute() or ".." in path.parts or "\\" in member.name
                    or any(ord(c) < 32 for c in member.name) or name in seen
                    or not (member.isfile() or member.isdir())
                    or member.size < 0 or member.size > 4 * 1024**3
                    or total_size > 16 * 1024**3 or len(seen) >= 25_000):
                raise ValueError("unsafe archive member")
            seen.add(name)
            if name not in wanted:
                continue
            if not member.isfile() or member.size > 6_000_000:
                raise ValueError("unsafe selected object")
            stream = archive.extractfile(member)
            assert stream is not None
            body = stream.read(6_000_001)
            if len(body) != member.size:
                raise ValueError("object length mismatch")
            news_id = wanted[name]
            receipts[news_id] = {
                "news_id": news_id, "archive_member": name,
                "bucket_intent": PRIVATE_BUCKET, "key": object_key(news_id),
                "content_type_intent": "image/png", "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                **png_structure(body),
            }
    if set(receipts) != set(ids):
        raise ValueError("selected news image missing from archive")
    return {
        "archive_sha256": ARCHIVE_SHA256,
        "status": "offline_encoded_objects_verified", "selected_count": len(ids),
        "objects": [receipts[news_id] for news_id in ids],
        "pixel_decode_this_run": "NOT_RUN", "live_s3_get": "NOT_RUN",
        "live_s3_content_type": "NOT_RUN", "uploads": 0,
    }
