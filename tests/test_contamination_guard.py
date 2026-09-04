"""Cross-split contamination tests for the Phase 2 benchmark."""

from __future__ import annotations

import pytest

from analytics_agent.benchmark import (
    BenchmarkCase,
    BenchmarkContaminationError,
    ContaminationKind,
    Split,
    assert_no_hard_contamination,
    scan_contamination,
)


def _case(
    case_id: str,
    request: str,
    split: Split,
    *,
    paraphrase_group: str | None = None,
    entities: list[str] | None = None,
) -> BenchmarkCase:
    return BenchmarkCase(
        case_id=case_id,
        request=request,
        split=split,
        disposition="generate",
        family="profile",
        paraphrase_group=paraphrase_group or f"group-{case_id}",
        entities=entities or [],
        generate_gold={
            "template_id": "project.profile-v2",
            "catalog_version": "catalog-v2",
            "bindings": {},
            "output_spec": {"storage_class": "USER_PRIVATE"},
            "policy_constraints": {"execution_domain": "LOCAL_TRUSTED"},
        },
    )


def test_exact_request_across_splits_is_a_hard_failure() -> None:
    report = scan_contamination(
        [
            _case("train-1", "Profile AAPL", Split.TRAIN),
            _case("test-1", "Profile AAPL", Split.TEST),
        ]
    )
    kinds = {finding.kind for finding in report.hard_failures}
    assert ContaminationKind.REQUEST_HASH in kinds
    assert ContaminationKind.NORMALIZED_TEXT in kinds
    assert report.passed is False
    with pytest.raises(BenchmarkContaminationError):
        assert_no_hard_contamination(report)


def test_case_and_whitespace_variation_hits_normalized_text_only() -> None:
    report = scan_contamination(
        [
            _case("train-1", "Profile   AAPL", Split.TRAIN),
            _case("dev-1", "  profile aapl  ", Split.DEV),
        ]
    )
    kinds = {finding.kind for finding in report.hard_failures}
    assert ContaminationKind.NORMALIZED_TEXT in kinds
    assert ContaminationKind.REQUEST_HASH not in kinds


def test_paraphrase_group_must_not_cross_splits() -> None:
    report = scan_contamination(
        [
            _case(
                "train-1",
                "Show Apple profile",
                Split.TRAIN,
                paraphrase_group="profile-apple-01",
            ),
            _case(
                "test-1",
                "Who is Apple?",
                Split.TEST,
                paraphrase_group="PROFILE-APPLE-01",
            ),
        ]
    )
    assert any(
        finding.kind is ContaminationKind.PARAPHRASE_GROUP
        for finding in report.hard_failures
    )


def test_reuse_inside_one_split_is_not_cross_split_contamination() -> None:
    report = scan_contamination(
        [
            _case(
                "train-1",
                "Show Apple profile",
                Split.TRAIN,
                paraphrase_group="same-train-group",
            ),
            _case(
                "train-2",
                "Who is Apple?",
                Split.TRAIN,
                paraphrase_group="same-train-group",
            ),
        ]
    )
    assert report.passed is True


def test_entity_set_overlap_is_reported_but_not_a_hard_failure() -> None:
    report = scan_contamination(
        [
            _case(
                "train-aapl",
                "Show Apple profile",
                Split.TRAIN,
                entities=["AAPL"],
            ),
            _case(
                "test-aapl",
                "Summarize the company named by the ticker",
                Split.TEST,
                entities=["aapl"],
            ),
        ]
    )
    assert report.hard_failures == []
    assert report.passed is True
    assert len(report.entity_overlaps) == 1
    assert report.entity_overlaps[0].entities == ["aapl"]
    assert_no_hard_contamination(report)
