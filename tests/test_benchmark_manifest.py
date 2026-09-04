"""Contract tests for draft and frozen Phase 2 benchmark manifests."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from analytics_agent.benchmark import (
    BenchmarkCase,
    BenchmarkManifest,
    DecisionGold,
    Disposition,
    GenerateGold,
    ManifestStatus,
    Split,
    compute_split_hashes,
    load_manifest,
)
from analytics_agent.benchmark.models import FROZEN_CELL_COUNTS
from analytics_agent.benchmark.models import GENERATE_FAMILIES

DRAFT_PATH = (
    Path(__file__).resolve().parents[1]
    / "nl-requests"
    / "phase2"
    / "manifest.draft.json"
)


def _case(
    case_id: str,
    *,
    split: Split = Split.TRAIN,
    disposition: Disposition = Disposition.GENERATE,
    retrieval_eligible: bool = False,
    family: str | None = None,
    paraphrase_group: str | None = None,
) -> BenchmarkCase:
    common = {
        "case_id": case_id,
        "request": f"Request for {case_id}",
        "split": split,
        "disposition": disposition,
        "family": family or (
            "profile" if disposition is Disposition.GENERATE else "decision"
        ),
        "paraphrase_group": paraphrase_group or f"group-{case_id}",
        "entities": [case_id.upper()],
        "retrieval_eligible": retrieval_eligible,
    }
    if disposition is Disposition.GENERATE:
        return BenchmarkCase(
            **common,
            generate_gold=GenerateGold(
                template_id="project.profile-v2",
                catalog_version="catalog-v2",
                bindings={"symbols": [case_id.upper()]},
                output_spec={"storage_class": "USER_PRIVATE"},
                policy_constraints={"execution_domain": "LOCAL_TRUSTED"},
            ),
        )
    return BenchmarkCase(
        **common,
        decision_gold=DecisionGold(reason_code=f"expected-{disposition.value}"),
    )


def _complete_frozen_cases() -> list[BenchmarkCase]:
    cases: list[BenchmarkCase] = []
    for split, dispositions in FROZEN_CELL_COUNTS.items():
        for disposition, count in dispositions.items():
            if disposition is Disposition.GENERATE:
                per_family = count // len(GENERATE_FAMILIES)
                case_cells = (
                    (family, index)
                    for family in GENERATE_FAMILIES
                    for index in range(per_family)
                )
            else:
                case_cells = (("decision", index) for index in range(count))
            for family, index in case_cells:
                cases.append(
                    _case(
                        f"{split.value}-{disposition.value}-{family}-{index:03d}",
                        split=split,
                        disposition=disposition,
                        retrieval_eligible=(split is Split.TRAIN),
                        family=family,
                    )
                )
    return cases


def test_checked_in_manifest_is_explicitly_empty_draft() -> None:
    manifest = load_manifest(DRAFT_PATH)
    assert manifest.status is ManifestStatus.DRAFT
    assert manifest.cases == []
    assert manifest.split_hashes is None


def test_draft_does_not_require_frozen_counts_or_hashes() -> None:
    manifest = BenchmarkManifest(
        benchmark_id="partial-authoring",
        status="draft",
        description="One reviewed case so far.",
        cases=[_case("draft-001", retrieval_eligible=True)],
    )
    assert len(manifest.cases) == 1


@pytest.mark.parametrize("split", [Split.DEV, Split.TEST])
def test_only_train_cases_may_be_retrieval_eligible(split: Split) -> None:
    with pytest.raises(ValidationError, match="only train cases"):
        _case("not-train", split=split, retrieval_eligible=True)


def test_disposition_requires_the_matching_gold_contract() -> None:
    with pytest.raises(ValidationError, match="generate cases require"):
        BenchmarkCase(
            case_id="bad-generate",
            request="Generate something",
            split="train",
            disposition="generate",
            family="profile",
            paraphrase_group="bad-generate",
            decision_gold={"reason_code": "wrong-kind"},
        )

    with pytest.raises(ValidationError, match="clarify/reject cases require"):
        BenchmarkCase(
            case_id="bad-clarify",
            request="Ambiguous request",
            split="train",
            disposition="clarify",
            family="decision",
            paraphrase_group="bad-clarify",
            generate_gold={
                "template_id": "project.profile-v2",
                "catalog_version": "catalog-v2",
                "bindings": {},
                "output_spec": {"storage_class": "USER_PRIVATE"},
                "policy_constraints": {"execution_domain": "LOCAL_TRUSTED"},
            },
        )


def test_frozen_manifest_rejects_draft_sized_content() -> None:
    cases = [_case("too-small")]
    with pytest.raises(ValidationError, match="frozen manifest count mismatch"):
        BenchmarkManifest(
            benchmark_id="not-complete",
            status="frozen",
            description="Must not freeze.",
            cases=cases,
            split_hashes=compute_split_hashes(cases),
        )


def test_complete_frozen_manifest_enforces_preregistered_counts() -> None:
    cases = _complete_frozen_cases()
    manifest = BenchmarkManifest(
        benchmark_id="complete",
        status="frozen",
        description="Synthetic contract fixture, not checked-in evidence.",
        cases=cases,
        split_hashes=compute_split_hashes(cases),
    )

    split_counts = Counter(case.split for case in manifest.cases)
    disposition_counts = Counter(case.disposition for case in manifest.cases)
    assert split_counts == {Split.TRAIN: 60, Split.DEV: 30, Split.TEST: 60}
    assert disposition_counts == {
        Disposition.GENERATE: 108,
        Disposition.CLARIFY: 21,
        Disposition.REJECT: 21,
    }


def test_frozen_manifest_enforces_generate_family_distribution() -> None:
    cases = _complete_frozen_cases()
    index = next(
        index
        for index, case in enumerate(cases)
        if case.split is Split.TRAIN and case.family == "news"
    )
    cases[index] = cases[index].model_copy(update={"family": "profile"})

    with pytest.raises(ValidationError, match="generate-family count mismatch"):
        BenchmarkManifest(
            benchmark_id="bad-family-cells",
            status="frozen",
            description="Counts match globally but not by family.",
            cases=cases,
            split_hashes=compute_split_hashes(cases),
        )


def test_frozen_manifest_always_rejects_cross_split_contamination() -> None:
    cases = _complete_frozen_cases()
    train = next(case for case in cases if case.split is Split.TRAIN)
    test_index = next(
        index for index, case in enumerate(cases) if case.split is Split.TEST
    )
    cases[test_index] = cases[test_index].model_copy(
        update={"paraphrase_group": train.paraphrase_group}
    )

    with pytest.raises(ValidationError, match="cross-split contamination"):
        BenchmarkManifest(
            benchmark_id="contaminated-frozen",
            status="frozen",
            description="Must fail even through the ordinary model loader.",
            cases=cases,
            split_hashes=compute_split_hashes(cases),
        )


def test_manifest_rejects_stale_split_hashes() -> None:
    cases = [_case("hash-case")]
    hashes = compute_split_hashes(cases)
    hashes[Split.TRAIN] = "0" * 64
    with pytest.raises(ValidationError, match="do not match"):
        BenchmarkManifest(
            benchmark_id="stale-hash",
            description="Hash mismatch fixture.",
            cases=cases,
            split_hashes=hashes,
        )


def test_manifest_rejects_duplicate_case_ids() -> None:
    duplicate = _case("duplicate")
    with pytest.raises(ValidationError, match="case_id values must be unique"):
        BenchmarkManifest(
            benchmark_id="duplicate-ids",
            description="Duplicate ID fixture.",
            cases=[duplicate, duplicate.model_copy()],
        )
