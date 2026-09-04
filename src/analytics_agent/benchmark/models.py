"""Strict Pydantic contracts for the Phase 2 benchmark manifest."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictBenchmarkModel(BaseModel):
    """Reject misspelled fields in benchmark evidence."""

    model_config = ConfigDict(extra="forbid")


class ManifestStatus(str, Enum):
    DRAFT = "draft"
    FROZEN = "frozen"


class Split(str, Enum):
    TRAIN = "train"
    DEV = "dev"
    TEST = "test"


class Disposition(str, Enum):
    GENERATE = "generate"
    CLARIFY = "clarify"
    REJECT = "reject"


FROZEN_CELL_COUNTS: dict[Split, dict[Disposition, int]] = {
    Split.TRAIN: {
        Disposition.GENERATE: 42,
        Disposition.CLARIFY: 9,
        Disposition.REJECT: 9,
    },
    Split.DEV: {
        Disposition.GENERATE: 18,
        Disposition.CLARIFY: 6,
        Disposition.REJECT: 6,
    },
    Split.TEST: {
        Disposition.GENERATE: 48,
        Disposition.CLARIFY: 6,
        Disposition.REJECT: 6,
    },
}

GENERATE_FAMILIES: tuple[str, ...] = (
    "profile",
    "news",
    "fundamentals",
    "market",
    "trading",
    "compare",
)

FROZEN_GENERATE_PER_FAMILY: dict[Split, int] = {
    Split.TRAIN: 7,
    Split.DEV: 3,
    Split.TEST: 8,
}


class GenerateGold(StrictBenchmarkModel):
    """Canonical gold for a request that should produce a JobSpec."""

    template_id: str = Field(min_length=1)
    catalog_version: str = Field(min_length=1)
    equivalence_version: Literal["job-spec-equivalence/v1"] = (
        "job-spec-equivalence/v1"
    )
    edit_spec: list[dict[str, Any]] = Field(default_factory=list)
    bindings: dict[str, Any]
    output_spec: dict[str, Any] = Field(min_length=1)
    policy_constraints: dict[str, Any] = Field(min_length=1)


class DecisionGold(StrictBenchmarkModel):
    """Expected structured response for clarify/reject cases."""

    reason_code: str = Field(min_length=1)
    response_constraints: list[str] = Field(default_factory=list)


class BenchmarkCase(StrictBenchmarkModel):
    """One independently scored Phase 2 request."""

    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    request: str = Field(min_length=1)
    split: Split
    disposition: Disposition
    family: str = Field(min_length=1)
    paraphrase_group: str = Field(min_length=1)
    entities: list[str] = Field(default_factory=list)
    retrieval_eligible: bool = False
    generate_gold: GenerateGold | None = None
    decision_gold: DecisionGold | None = None

    @field_validator("request", "family", "paraphrase_group")
    @classmethod
    def strip_nonempty_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped

    @field_validator("entities")
    @classmethod
    def require_unique_entities(cls, entities: list[str]) -> list[str]:
        seen: set[str] = set()
        for entity in entities:
            normalized = unicodedata.normalize("NFKC", entity).strip().casefold()
            if not normalized:
                raise ValueError("entities must not contain blank values")
            if normalized in seen:
                raise ValueError("entities must be unique within a case")
            seen.add(normalized)
        return entities

    @model_validator(mode="after")
    def enforce_split_and_gold_contract(self) -> BenchmarkCase:
        if self.retrieval_eligible and self.split is not Split.TRAIN:
            raise ValueError("only train cases may be retrieval_eligible")

        if self.disposition is Disposition.GENERATE:
            if self.family not in GENERATE_FAMILIES:
                raise ValueError(
                    "generate family must be one of " + ", ".join(GENERATE_FAMILIES)
                )
            if self.generate_gold is None or self.decision_gold is not None:
                raise ValueError(
                    "generate cases require generate_gold and forbid decision_gold"
                )
        elif self.decision_gold is None or self.generate_gold is not None:
            raise ValueError(
                "clarify/reject cases require decision_gold and forbid generate_gold"
            )
        return self


def compute_split_hashes(cases: list[BenchmarkCase]) -> dict[Split, str]:
    """Hash the canonical JSON representation of each split.

    Case order in the source file does not affect the digest; ``case_id`` is the
    stable ordering key.  The hashes therefore identify benchmark content, not
    incidental formatting.
    """

    result: dict[Split, str] = {}
    for split in Split:
        payload = [
            case.model_dump(mode="json", exclude_none=False)
            for case in sorted(
                (case for case in cases if case.split is split),
                key=lambda item: item.case_id,
            )
        ]
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        result[split] = hashlib.sha256(encoded).hexdigest()
    return result


class BenchmarkManifest(StrictBenchmarkModel):
    """Versioned authoring or frozen-evidence manifest.

    Draft manifests may be empty or incomplete.  Frozen manifests must match
    every cell of the pre-registered 60/30/60 and 108/21/21 protocol and must
    carry hashes computed from their actual content.
    """

    schema_version: Literal["0.2"] = "0.2"
    benchmark_id: str = Field(min_length=1)
    status: ManifestStatus = ManifestStatus.DRAFT
    description: str = Field(min_length=1)
    cases: list[BenchmarkCase] = Field(default_factory=list)
    split_hashes: dict[Split, str] | None = None

    @field_validator("benchmark_id", "description")
    @classmethod
    def strip_manifest_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped

    @model_validator(mode="after")
    def enforce_manifest_contract(self) -> BenchmarkManifest:
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("case_id values must be unique")

        if self.split_hashes is not None:
            if set(self.split_hashes) != set(Split):
                raise ValueError("split_hashes must contain train, dev, and test")
            if any(
                re.fullmatch(r"[0-9a-f]{64}", digest) is None
                for digest in self.split_hashes.values()
            ):
                raise ValueError("split_hashes values must be lowercase SHA-256")
            expected_hashes = compute_split_hashes(self.cases)
            if self.split_hashes != expected_hashes:
                raise ValueError("split_hashes do not match manifest case content")

        if self.status is ManifestStatus.DRAFT:
            return self

        if self.split_hashes is None:
            raise ValueError("frozen manifests require verified split_hashes")

        counts = Counter((case.split, case.disposition) for case in self.cases)
        mismatches: list[str] = []
        for split, expected_by_disposition in FROZEN_CELL_COUNTS.items():
            for disposition, expected in expected_by_disposition.items():
                actual = counts[(split, disposition)]
                if actual != expected:
                    mismatches.append(
                        f"{split.value}/{disposition.value}: "
                        f"expected {expected}, got {actual}"
                    )
        if mismatches:
            raise ValueError(
                "frozen manifest count mismatch (requires split totals "
                "60/30/60 and disposition totals 108/21/21): "
                + "; ".join(mismatches)
            )

        family_counts = Counter(
            (case.split, case.family)
            for case in self.cases
            if case.disposition is Disposition.GENERATE
        )
        family_mismatches: list[str] = []
        for split, expected in FROZEN_GENERATE_PER_FAMILY.items():
            for family in GENERATE_FAMILIES:
                actual = family_counts[(split, family)]
                if actual != expected:
                    family_mismatches.append(
                        f"{split.value}/{family}: expected {expected}, got {actual}"
                    )
        if family_mismatches:
            raise ValueError(
                "frozen generate-family count mismatch: "
                + "; ".join(family_mismatches)
            )

        # A frozen object is evidence-bearing; contamination must not depend on
        # callers remembering to choose a stricter loader.
        from .contamination import scan_contamination

        report = scan_contamination(self.cases)
        if not report.passed:
            summary = ", ".join(
                f"{finding.kind.value}:{'/'.join(finding.case_ids)}"
                for finding in report.hard_failures
            )
            raise ValueError(
                "frozen manifest contains cross-split contamination: " + summary
            )
        return self
