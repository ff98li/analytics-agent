"""Deterministic test double. Its output is never valid B1/B2 evidence."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .base import (
    InferenceReadiness,
    InferenceRequest,
    InferenceResult,
    ProviderMetadata,
)


class FakeInferenceAdapter:
    def __init__(self, response: dict[str, Any]) -> None:
        self._response = deepcopy(response)
        self._metadata = ProviderMetadata(
            provider_id="fake-test-double",
            endpoint=None,
            model_artifact=None,
            protocol="in-process",
            capability_version="fake-v1",
            structured_output=True,
            auth_mode="none-test-only",
            timeout_seconds=1.0,
            lifecycle_mode="in_process_test",
            lifecycle_owner="pytest",
            resource_owner="pytest",
            evidence_eligible=False,
            test_double=True,
        )

    @property
    def metadata(self) -> ProviderMetadata:
        return self._metadata

    def readiness(self) -> InferenceReadiness:
        return InferenceReadiness(state="test_only", detail_code="SCRIPTED_RESPONSE")

    def generate(self, request: InferenceRequest) -> InferenceResult:
        # Validate the complete request even though the response is scripted.
        InferenceRequest.model_validate(request)
        return InferenceResult(
            content=deepcopy(self._response),
            provider=self.metadata,
            latency_ms=0,
        )
