import io
import hashlib
import struct
import tarfile
import zlib

import pytest

from analytics_agent import demo_intake_objects as objects
from analytics_agent.demo_intake_objects import object_key, png_structure
from analytics_agent.lumid_gateway.storage import Storage


def png(width=1, height=1):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b"")


def test_png_structure_and_resource_bound():
    assert png_structure(png()) == {"width": 1, "height": 1}
    with pytest.raises(ValueError):
        png_structure(png(1200, 50000))
    with pytest.raises(ValueError):
        png_structure(png()[:-1])
    with pytest.raises(ValueError):
        png_structure(png() + b"trailer")
    corrupt = bytearray(png())
    corrupt[29] ^= 1
    with pytest.raises(ValueError):
        png_structure(bytes(corrupt))


@pytest.mark.parametrize("identifier", ["../x", "a/b", "", "x", None, 7, "a" * 65])
def test_unsafe_identifiers_rejected(identifier):
    with pytest.raises(ValueError):
        object_key(identifier)


def test_actual_gateway_routing_and_mime_contract_with_stub_client():
    """Unit contract only: not evidence of an S3 server or uploaded metadata."""
    storage = Storage.__new__(Storage)
    storage._private_bucket = "lumilake-private"
    storage._public_bucket = "lumilake-public"
    key = object_key("91c0fc6bad6f4330b6bf33748dd2cccc")
    body = png()
    calls = []

    class StubClient:
        def get_object(self, **kwargs):
            calls.append(kwargs)
            return {"Body": io.BytesIO(body), "ContentType": "image/png"}

    storage._client = StubClient()
    assert storage.bucket_for_key(key) == "lumilake-private"
    assert storage.get_blob(key) == (body, "image/png")
    assert calls == [{"Bucket": "lumilake-private", "Key": key}]


def test_identifier_iterable_is_bounded_before_archive_io(tmp_path):
    consumed = 0
    def ids():
        nonlocal consumed
        while True:
            consumed += 1
            if consumed > 101:
                raise AssertionError("overconsumed iterator")
            yield format(consumed, "x")
    with pytest.raises(ValueError):
        objects.inspect_selected_objects(tmp_path / "absent", ids())
    assert consumed == 101


def pinned_test_archive(tmp_path, monkeypatch, names):
    path = tmp_path / "synthetic.tar.gz"
    body = png()
    with tarfile.open(path, "w:gz") as archive:
        for name in names:
            member = tarfile.TarInfo(name)
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    # Test-only authority injection: production exposes no custom fingerprint argument.
    monkeypatch.setattr(objects, "ARCHIVE_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
    monkeypatch.setattr(objects, "ARCHIVE_BYTES", path.stat().st_size)
    return path


def test_selected_object_receipt(tmp_path, monkeypatch):
    path = pinned_test_archive(tmp_path, monkeypatch, ["s3/demo/unstructured/news-images/abc.png"])
    receipt = objects.inspect_selected_objects(path, ["abc"])
    assert receipt["selected_count"] == 1
    assert receipt["objects"][0]["sha256"] == hashlib.sha256(png()).hexdigest()
    assert receipt["objects"][0]["key"] == object_key("abc")
    assert receipt["live_s3_get"] == receipt["live_s3_content_type"] == "NOT_RUN"


def test_wrong_hash_rejected(tmp_path, monkeypatch):
    path = pinned_test_archive(tmp_path, monkeypatch, [])
    monkeypatch.setattr(objects, "ARCHIVE_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="fingerprint"):
        objects.inspect_selected_objects(path, ["abc"])


@pytest.mark.parametrize("names", [[], ["../escape.png"],
    ["s3/demo/unstructured/news-images/abc.png"] * 2])
def test_incomplete_or_unsafe_members_rejected(tmp_path, monkeypatch, names):
    path = pinned_test_archive(tmp_path, monkeypatch, names)
    with pytest.raises(ValueError):
        objects.inspect_selected_objects(path, ["abc"])
