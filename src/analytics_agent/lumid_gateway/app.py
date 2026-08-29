"""FastAPI application implementing the Phase-1 lumid-data-compatible surface."""

from __future__ import annotations

import asyncio
import csv
import io
import json
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import GatewayConfig, load_config
from .db import DB, InvalidSQL
from .storage import BlobMissing, QuotaExceeded, Storage

_CONTENT_TYPES = {"jsonl": "application/x-ndjson", "csv": "text/csv"}


def _json_default(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


class RetrieveRequest(BaseModel):
    sql: str
    output_format: str = Field(default="jsonl", pattern="^(jsonl|csv)$")


class RetrieveResponse(BaseModel):
    materialized_uri: str
    output_format: str
    rowcount: int
    size_bytes: int
    access_chain: list[dict[str, Any]]
    run_id: str


def create_app(
    cfg: GatewayConfig | None = None,
    storage: Storage | None = None,
    db: DB | None = None,
) -> FastAPI:
    cfg = cfg or load_config()
    storage = storage or Storage(cfg)
    try:
        db = db or DB(cfg)
    except RuntimeError:
        # Allow construction without a database URL (health checks, blob-only
        # usage); /retrieve will surface a clear error instead.
        db = None

    app = FastAPI(title="lumid-gateway", version="0.1.0")

    async def require_auth(request: Request) -> None:
        if cfg.token is None:
            return
        expected = f"Bearer {cfg.token}"
        if request.headers.get("Authorization") != expected:
            raise HTTPException(status_code=403, detail="invalid bearer token")

    def _materialized_key(run_id: str, fmt: str) -> str:
        return f"{cfg.materialized_prefix}/{run_id}.{fmt}"

    def _serialize(
        rows: Iterator[dict[str, Any]], fmt: str, cap: int
    ) -> tuple[bytes, int, int]:
        """Serialize rows to ``(data, rowcount, size_bytes)``."""
        buf = io.StringIO()
        rowcount = 0
        writer: csv.DictWriter | None = None
        fieldnames: list[str] | None = None
        for row in rows:
            if fmt == "jsonl":
                buf.write(json.dumps(row, default=_json_default))
                buf.write("\n")
            else:
                keys = list(row.keys())
                if fieldnames is None:
                    fieldnames = keys
                    writer = csv.DictWriter(buf, fieldnames=fieldnames)
                    writer.writeheader()
                assert writer is not None
                writer.writerow(row)
            rowcount += 1
            if buf.tell() > cap:
                raise QuotaExceeded(
                    f"query result exceeds {cap} bytes; narrow the query"
                )
        data = buf.getvalue().encode("utf-8")
        return data, rowcount, len(data)

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        return {"status": "ok"}

    async def _probe(check: Any) -> dict[str, str]:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(check, cfg.health_timeout_seconds),
                timeout=cfg.health_timeout_seconds,
            )
        except TimeoutError:
            return {"status": "error", "reason": "timeout"}
        except Exception:
            # Database/S3 exceptions can contain endpoints, usernames, signed
            # URLs, or credential metadata. Keep the public response generic.
            return {"status": "error", "reason": "unavailable"}
        return {"status": "ok"}

    @app.get("/healthz")
    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        if db is None:
            database_check: dict[str, str] = {
                "status": "error",
                "reason": "unconfigured",
            }
            s3_check = await _probe(storage.check_ready)
        else:
            database_check, s3_check = await asyncio.gather(
                _probe(db.check_ready),
                _probe(storage.check_ready),
            )
        checks = {"database": database_check, "s3": s3_check}
        ready = all(check["status"] == "ok" for check in checks.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "ok": ready,
                "status": "ok" if ready else "not_ready",
                "checks": checks,
            },
        )

    @app.post("/retrieve", dependencies=[Depends(require_auth)])
    async def retrieve(body: RetrieveRequest) -> RetrieveResponse:
        if db is None:
            raise HTTPException(
                status_code=502,
                detail="LUMID_GATEWAY_DATABASE_URL is not configured",
            )
        fmt = body.output_format
        run_id = uuid4().hex
        try:
            with db.select(body.sql) as cur:
                data, rowcount, size_bytes = _serialize(
                    iter(cur), fmt, cfg.max_result_bytes
                )
        except InvalidSQL as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except QuotaExceeded as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except Exception as exc:  # DB-level errors surface as 502
            raise HTTPException(
                status_code=502, detail=f"query failed: {exc}"
            ) from exc

        key = _materialized_key(run_id, fmt)
        try:
            storage.put_blob(
                key, data, _CONTENT_TYPES[fmt], cfg.max_blob_bytes
            )
        except QuotaExceeded as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"materialize failed: {exc}"
            ) from exc

        return RetrieveResponse(
            materialized_uri=f"/{key}",
            output_format=fmt,
            rowcount=rowcount,
            size_bytes=size_bytes,
            access_chain=[{"type": "postgres", "query": body.sql}],
            run_id=run_id,
        )

    @app.get("/materialized/{key:path}", dependencies=[Depends(require_auth)])
    async def get_materialized(key: str) -> Response:
        try:
            body, ct = storage.get_blob(f"{cfg.materialized_prefix}/{key}")
        except BlobMissing as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(content=body, media_type=ct)

    @app.get("/blobs", dependencies=[Depends(require_auth)])
    async def list_blobs(
        prefix: str = Query(default=""),
        delimiter: str = Query(default=""),
        limit: int = Query(default=10000, ge=1, le=10000),
    ) -> dict[str, Any]:
        objects, truncated = storage.list_blobs(prefix, delimiter, limit)
        return {"objects": objects, "truncated": truncated}

    @app.put("/blobs/{key:path}", dependencies=[Depends(require_auth)])
    async def put_blob(key: str, request: Request) -> dict[str, Any]:
        body = await request.body()
        if len(body) > cfg.max_blob_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"blob {key!r} exceeds quota of {cfg.max_blob_bytes} bytes",
            )
        content_type = request.headers.get("Content-Type", "application/octet-stream")
        try:
            storage.put_blob(key, body, content_type, cfg.max_blob_bytes)
        except QuotaExceeded as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"blob upload failed: {exc}"
            ) from exc
        return {"key": key, "size": len(body)}

    @app.get("/blobs/{key:path}", dependencies=[Depends(require_auth)])
    async def get_blob(key: str) -> Response:
        try:
            body, ct = storage.get_blob(key)
        except BlobMissing as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(content=body, media_type=ct)

    @app.get(
        "/catalog/tables/{schema}/{table}",
        dependencies=[Depends(require_auth)],
    )
    async def catalog_table(schema: str, table: str) -> dict[str, Any]:
        if db is None:
            raise HTTPException(
                status_code=502,
                detail="LUMID_GATEWAY_DATABASE_URL is not configured",
            )
        try:
            columns = db.catalog_columns(schema, table)
        except InvalidSQL as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"catalog lookup failed: {exc}"
            ) from exc
        if not columns:
            raise HTTPException(
                status_code=404, detail=f"table {schema}.{table} not found"
            )
        return {"columns": [{"name": name} for name in columns]}

    return app
