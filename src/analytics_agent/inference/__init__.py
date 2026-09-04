"""Provider-neutral local inference seam for Phase 2."""

from .base import (
    EvidenceIneligibleError,
    InferenceAdapter,
    InferenceMessage,
    InferenceReadiness,
    InferenceRequest,
    InferenceResult,
    ProviderMetadata,
    require_evidence_eligible,
)
from .fake import FakeInferenceAdapter

__all__ = [
    "EvidenceIneligibleError",
    "FakeInferenceAdapter",
    "InferenceAdapter",
    "InferenceMessage",
    "InferenceReadiness",
    "InferenceRequest",
    "InferenceResult",
    "ProviderMetadata",
    "require_evidence_eligible",
]
