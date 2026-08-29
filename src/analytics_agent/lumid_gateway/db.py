"""PostgreSQL access for the gateway (read-only, dict rows)."""

from __future__ import annotations

import re
from math import ceil
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .config import GatewayConfig

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LEADING_COMMENT_RE = re.compile(r"^\s*(?:--[^\n]*\n|/\*.*?\*/|\s)*", re.S)
_READ_ONLY_LEADERS = ("SELECT", "WITH", "EXPLAIN", "TABLE", "SHOW", "VALUES")


class InvalidSQL(ValueError):
    """Statement is not a read-only query."""


def validate_identifier(name: str, kind: str) -> None:
    if not _IDENTIFIER_RE.fullmatch(name):
        raise InvalidSQL(f"invalid {kind} identifier: {name!r}")


def validate_read_only(sql: str) -> None:
    """Reject anything that is not a read-only statement.

    psycopg's extended protocol executes a single statement per call, so
    multi-statement payloads cannot smuggle DML past this check. The
    connection is additionally opened with ``default_transaction_read_only``
    as a second line of defence.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise InvalidSQL("empty SQL statement")
    stripped = _LEADING_COMMENT_RE.sub("", sql)
    first_word = stripped.lstrip().split(None, 1)[0].upper().rstrip(";")
    if first_word not in _READ_ONLY_LEADERS:
        raise InvalidSQL(
            f"only read-only statements allowed, got: {first_word or '?'}"
        )


class DB:
    def __init__(self, cfg: GatewayConfig) -> None:
        if not cfg.database_url:
            raise RuntimeError("LUMID_GATEWAY_DATABASE_URL is not configured")
        self._url = cfg.database_url

    def check_ready(self, timeout_seconds: float) -> None:
        """Verify that PostgreSQL accepts a small read-only query."""
        statement_timeout_ms = max(1, int(timeout_seconds * 1000))
        conninfo = psycopg.conninfo.make_conninfo(
            self._url,
            connect_timeout=max(1, ceil(timeout_seconds)),
            options=(
                "-c default_transaction_read_only=on "
                f"-c statement_timeout={statement_timeout_ms}"
            ),
        )
        with psycopg.connect(conninfo, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS ready")
                row = cur.fetchone()
                if row is None or row.get("ready") != 1:
                    raise RuntimeError("database readiness query returned no result")

    @contextmanager
    def select(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> Iterator[psycopg.Cursor[dict[str, Any]]]:
        validate_read_only(sql)
        conninfo = psycopg.conninfo.make_conninfo(
            self._url, options="-c default_transaction_read_only=on"
        )
        with psycopg.connect(conninfo, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                yield cur

    def catalog_columns(self, schema: str, table: str) -> list[str]:
        validate_identifier(schema, "schema")
        validate_identifier(table, "table")
        with self.select(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %(schema)s AND table_name = %(table)s "
            "ORDER BY ordinal_position",
            {"schema": schema, "table": table},
        ) as cur:
            return [row["column_name"] for row in cur]
