"""The Team B seam can be developed without accepting fake evidence."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from analytics_agent.inference import (
    EvidenceIneligibleError,
    FakeInferenceAdapter,
    InferenceRequest,
    ProviderMetadata,
    require_evidence_eligible,
)


def _request() -> InferenceRequest:
    return InferenceRequest(
        messages=[{"role": "user", "content": "Plan an AAPL profile workflow."}],
        response_schema={"type": "object"},
        seed=17,
        temperature=0.0,
        max_tokens=256,
    )


def test_fake_adapter_is_deterministic_and_test_only() -> None:
    adapter = FakeInferenceAdapter({"template_id": "project.profile-b0"})
    first = adapter.generate(_request())
    second = adapter.generate(_request())

    assert first.content == second.content
    assert first.provider.test_double is True
    assert first.provider.evidence_eligible is False
    assert adapter.readiness().state == "test_only"
    with pytest.raises(EvidenceIneligibleError, match="test-only"):
        require_evidence_eligible(
            first,
            adapter=adapter,
            approved_provider=adapter.metadata,
        )


def test_evidence_provider_requires_reproducible_identity() -> None:
    with pytest.raises(ValidationError, match="endpoint, model_artifact"):
        ProviderMetadata(
            provider_id="incomplete-real-provider",
            endpoint=None,
            model_artifact=None,
            protocol="openai-compatible",
            capability_version="v1",
            structured_output=True,
            auth_mode="bearer",
            timeout_seconds=30,
            lifecycle_mode="external",
            lifecycle_owner="team-b",
            resource_owner="team-b",
            evidence_eligible=True,
        )


def test_test_double_cannot_claim_evidence_eligibility() -> None:
    with pytest.raises(ValidationError, match="test doubles"):
        ProviderMetadata(
            provider_id="dishonest-fake",
            endpoint="http://127.0.0.1:1",
            model_artifact="fake/model",
            model_revision="fake-revision",
            protocol="in-process",
            capability_version="fake-v1",
            structured_output=True,
            auth_mode="none",
            timeout_seconds=1,
            lifecycle_mode="in_process_test",
            lifecycle_owner="pytest",
            resource_owner="pytest",
            evidence_eligible=True,
            test_double=True,
        )


def test_evidence_metadata_is_frozen() -> None:
    provider = FakeInferenceAdapter({"ok": True}).metadata
    with pytest.raises(ValidationError, match="frozen"):
        provider.evidence_eligible = True


def test_evidence_gate_revalidates_unchecked_model_copy() -> None:
    result = FakeInferenceAdapter({"ok": True}).generate(_request())
    forged_provider = result.provider.model_copy(
        update={
            "test_double": False,
            "evidence_eligible": True,
            "endpoint": "http://127.0.0.1:1",
            "model_artifact": "fake/model",
            "model_revision": "fake-revision",
            "lifecycle_mode": "external",
        }
    )
    forged_result = result.model_copy(update={"provider": forged_provider})

    with pytest.raises(EvidenceIneligibleError, match="metadata is invalid"):
        require_evidence_eligible(
            forged_result,
            adapter=FakeInferenceAdapter({"ok": True}),
            approved_provider=forged_provider,
        )


def test_evidence_gate_requires_exact_approved_g0_contract() -> None:
    approved = ProviderMetadata(
        provider_id="team-b-local",
        endpoint="https://inference.internal/v1",
        model_artifact="org/model",
        model_revision="sha256:abc123",
        tokenizer_artifact="org/model-tokenizer@sha256:def456",
        protocol="openai-compatible",
        capability_version="v1",
        structured_output=True,
        auth_mode="bearer",
        timeout_seconds=30,
        lifecycle_mode="external",
        lifecycle_owner="team-b",
        resource_owner="team-b",
        evidence_eligible=True,
    )
    class ContractAdapter:
        metadata = approved

        def readiness(self):
            from analytics_agent.inference import InferenceReadiness

            return InferenceReadiness(state="ready", detail_code="MODEL_LOADED")

        def generate(self, request):
            from analytics_agent.inference import InferenceResult

            return InferenceResult(
                content={"ok": True},
                provider=self.metadata,
                latency_ms=1,
            )

    adapter = ContractAdapter()
    result = adapter.generate(_request())
    require_evidence_eligible(
        result,
        adapter=adapter,
        approved_provider=approved,
    )

    fake_adapter = FakeInferenceAdapter({"ok": True})
    relabeled_fake = fake_adapter.generate(_request()).model_copy(
        update={"provider": approved}
    )
    with pytest.raises(EvidenceIneligibleError, match="not ready"):
        require_evidence_eligible(
            relabeled_fake,
            adapter=fake_adapter,
            approved_provider=approved,
        )

    changed = approved.model_copy(update={"model_revision": "sha256:different"})
    with pytest.raises(EvidenceIneligibleError, match="does not match"):
        require_evidence_eligible(
            result,
            adapter=adapter,
            approved_provider=changed,
        )
