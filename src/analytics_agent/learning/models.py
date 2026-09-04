"""Versioned, backend-neutral models for Phase 2 episodic memory.

The models intentionally live inside :mod:`analytics_agent.learning` so the
Phase 2 store does not depend on a future project-wide model module.  A later
Team A adapter can translate these records without changing the experiment's
on-disk contract.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Mapping

LEARNING_EPISODE_SCHEMA_VERSION = "learning-episode/v1"
MEMORY_QUERY_SCHEMA_VERSION = "memory-query/v1"

Split = Literal["train", "dev", "test"]
Disposition = Literal["generate", "clarify", "reject"]


class LearningModelValidationError(ValueError):
    """Raised when an episode or query violates the memory contract."""


def _require_text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LearningModelValidationError(f"{name} must be a non-empty string")
    return value.strip()


def _json_safe_mapping(name: str, value: Mapping[str, Any]) -> dict[str, Any]:
    copied = dict(value)
    try:
        json.dumps(copied, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise LearningModelValidationError(f"{name} must be JSON-serializable") from exc
    return copied


@dataclass(frozen=True, slots=True)
class LearningEpisode:
    """One immutable learning event.

    Corrections are represented by a *new* episode whose
    :attr:`corrects_episode_id` refers to an older episode.  The storage API
    never mutates the referenced record.
    """

    episode_id: str
    tenant_id: str
    privacy_scope: str
    catalog_version: str
    split: Split
    disposition: Disposition
    retrieval_eligible: bool
    request_text: str
    schema_version: str = LEARNING_EPISODE_SCHEMA_VERSION
    request_hash: str | None = None
    family: str | None = None
    feedback_source: str | None = None
    error_types: tuple[str, ...] = ()
    generated_job_spec_ref: str | None = None
    outcome_ref: str | None = None
    corrected_job_spec_ref: str | None = None
    corrects_episode_id: str | None = None
    feedback_summary: str | None = None
    correction_summary: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != LEARNING_EPISODE_SCHEMA_VERSION:
            raise LearningModelValidationError(
                "unsupported LearningEpisode schema_version: "
                f"{self.schema_version!r}"
            )

        for name in (
            "episode_id",
            "tenant_id",
            "privacy_scope",
            "catalog_version",
            "request_text",
            "created_at",
        ):
            object.__setattr__(self, name, _require_text(name, getattr(self, name)))

        if self.split not in {"train", "dev", "test"}:
            raise LearningModelValidationError(
                "split must be one of 'train', 'dev', or 'test'"
            )
        if self.disposition not in {"generate", "clarify", "reject"}:
            raise LearningModelValidationError(
                "disposition must be one of 'generate', 'clarify', or 'reject'"
            )
        if self.split != "train" and self.retrieval_eligible:
            raise LearningModelValidationError(
                "dev/test episodes cannot be retrieval eligible"
            )
        if self.corrects_episode_id == self.episode_id:
            raise LearningModelValidationError("an episode cannot correct itself")

        normalized_errors = tuple(_require_text("error_type", item) for item in self.error_types)
        object.__setattr__(self, "error_types", normalized_errors)
        object.__setattr__(
            self,
            "attributes",
            _json_safe_mapping("attributes", self.attributes),
        )

    @property
    def retrieval_text(self) -> str:
        """Return the deterministic text corpus used by the fallback retriever."""

        parts = [self.request_text]
        for optional in (
            self.family,
            self.feedback_source,
            self.feedback_summary,
            self.correction_summary,
        ):
            if optional:
                parts.append(optional)
        parts.extend(self.error_types)
        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-compatible representation."""

        return {
            "schema_version": self.schema_version,
            "episode_id": self.episode_id,
            "tenant_id": self.tenant_id,
            "privacy_scope": self.privacy_scope,
            "catalog_version": self.catalog_version,
            "split": self.split,
            "disposition": self.disposition,
            "retrieval_eligible": self.retrieval_eligible,
            "request_text": self.request_text,
            "request_hash": self.request_hash,
            "family": self.family,
            "feedback_source": self.feedback_source,
            "error_types": list(self.error_types),
            "generated_job_spec_ref": self.generated_job_spec_ref,
            "outcome_ref": self.outcome_ref,
            "corrected_job_spec_ref": self.corrected_job_spec_ref,
            "corrects_episode_id": self.corrects_episode_id,
            "feedback_summary": self.feedback_summary,
            "correction_summary": self.correction_summary,
            "created_at": self.created_at,
            "attributes": dict(self.attributes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LearningEpisode":
        """Rebuild an episode from its versioned JSON record."""

        data = dict(value)
        data["error_types"] = tuple(data.get("error_types") or ())
        data["attributes"] = data.get("attributes") or {}
        return cls(**data)


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    """A retrieval request with mandatory isolation filters.

    No caller-controlled split flag exists: adapters must always retrieve only
    eligible training episodes.
    """

    query_text: str
    tenant_id: str
    privacy_scope: str
    catalog_version: str
    disposition: Disposition
    limit: int = 4
    schema_version: str = MEMORY_QUERY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MEMORY_QUERY_SCHEMA_VERSION:
            raise LearningModelValidationError(
                f"unsupported MemoryQuery schema_version: {self.schema_version!r}"
            )
        for name in ("query_text", "tenant_id", "privacy_scope", "catalog_version"):
            object.__setattr__(self, name, _require_text(name, getattr(self, name)))
        if self.disposition not in {"generate", "clarify", "reject"}:
            raise LearningModelValidationError(
                "disposition must be one of 'generate', 'clarify', or 'reject'"
            )
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise LearningModelValidationError("limit must be an integer")
        if not 1 <= self.limit <= 100:
            raise LearningModelValidationError("limit must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class RetrievedEpisode:
    """An episode returned by a deterministic retrieval implementation."""

    episode: LearningEpisode
    score: float
    matched_tokens: tuple[str, ...]
