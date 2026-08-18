"""Tests for the lumid-gateway FastAPI surface (Phase-1 slice)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from analytics_agent.lumid_gateway.app import create_app
from analytics_agent.lumid_gateway.config import GatewayConfig
from analytics_agent.lumid_gateway.db import InvalidSQL, validate_read_only


class FakeDB:
    """In-memory stand-in for the gateway DB class."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows if rows is not None else []
        self.columns: list[str] | None = None
        self.fail_with: Exception | None = None

    @contextmanager
    def select(self, sql: str, params: dict | None = None):
        if self.fail_with is not None:
            raise self.fail_with
        validate_read_only(sql)  # production validation lives in db.select too
        yield iter(self.rows)

    def catalog_columns(self, schema: str, table: str) -> list[str]:
        return self.columns if self.columns is not None else []


class FakeStorage:
    """In-memory stand-in for the gateway storage class."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put_blob(self, key: str, body: bytes, content_type: str, max_bytes: int) -> None:
        from analytics_agent.lumid_gateway.storage import QuotaExceeded

        if len(body) > max_bytes:
            raise QuotaExceeded(f"blob {key!r} exceeds quota")
        self.objects[key] = (body, content_type)

    def get_blob(self, key: str) -> tuple[bytes, str]:
        from analytics_agent.lumid_gateway.storage import BlobMissing

        if key not in self.objects:
            raise BlobMissing(key)
        return self.objects[key]

    def list_blobs(self, prefix: str, delimiter: str, limit: int) -> tuple[list[dict], bool]:
        keys = sorted(k for k in self.objects if k.startswith(prefix))
        objects: list[dict[str, Any]] = []
        seen_prefixes: set[str] = set()
        for key in keys:
            rest = key[len(prefix):]
            if delimiter:
                head, sep, _tail = rest.partition(delimiter)
                if sep:
                    folder = prefix + head + delimiter
                    if folder not in seen_prefixes:
                        seen_prefixes.add(folder)
                        objects.append({"key": folder, "size": None})
                    continue
            objects.append({"key": key, "size": len(self.objects[key][0])})
        truncated = len(objects) > limit
        return objects[:limit], truncated


def make_client(
    db: FakeDB | None = None,
    storage: FakeStorage | None = None,
    token: str | None = None,
    max_blob_bytes: int = 1024**3,
) -> TestClient:
    cfg = GatewayConfig(
        database_url="postgresql://fake/fake",
        s3_bucket="test-bucket",
        token=token,
        max_blob_bytes=max_blob_bytes,
    )
    app = create_app(cfg=cfg, storage=storage or FakeStorage(), db=db or FakeDB())
    return TestClient(app)


def test_healthz() -> None:
    with make_client() as client:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


def test_retrieve_jsonl() -> None:
    db = FakeDB([{"a": 1, "b": "x"}, {"a": 2, "b": "y"}])
    storage = FakeStorage()
    with make_client(db=db, storage=storage) as client:
        resp = client.post("/retrieve", json={"sql": "SELECT 1"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["output_format"] == "jsonl"
        assert body["rowcount"] == 2
        assert body["materialized_uri"].startswith("/materialized/")
        assert body["access_chain"] == [{"type": "postgres", "query": "SELECT 1"}]
        # materialized object exists in storage and matches the URI
        key = body["materialized_uri"].lstrip("/")
        stored, ct = storage.objects[key]
        assert ct == "application/x-ndjson"
        assert body["size_bytes"] == len(stored)
        lines = [json.loads(line) for line in stored.decode().splitlines()]
        assert lines == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
        # and it is fetchable through the gateway
        got = client.get(body["materialized_uri"])
        assert got.status_code == 200
        assert got.content == stored


def test_retrieve_csv() -> None:
    db = FakeDB([{"a": 1}, {"a": 2}])
    storage = FakeStorage()
    with make_client(db=db, storage=storage) as client:
        resp = client.post(
            "/retrieve", json={"sql": "SELECT 1", "output_format": "csv"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["output_format"] == "csv"
        stored, ct = storage.objects[body["materialized_uri"].lstrip("/")]
        assert ct == "text/csv"
        assert stored.decode().splitlines() == ["a", "1", "2"]


def test_retrieve_rejects_non_read_only() -> None:
    with make_client() as client:
        resp = client.post("/retrieve", json={"sql": "DELETE FROM t"})
        assert resp.status_code == 400
        resp = client.post("/retrieve", json={"sql": "UPDATE t SET a=1"})
        assert resp.status_code == 400


def test_retrieve_db_error_is_502() -> None:
    db = FakeDB()
    db.fail_with = RuntimeError("connection refused")
    with make_client(db=db) as client:
        resp = client.post("/retrieve", json={"sql": "SELECT 1"})
        assert resp.status_code == 502
        assert "query failed" in resp.json()["detail"]


def test_blob_put_get_404() -> None:
    with make_client() as client:
        put = client.put(
            "/blobs/data/file.txt",
            content=b"hello world",
            headers={"Content-Type": "text/plain"},
        )
        assert put.status_code == 200
        assert put.json() == {"key": "data/file.txt", "size": 11}

        got = client.get("/blobs/data/file.txt")
        assert got.status_code == 200
        assert got.content == b"hello world"
        assert got.headers["content-type"].startswith("text/plain")

        missing = client.get("/blobs/nope.txt")
        assert missing.status_code == 404


def test_blob_list_passthrough() -> None:
    storage = FakeStorage()
    storage.objects["a/1.txt"] = (b"1", "text/plain")
    storage.objects["a/2.txt"] = (b"22", "text/plain")
    storage.objects["b/3.txt"] = (b"333", "text/plain")
    with make_client(storage=storage) as client:
        resp = client.get("/blobs", params={"prefix": "a/", "limit": "10000"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["truncated"] is False
        assert sorted(o["key"] for o in body["objects"]) == ["a/1.txt", "a/2.txt"]
        assert {o["key"]: o["size"] for o in body["objects"]} == {
            "a/1.txt": 1,
            "a/2.txt": 2,
        }
        # delimited listing folds folders into objects with size=None
        resp = client.get("/blobs", params={"prefix": "a/", "delimiter": "/"})
        assert resp.status_code == 200
        assert resp.json()["objects"] == [
            {"key": "a/1.txt", "size": 1},
            {"key": "a/2.txt", "size": 2},
        ]


def test_blob_quota_413() -> None:
    with make_client(max_blob_bytes=4) as client:
        resp = client.put("/blobs/big.bin", content=b"12345")
        assert resp.status_code == 413


def test_auth_required_when_token_set() -> None:
    with make_client(token="sekret") as client:
        assert client.get("/blobs").status_code == 403
        assert (
            client.get("/blobs", headers={"Authorization": "Bearer wrong"}).status_code
            == 403
        )
        assert (
            client.get("/blobs", headers={"Authorization": "Bearer sekret"}).status_code
            == 200
        )


def test_no_auth_in_local_mode() -> None:
    with make_client(token=None) as client:
        assert client.get("/blobs").status_code == 200


def test_catalog_columns() -> None:
    db = FakeDB()
    db.columns = ["id", "name"]
    with make_client(db=db) as client:
        resp = client.get("/catalog/tables/public/users")
        assert resp.status_code == 200
        assert resp.json() == {"columns": [{"name": "id"}, {"name": "name"}]}
        db.columns = []
        resp = client.get("/catalog/tables/public/users")
        assert resp.status_code == 404


def test_validate_read_only_unit() -> None:
    assert validate_read_only("SELECT 1") is None
    assert validate_read_only("  with x as (select 1) select * from x") is None
    assert validate_read_only("-- comment\nSELECT 1") is None
    with pytest.raises(InvalidSQL):
        validate_read_only("INSERT INTO t VALUES (1)")
    with pytest.raises(InvalidSQL):
        validate_read_only("DROP TABLE t")
    with pytest.raises(InvalidSQL):
        validate_read_only("")


def test_storage_partition_routing() -> None:
    from analytics_agent.lumid_gateway.config import GatewayConfig
    from analytics_agent.lumid_gateway.storage import Storage

    cfg = GatewayConfig(
        database_url="postgresql://fake/fake",
        s3_bucket="lumilake-private",
        public_bucket="lumilake-public",
    )
    storage = Storage(cfg)
    assert storage.bucket_for_key("lumilake-public/report.txt") == "lumilake-public"
    assert storage.bucket_for_key("lumilake-archive/artifacts/jobs_index.json") == "lumilake-private"
    assert storage.bucket_for_key("lumilake-demo/data.csv") == "lumilake-private"
    assert storage.bucket_for_key("materialized/abc.jsonl") == "lumilake-private"
