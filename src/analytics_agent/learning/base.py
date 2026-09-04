"""Backend contract for Phase 2 episodic memory."""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from .models import LearningEpisode, MemoryQuery, RetrievedEpisode


@runtime_checkable
class MemoryAdapter(Protocol):
    """Replaceable memory backend used by the learning loop."""

    def append_episode(self, episode: LearningEpisode) -> None:
        """Append a new immutable episode, rejecting duplicate IDs."""

    def get_episode(
        self,
        episode_id: str,
        *,
        tenant_id: str,
        privacy_scope: str,
        catalog_version: str,
    ) -> LearningEpisode | None:
        """Read one episode only within an exact isolation scope."""

    def retrieve(self, query: MemoryQuery) -> Sequence[RetrievedEpisode]:
        """Retrieve eligible training episodes within the query's scope."""

    def close(self) -> None:
        """Release backend resources."""
