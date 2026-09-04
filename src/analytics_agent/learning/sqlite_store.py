"""Append-only SQLite memory backend with deterministic local retrieval."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from pathlib import Path
from typing import Final

from .models import LearningEpisode, MemoryQuery, RetrievedEpisode

STORE_SCHEMA_VERSION: Final = "2"
_TOKEN_RE: Final = re.compile(r"[^\W_]+", flags=re.UNICODE)


class MemoryStoreError(RuntimeError):
    """Base class for SQLite memory-store errors."""


class DuplicateEpisodeError(MemoryStoreError):
    """Raised when an existing episode ID is appended again."""


class CorrectionReferenceError(MemoryStoreError):
    """Raised when a correction does not reference a compatible prior event."""


def tokenize(text: str) -> tuple[str, ...]:
    """Tokenize text deterministically without optional search dependencies."""

    return tuple(_TOKEN_RE.findall(text.casefold()))


class SQLiteMemoryStore:
    """A small append-only backend for self-contained Phase 2 experiments.

    Retrieval applies tenant, privacy-scope, and catalog-version equality in
    SQL before scoring.  It additionally hard-codes ``split='train'`` and
    ``retrieval_eligible=1`` so a caller cannot request dev/test examples.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._connection = sqlite3.connect(self.path)
        if self.path != ":memory:":
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._closed = False
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        metadata_table_exists = self._connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'memory_store_metadata'
            """
        ).fetchone()
        if metadata_table_exists is not None:
            existing = self._connection.execute(
                """
                SELECT value FROM memory_store_metadata
                WHERE key = 'store_schema_version'
                """
            ).fetchone()
            if existing is None or existing["value"] != STORE_SCHEMA_VERSION:
                found = None if existing is None else existing["value"]
                raise MemoryStoreError(
                    "unsupported SQLite memory-store schema version: "
                    f"expected {STORE_SCHEMA_VERSION}, found {found!r}"
                )

        schema = """
        CREATE TABLE IF NOT EXISTS memory_store_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS learning_episodes (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_id TEXT NOT NULL UNIQUE,
            schema_version TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            privacy_scope TEXT NOT NULL,
            catalog_version TEXT NOT NULL,
            split TEXT NOT NULL CHECK (split IN ('train', 'dev', 'test')),
            disposition TEXT NOT NULL
                CHECK (disposition IN ('generate', 'clarify', 'reject')),
            retrieval_eligible INTEGER NOT NULL
                CHECK (retrieval_eligible IN (0, 1)),
            request_text TEXT NOT NULL,
            retrieval_text TEXT NOT NULL,
            corrects_episode_id TEXT,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            CHECK (split = 'train' OR retrieval_eligible = 0),
            FOREIGN KEY (corrects_episode_id)
                REFERENCES learning_episodes(episode_id)
        );

        CREATE INDEX IF NOT EXISTS idx_learning_retrieval_scope
        ON learning_episodes (
            tenant_id,
            privacy_scope,
            catalog_version,
            disposition,
            split,
            retrieval_eligible
        );

        CREATE TRIGGER IF NOT EXISTS learning_episodes_no_update
        BEFORE UPDATE ON learning_episodes
        BEGIN
            SELECT RAISE(ABORT, 'learning_episodes is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS learning_episodes_no_replace
        BEFORE INSERT ON learning_episodes
        WHEN EXISTS (
            SELECT 1 FROM learning_episodes
            WHERE episode_id = NEW.episode_id
               OR (NEW.sequence >= 0 AND sequence = NEW.sequence)
        )
        BEGIN
            SELECT RAISE(ABORT, 'learning_episodes is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS learning_episodes_no_delete
        BEFORE DELETE ON learning_episodes
        BEGIN
            SELECT RAISE(ABORT, 'learning_episodes is append-only');
        END;
        """
        with self._connection:
            self._connection.executescript(schema)
            self._connection.execute(
                """
                INSERT OR IGNORE INTO memory_store_metadata(key, value)
                VALUES ('store_schema_version', ?)
                """,
                (STORE_SCHEMA_VERSION,),
            )
        row = self._connection.execute(
            "SELECT value FROM memory_store_metadata WHERE key = 'store_schema_version'"
        ).fetchone()
        if row is None or row["value"] != STORE_SCHEMA_VERSION:
            raise MemoryStoreError("unsupported SQLite memory-store schema version")

    def _ensure_open(self) -> None:
        if self._closed:
            raise MemoryStoreError("memory store is closed")

    def append_episode(self, episode: LearningEpisode) -> None:
        """Append ``episode`` atomically without replacing prior history."""

        self._ensure_open()
        payload = json.dumps(
            episode.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

        try:
            with self._connection:
                if episode.corrects_episode_id is not None:
                    previous = self._connection.execute(
                        """
                        SELECT tenant_id, privacy_scope, catalog_version, split
                        FROM learning_episodes
                        WHERE episode_id = ?
                        """,
                        (episode.corrects_episode_id,),
                    ).fetchone()
                    if previous is None:
                        raise CorrectionReferenceError(
                            "correction must reference an existing prior episode"
                        )
                    expected_scope = (
                        episode.tenant_id,
                        episode.privacy_scope,
                        episode.catalog_version,
                    )
                    actual_scope = (
                        previous["tenant_id"],
                        previous["privacy_scope"],
                        previous["catalog_version"],
                    )
                    if actual_scope != expected_scope:
                        raise CorrectionReferenceError(
                            "correction and prior episode must share tenant, "
                            "privacy scope, and catalog version"
                        )
                    if previous["split"] != episode.split:
                        raise CorrectionReferenceError(
                            "correction and prior episode must share the same split"
                        )
                    if episode.retrieval_eligible and previous["split"] != "train":
                        raise CorrectionReferenceError(
                            "a retrieval-eligible correction must reference train data"
                        )

                self._connection.execute(
                    """
                    INSERT INTO learning_episodes (
                        episode_id,
                        schema_version,
                        tenant_id,
                        privacy_scope,
                        catalog_version,
                        split,
                        disposition,
                        retrieval_eligible,
                        request_text,
                        retrieval_text,
                        corrects_episode_id,
                        created_at,
                        payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        episode.episode_id,
                        episode.schema_version,
                        episode.tenant_id,
                        episode.privacy_scope,
                        episode.catalog_version,
                        episode.split,
                        episode.disposition,
                        int(episode.retrieval_eligible),
                        episode.request_text,
                        episode.retrieval_text,
                        episode.corrects_episode_id,
                        episode.created_at,
                        payload,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            duplicate = self._connection.execute(
                "SELECT 1 FROM learning_episodes WHERE episode_id = ?",
                (episode.episode_id,),
            ).fetchone()
            if duplicate is not None:
                raise DuplicateEpisodeError(
                    f"episode_id already exists: {episode.episode_id}"
                ) from exc
            raise MemoryStoreError("episode violates the SQLite memory contract") from exc

    def get_episode(
        self,
        episode_id: str,
        *,
        tenant_id: str,
        privacy_scope: str,
        catalog_version: str,
    ) -> LearningEpisode | None:
        """Return one record only when all isolation labels match exactly."""

        self._ensure_open()
        row = self._connection.execute(
            """
            SELECT payload_json
            FROM learning_episodes
            WHERE episode_id = ?
              AND tenant_id = ?
              AND privacy_scope = ?
              AND catalog_version = ?
            """,
            (
                episode_id,
                tenant_id,
                privacy_scope,
                catalog_version,
            ),
        ).fetchone()
        if row is None:
            return None
        return LearningEpisode.from_dict(json.loads(row["payload_json"]))

    def retrieve(self, query: MemoryQuery) -> list[RetrievedEpisode]:
        """Retrieve with exact scope filters and deterministic token overlap."""

        self._ensure_open()
        query_tokens = frozenset(tokenize(query.query_text))
        if not query_tokens:
            return []

        rows = self._connection.execute(
            """
            SELECT episode_id, retrieval_text, payload_json
            FROM learning_episodes
            WHERE tenant_id = ?
              AND privacy_scope = ?
              AND catalog_version = ?
              AND disposition = ?
              AND split = 'train'
              AND retrieval_eligible = 1
            """,
            (
                query.tenant_id,
                query.privacy_scope,
                query.catalog_version,
                query.disposition,
            ),
        ).fetchall()

        ranked: list[RetrievedEpisode] = []
        for row in rows:
            episode_tokens = frozenset(tokenize(row["retrieval_text"]))
            matched = query_tokens & episode_tokens
            if not matched:
                continue
            union = query_tokens | episode_tokens
            score = len(matched) / len(union)
            ranked.append(
                RetrievedEpisode(
                    episode=LearningEpisode.from_dict(json.loads(row["payload_json"])),
                    score=score,
                    matched_tokens=tuple(sorted(matched)),
                )
            )

        ranked.sort(
            key=lambda item: (
                -item.score,
                -len(item.matched_tokens),
                item.episode.episode_id,
            )
        )
        return ranked[: query.limit]

    def count_episodes(
        self,
        *,
        tenant_id: str,
        privacy_scope: str,
        catalog_version: str,
    ) -> int:
        """Count stored history within one exact isolation scope."""

        self._ensure_open()
        row = self._connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM learning_episodes
            WHERE tenant_id = ?
              AND privacy_scope = ?
              AND catalog_version = ?
            """,
            (tenant_id, privacy_scope, catalog_version),
        ).fetchone()
        return int(row["count"])

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def __enter__(self) -> "SQLiteMemoryStore":
        self._ensure_open()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()
