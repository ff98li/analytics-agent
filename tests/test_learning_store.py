"""Contract tests for the append-only Phase 2 learning store."""

from __future__ import annotations

import sqlite3
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from analytics_agent.learning import (
    CorrectionReferenceError,
    DuplicateEpisodeError,
    LearningEpisode,
    LearningModelValidationError,
    MemoryAdapter,
    MemoryQuery,
    MemoryStoreError,
    SQLiteMemoryStore,
    tokenize,
)


def make_episode(
    episode_id: str,
    request_text: str = "Compare AAPL and NVDA fundamentals",
    **overrides: object,
) -> LearningEpisode:
    values: dict[str, object] = {
        "episode_id": episode_id,
        "tenant_id": "tenant-a",
        "privacy_scope": "LOCAL_ONLY",
        "catalog_version": "catalog-v1",
        "split": "train",
        "disposition": "generate",
        "retrieval_eligible": True,
        "request_text": request_text,
        "family": "compare",
        "feedback_source": "gold_train",
        "created_at": "2026-09-03T00:00:00+00:00",
    }
    values.update(overrides)
    return LearningEpisode(**values)  # type: ignore[arg-type]


def make_query(query_text: str = "compare AAPL fundamentals", **overrides: object) -> MemoryQuery:
    values: dict[str, object] = {
        "query_text": query_text,
        "tenant_id": "tenant-a",
        "privacy_scope": "LOCAL_ONLY",
        "catalog_version": "catalog-v1",
        "disposition": "generate",
        "limit": 4,
    }
    values.update(overrides)
    return MemoryQuery(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("split", ["dev", "test"])
def test_dev_and_test_cannot_be_retrieval_eligible(split: str) -> None:
    with pytest.raises(
        LearningModelValidationError,
        match="dev/test episodes cannot be retrieval eligible",
    ):
        make_episode("bad", split=split, retrieval_eligible=True)


def test_models_are_versioned_and_queries_require_isolation_labels() -> None:
    episode = make_episode("ep-version")
    assert episode.schema_version == "learning-episode/v1"
    assert make_query().schema_version == "memory-query/v1"

    with pytest.raises(LearningModelValidationError, match="tenant_id"):
        make_query(tenant_id=" ")
    with pytest.raises(LearningModelValidationError, match="limit"):
        make_query(limit=0)
    with pytest.raises(LearningModelValidationError, match="unsupported"):
        replace(episode, schema_version="learning-episode/v0")


def test_store_satisfies_replaceable_memory_protocol() -> None:
    with SQLiteMemoryStore() as store:
        assert isinstance(store, MemoryAdapter)


def test_retrieval_hard_filters_split_eligibility_and_scope() -> None:
    episodes = [
        make_episode("allowed"),
        make_episode("train-ineligible", retrieval_eligible=False),
        make_episode("dev", split="dev", retrieval_eligible=False),
        make_episode("test", split="test", retrieval_eligible=False),
        make_episode("other-tenant", tenant_id="tenant-b"),
        make_episode("other-privacy", privacy_scope="PUBLIC_ONLY"),
        make_episode("other-catalog", catalog_version="catalog-v2"),
        make_episode("other-disposition", disposition="clarify"),
    ]

    with SQLiteMemoryStore() as store:
        for episode in episodes:
            store.append_episode(episode)

        results = store.retrieve(make_query())

    assert [item.episode.episode_id for item in results] == ["allowed"]


def test_scoped_get_does_not_cross_isolation_boundaries() -> None:
    episode = make_episode("private-episode", attributes={"score": 0.75})
    with SQLiteMemoryStore() as store:
        store.append_episode(episode)
        assert (
            store.get_episode(
                episode.episode_id,
                tenant_id="tenant-b",
                privacy_scope=episode.privacy_scope,
                catalog_version=episode.catalog_version,
            )
            is None
        )
        loaded = store.get_episode(
            episode.episode_id,
            tenant_id=episode.tenant_id,
            privacy_scope=episode.privacy_scope,
            catalog_version=episode.catalog_version,
        )

    assert loaded == episode
    assert loaded is not None
    assert loaded.attributes == {"score": 0.75}


def test_duplicate_append_is_rejected_and_original_is_unchanged() -> None:
    original = make_episode("same-id", "original request")
    replacement = make_episode("same-id", "silently replaced request")

    with SQLiteMemoryStore() as store:
        store.append_episode(original)
        with pytest.raises(DuplicateEpisodeError, match="already exists"):
            store.append_episode(replacement)
        loaded = store.get_episode(
            "same-id",
            tenant_id="tenant-a",
            privacy_scope="LOCAL_ONLY",
            catalog_version="catalog-v1",
        )

    assert loaded == original


def test_correction_appends_new_history_without_overwriting_original() -> None:
    original = make_episode(
        "failure",
        feedback_source="validator",
        error_types=("WRONG_BINDING",),
        feedback_summary="NVDA binding was missing",
    )
    correction = make_episode(
        "correction",
        corrects_episode_id=original.episode_id,
        corrected_job_spec_ref="jobspec:sha256:fixed",
        correction_summary="Add the NVDA literal binding",
    )

    with SQLiteMemoryStore() as store:
        store.append_episode(original)
        store.append_episode(correction)
        assert store.count_episodes(
            tenant_id="tenant-a",
            privacy_scope="LOCAL_ONLY",
            catalog_version="catalog-v1",
        ) == 2
        loaded_original = store.get_episode(
            original.episode_id,
            tenant_id="tenant-a",
            privacy_scope="LOCAL_ONLY",
            catalog_version="catalog-v1",
        )
        loaded_correction = store.get_episode(
            correction.episode_id,
            tenant_id="tenant-a",
            privacy_scope="LOCAL_ONLY",
            catalog_version="catalog-v1",
        )

    assert loaded_original == original
    assert loaded_correction == correction
    assert loaded_correction is not None
    assert loaded_correction.corrects_episode_id == original.episode_id


def test_correction_requires_prior_record_in_the_same_scope() -> None:
    with SQLiteMemoryStore() as store:
        with pytest.raises(CorrectionReferenceError, match="existing prior"):
            store.append_episode(make_episode("orphan", corrects_episode_id="missing"))

        store.append_episode(make_episode("prior"))
        with pytest.raises(CorrectionReferenceError, match="must share"):
            store.append_episode(
                make_episode(
                    "wrong-tenant-correction",
                    tenant_id="tenant-b",
                    corrects_episode_id="prior",
                )
            )


def test_retrieval_eligible_correction_cannot_reference_held_out_data() -> None:
    held_out = make_episode(
        "dev-source",
        split="dev",
        retrieval_eligible=False,
    )
    leaking_correction = make_episode(
        "leaking-correction",
        split="train",
        retrieval_eligible=True,
        corrects_episode_id=held_out.episode_id,
    )

    with SQLiteMemoryStore() as store:
        store.append_episode(held_out)
        with pytest.raises(CorrectionReferenceError, match="same split"):
            store.append_episode(leaking_correction)


def test_insert_or_replace_cannot_overwrite_append_only_history(tmp_path: Path) -> None:
    database = tmp_path / "memory.sqlite3"
    original = make_episode("replace-target")
    with SQLiteMemoryStore(database) as store:
        store.append_episode(original)

        with sqlite3.connect(database) as connection:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(
                    """
                    INSERT OR REPLACE INTO learning_episodes (
                        episode_id, schema_version, tenant_id, privacy_scope,
                        catalog_version, split, disposition, retrieval_eligible,
                        request_text, retrieval_text, corrects_episode_id,
                        created_at, payload_json
                    )
                    SELECT episode_id, schema_version, tenant_id, privacy_scope,
                           catalog_version, split, disposition, retrieval_eligible,
                           'replaced', retrieval_text, corrects_episode_id,
                           created_at, payload_json
                    FROM learning_episodes WHERE episode_id = ?
                    """,
                    (original.episode_id,),
                )

        loaded = store.get_episode(
            original.episode_id,
            tenant_id=original.tenant_id,
            privacy_scope=original.privacy_scope,
            catalog_version=original.catalog_version,
        )

    assert loaded == original


def test_database_file_is_owner_only(tmp_path: Path) -> None:
    database = tmp_path / "private-memory.sqlite3"
    with SQLiteMemoryStore(database):
        pass
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


def test_legacy_store_version_fails_before_schema_use(tmp_path: Path) -> None:
    database = tmp_path / "legacy-memory.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE memory_store_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO memory_store_metadata(key, value) VALUES (?, ?)",
            ("store_schema_version", "1"),
        )

    with pytest.raises(MemoryStoreError, match="expected 2, found '1'"):
        SQLiteMemoryStore(database)


