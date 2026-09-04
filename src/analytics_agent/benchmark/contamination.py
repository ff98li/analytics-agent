"""Cross-split contamination detection for Phase 2 benchmark cases."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable
from collections import defaultdict
from enum import Enum

from pydantic import Field

from .models import BenchmarkCase, StrictBenchmarkModel


class ContaminationKind(str, Enum):
    REQUEST_HASH = "request_hash"
    NORMALIZED_TEXT = "normalized_text"
    PARAPHRASE_GROUP = "paraphrase_group"


class ContaminationFinding(StrictBenchmarkModel):
    kind: ContaminationKind
    fingerprint: str = Field(min_length=1)
    case_ids: list[str] = Field(min_length=2)
    splits: list[str] = Field(min_length=2)


class EntityOverlap(StrictBenchmarkModel):
    """Informational entity-set reuse; it never makes the report fail."""

    entities: list[str] = Field(min_length=1)
    case_ids: list[str] = Field(min_length=2)
    splits: list[str] = Field(min_length=2)


class ContaminationReport(StrictBenchmarkModel):
    hard_failures: list[ContaminationFinding] = Field(default_factory=list)
    entity_overlaps: list[EntityOverlap] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.hard_failures


class BenchmarkContaminationError(ValueError):
    """Raised when a caller requires a contamination-free benchmark."""


def normalize_request_text(text: str) -> str:
    """Normalize Unicode, case, and whitespace without changing semantics."""

    normalized = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalized_label(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _hard_findings(
    cases: list[BenchmarkCase],
    *,
    kind: ContaminationKind,
    key_for_case: Callable[[BenchmarkCase], str],
) -> list[ContaminationFinding]:
    groups: dict[str, list[BenchmarkCase]] = defaultdict(list)
    for case in cases:
        groups[key_for_case(case)].append(case)

    findings: list[ContaminationFinding] = []
    for key, grouped_cases in groups.items():
        split_names = sorted({case.split.value for case in grouped_cases})
        if len(split_names) < 2:
            continue
        findings.append(
            ContaminationFinding(
                kind=kind,
                fingerprint=key,
                case_ids=sorted(case.case_id for case in grouped_cases),
                splits=split_names,
            )
        )
    return sorted(findings, key=lambda item: (item.kind.value, item.fingerprint))


def _entity_overlaps(cases: list[BenchmarkCase]) -> list[EntityOverlap]:
    groups: dict[tuple[str, ...], list[BenchmarkCase]] = defaultdict(list)
    for case in cases:
        entity_set = tuple(sorted({_normalized_label(value) for value in case.entities}))
        if entity_set:
            groups[entity_set].append(case)

    overlaps: list[EntityOverlap] = []
    for entity_set, grouped_cases in groups.items():
        split_names = sorted({case.split.value for case in grouped_cases})
        if len(split_names) < 2:
            continue
        overlaps.append(
            EntityOverlap(
                entities=list(entity_set),
                case_ids=sorted(case.case_id for case in grouped_cases),
                splits=split_names,
            )
        )
    return sorted(overlaps, key=lambda item: tuple(item.entities))


def scan_contamination(cases: list[BenchmarkCase]) -> ContaminationReport:
    """Return hard cross-split collisions and informational entity reuse."""

    hard_failures: list[ContaminationFinding] = []
    hard_failures.extend(
        _hard_findings(
            cases,
            kind=ContaminationKind.REQUEST_HASH,
            key_for_case=lambda case: _sha256(case.request),
        )
    )
    hard_failures.extend(
        _hard_findings(
            cases,
            kind=ContaminationKind.NORMALIZED_TEXT,
            key_for_case=lambda case: _sha256(
                normalize_request_text(case.request)
            ),
        )
    )
    hard_failures.extend(
        _hard_findings(
            cases,
            kind=ContaminationKind.PARAPHRASE_GROUP,
            key_for_case=lambda case: _normalized_label(case.paraphrase_group),
        )
    )
    hard_failures.sort(key=lambda item: (item.kind.value, item.fingerprint))
    return ContaminationReport(
        hard_failures=hard_failures,
        entity_overlaps=_entity_overlaps(cases),
    )


def assert_no_hard_contamination(report: ContaminationReport) -> None:
    if report.passed:
        return
    summary = ", ".join(
        f"{finding.kind.value}:{'/'.join(finding.case_ids)}"
        for finding in report.hard_failures
    )
    raise BenchmarkContaminationError(f"cross-split contamination detected: {summary}")
