"""Contract required from Team B or a project-owned local fallback."""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import ConfigDict, Field, ValidationError, model_validator

from analytics_agent.models import StrictModel


class EvidenceIneligibleError(RuntimeError):
    """Raised when a test double is used as experimental evidence."""


class InferenceMessage(StrictModel):
    role: Literal["system", "user", "assistant"]
    content: str


class InferenceRequest(StrictModel):
    messages: list[InferenceMessage] = Field(min_length=1)
    response_schema: dict[str, Any]
    seed: int
    temperature: float = Field(ge=0.0, le=2.0)
    max_tokens: int = Field(gt=0)


class InferenceReadiness(StrictModel):
    state: Literal["ready", "not_ready", "test_only"]
    detail_code: str = Field(min_length=1)


class ProviderMetadata(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str = Field(min_length=1)
    endpoint: str | None
    model_artifact: str | None
    model_revision: str | None = None
    protocol: str = Field(min_length=1)
    capability_version: str = Field(min_length=1)
    tokenizer_artifact: str | None = None
    structured_output: bool
    auth_mode: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    lifecycle_mode: Literal["external", "per_allocation", "in_process_test"]
    lifecycle_owner: str = Field(min_length=1)
    resource_owner: str = Field(min_length=1)
    evidence_eligible: bool
    test_double: bool = False

    @model_validator(mode="after")
    def validate_evidence_metadata(self) -> ProviderMetadata:
        if self.evidence_eligible:
            if self.test_double:
                raise ValueError("test doubles cannot be evidence eligible")
            if self.lifecycle_mode == "in_process_test":
                raise ValueError(
                    "in-process test providers cannot be evidence eligible"
                )
            if (
                self.provider_id == "fake-test-double"
                or self.protocol == "in-process"
                or self.auth_mode == "none-test-only"
                or self.lifecycle_owner == "pytest"
                or self.resource_owner == "pytest"
            ):
                raise ValueError(
                    "test-only provider markers cannot be evidence eligible"
                )
            if not self.endpoint or not self.model_artifact or not self.model_revision:
                raise ValueError(
                    "evidence-eligible providers require endpoint, model_artifact, "
                    "and model_revision"
                )
        return self


class InferenceResult(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content: dict[str, Any]
    provider: ProviderMetadata
    latency_ms: int = Field(ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)


@runtime_checkable
class InferenceAdapter(Protocol):
    @property
    def metadata(self) -> ProviderMetadata: ...

    def readiness(self) -> InferenceReadiness: ...

    def generate(self, request: InferenceRequest) -> InferenceResult: ...


def require_evidence_eligible(
    result: InferenceResult,
    *,
    adapter: InferenceAdapter,
    approved_provider: ProviderMetadata,
) -> None:
    """Validate adapter and result against an approved frozen G0 contract."""

    # ``model_copy(update=...)`` intentionally skips Pydantic validation.  An
    # evidence boundary must therefore reconstruct and revalidate the complete
    # result rather than trusting an object's earlier construction history.
    try:
        checked = InferenceResult.model_validate(result.model_dump(mode="python"))
    except (AttributeError, TypeError, ValidationError, ValueError) as exc:
        raise EvidenceIneligibleError("inference result metadata is invalid") from exc

    provider = checked.provider
    if (
        not provider.evidence_eligible
        or provider.test_double
        or provider.lifecycle_mode == "in_process_test"
    ):
        raise EvidenceIneligibleError(
            f"provider {provider.provider_id!r} is test-only"
        )

    try:
        approved = ProviderMetadata.model_validate(
            approved_provider.model_dump(mode="python")
        )
    except (AttributeError, TypeError, ValidationError, ValueError) as exc:
        raise EvidenceIneligibleError("approved G0 provider contract is invalid") from exc
    if not approved.evidence_eligible or approved.test_double:
        raise EvidenceIneligibleError(
            "approved G0 provider contract is not evidence eligible"
        )

    try:
        adapter_provider = ProviderMetadata.model_validate(
            adapter.metadata.model_dump(mode="python")
        )
        readiness = InferenceReadiness.model_validate(
            adapter.readiness().model_dump(mode="python")
        )
    except (AttributeError, TypeError, ValidationError, ValueError) as exc:
        raise EvidenceIneligibleError("inference adapter contract is invalid") from exc
    if readiness.state != "ready":
        raise EvidenceIneligibleError(
            f"inference adapter is not ready: {readiness.detail_code}"
        )
    if provider != approved or adapter_provider != approved:
        raise EvidenceIneligibleError(
            "adapter/result provider metadata does not match the approved G0 contract"
        )