def test_token_overlap_retrieval_is_deterministic_and_bounded() -> None:
    episodes = [
        make_episode("ep-b", "compare AAPL earnings"),
        make_episode("ep-a", "compare AAPL earnings"),
        make_episode("ep-more-specific", "compare AAPL"),
        make_episode("unrelated", "summarize healthcare materials"),
    ]
    with SQLiteMemoryStore() as store:
        for episode in episodes:
            store.append_episode(episode)
        first = store.retrieve(make_query("compare AAPL", limit=3))
        second = store.retrieve(make_query("COMPARE aapl", limit=3))
        no_overlap = store.retrieve(make_query("quantum chemistry"))

    assert [item.episode.episode_id for item in first] == [
        "ep-more-specific",
        "ep-a",
        "ep-b",
    ]
    assert [item.episode.episode_id for item in second] == [
        item.episode.episode_id for item in first
    ]
    assert all(item.matched_tokens == ("aapl", "compare") for item in first)
    assert no_overlap == []
    assert tokenize("AAPL_aapl, 比较 股票") == ("aapl", "aapl", "比较", "股票")


def test_sqlite_table_rejects_update_and_delete_even_outside_store_api(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "memory.sqlite3"
    with SQLiteMemoryStore(db_path) as store:
        store.append_episode(make_episode("immutable"))

    with sqlite3.connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE learning_episodes SET request_text = 'changed' "
                "WHERE episode_id = 'immutable'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM learning_episodes WHERE episode_id = 'immutable'"
            )


def test_store_persists_round_trip_and_rejects_use_after_close(tmp_path: Path) -> None:
    db_path = tmp_path / "persist.sqlite3"
    episode = make_episode(
        "persisted",
        error_types=("WRONG_BINDING",),
        attributes={"nested": {"value": 1}},
    )
    with SQLiteMemoryStore(db_path) as store:
        store.append_episode(episode)

    with SQLiteMemoryStore(db_path) as reopened:
        loaded = reopened.get_episode(
            episode.episode_id,
            tenant_id=episode.tenant_id,
            privacy_scope=episode.privacy_scope,
            catalog_version=episode.catalog_version,
        )
    assert loaded == episode

    closed = SQLiteMemoryStore()
    closed.close()
    with pytest.raises(MemoryStoreError, match="closed"):
        closed.retrieve(make_query())
