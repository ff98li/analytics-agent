"""Contract tests for the Phase 2 JobSpec and three success definitions."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from analytics_agent.api import PlanningContext, plan
from analytics_agent.models import (
    Disposition,
    ExecutionOutcome,
    ExecutionStatus,
    JobSpec,
    JobSpecAssessment,
    OutputContractStatus,
    RuntimeAssessment,
    SuccessAssessment,
)
from analytics_agent.nl2workflow import nl2workflow


def _semantic_success(**overrides: bool) -> JobSpecAssessment:
    values = {
        "correct_disposition": True,
        "correct_logical_plan": True,
        "correct_rendered_workflow": True,
        "correct_bindings": True,
        "correct_planned_output_spec": True,
        "correct_policy": True,
        "static_validation_pass": True,
    }
    values.update(overrides)
    return JobSpecAssessment(**values)


def test_internal_plan_preserves_dag_facade() -> None:
    request = "Compare AAPL and NVDA fundamentals."
    job = plan(request, PlanningContext(request_id="p2-test-001"))

    assert job.workflow == nl2workflow(request)
    assert job.disposition is Disposition.GENERATE
    assert job.bindings["symbols"]["values"] == ["AAPL", "NVDA"]
    assert job.output is not None
    assert job.output.storage_class == "USER_PRIVATE"
    assert job.output.uri.startswith("s3://lumilake-private/")


def test_dag_facade_delegates_to_internal_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = {"name": "delegated", "ops": [], "outputs": []}

    class Planned:
        workflow = expected

    def fake_plan(_request: str, _context: PlanningContext | None) -> Planned:
        return Planned()

    monkeypatch.setattr("analytics_agent.api.plan", fake_plan)
    assert nl2workflow("Any request") is expected


def test_dag_facade_preserves_ignored_legacy_context() -> None:
    request = "Give me Apple's company profile."
    assert nl2workflow(request, {"legacy_caller_state": "ignored"}) == nl2workflow(
        request
    )


def test_compare_default_bindings_match_frozen_b0_dag() -> None:
    job = plan("Compare both companies")
    assert job.bindings["symbols"]["values"] == ["AAPL", "NVDA"]
    assert [op["id"] for op in job.workflow["ops"]] == [
        "Fundamentals Query AAPL",
        "Fundamentals Query NVDA",
    ]


def test_plan_separates_stable_request_identity_from_unique_run_id() -> None:
    first = plan("Give me Apple's company profile.")
    second = plan("Give me Apple's company profile.")
    assert first.request_fingerprint == second.request_fingerprint
    assert first.request_id != second.request_id
    assert first.output is not None and second.output is not None
    assert first.output.uri != second.output.uri


def test_frozen_b0_cannot_be_relabeled_as_a_learned_planner() -> None:
    with pytest.raises(ValueError, match="cannot claim B1/B2"):
        plan(
            "Give me Apple's company profile.",
            {"generator_id": "b2-corr", "model_artifact": "claimed-real"},
        )


def test_generate_jobspec_requires_complete_payload() -> None:
    with pytest.raises(ValidationError, match="generate JobSpec requires"):
        JobSpec(
            request_id="bad",
            request_fingerprint="0" * 64,
            nl_request="Analyze AAPL",
            disposition="generate",
            planner_metadata={
                "generator_id": "b0",
                "catalog_version": "phase1-b0-frozen",
            },
        )


@pytest.mark.parametrize("disposition", ["clarify", "reject"])
def test_non_generate_jobspec_forbids_executable_payload(disposition: str) -> None:
    with pytest.raises(ValidationError, match="cannot carry an executable payload"):
        JobSpec(
            request_id="not-generated",
            request_fingerprint="0" * 64,
            nl_request="Ambiguous request",
            disposition=disposition,
            decision_reason="NEEDS_SCOPE",
            logical_plan={"template_id": "must-not-run"},
            planner_metadata={
                "generator_id": "b0",
                "catalog_version": "phase1-b0-frozen",
            },
        )


def test_jobspec_success_is_semantic_and_static_only() -> None:
    success = SuccessAssessment(jobspec=_semantic_success())
    failure = SuccessAssessment(
        jobspec=_semantic_success(correct_bindings=False),
    )

    assert success.jobspec_success is True
    assert success.runtime_success is None
    assert success.end_to_end_success is None
    assert failure.jobspec_success is False


@pytest.mark.parametrize(
    ("admitted", "status", "expected"),
    [
        (True, ExecutionStatus.COMPLETED, True),
        (False, ExecutionStatus.NOT_RUN, False),
        (True, ExecutionStatus.FAILED, False),
        (True, ExecutionStatus.RUNNING, None),
    ],
)
def test_runtime_success_requires_admission_and_terminal_success(
    admitted: bool,
    status: ExecutionStatus,
    expected: bool | None,
) -> None:
    assessment = SuccessAssessment(
        jobspec=_semantic_success(),
        runtime=RuntimeAssessment(
            submission_accepted=admitted,
            execution_status=status,
        ),
    )
    assert assessment.runtime_success is expected


@pytest.mark.parametrize(
    ("admitted", "status"),
    [
        (False, ExecutionStatus.COMPLETED),
        (True, ExecutionStatus.NOT_RUN),
    ],
)
def test_runtime_assessment_rejects_impossible_state_combinations(
    admitted: bool,
    status: ExecutionStatus,
) -> None:
    with pytest.raises(ValidationError, match="submission"):
        RuntimeAssessment(
            submission_accepted=admitted,
            execution_status=status,
        )


def test_end_to_end_requires_all_three_layers() -> None:
    complete = SuccessAssessment(
        jobspec=_semantic_success(),
        runtime=RuntimeAssessment(
            submission_accepted=True,
            execution_status=ExecutionStatus.COMPLETED,
        ),
        observed_output_contract=OutputContractStatus.PASS,
    )
    bad_output = complete.model_copy(
        update={"observed_output_contract": OutputContractStatus.FAIL}
    )
    bad_semantics = complete.model_copy(
        update={"jobspec": _semantic_success(correct_logical_plan=False)}
    )

    assert complete.jobspec_success is True
    assert complete.runtime_success is True
    assert complete.end_to_end_success is True
    assert bad_output.end_to_end_success is False
    assert bad_semantics.runtime_success is True
    assert bad_semantics.end_to_end_success is False


def test_runtime_failure_makes_end_to_end_false_without_output() -> None:
    failed = SuccessAssessment(
        jobspec=_semantic_success(),
        runtime=RuntimeAssessment(
            submission_accepted=True,
            execution_status=ExecutionStatus.FAILED,
        ),
    )
    assert failed.runtime_success is False
    assert failed.end_to_end_success is False


def test_execution_outcome_mode_cannot_overclaim_runtime_evidence() -> None:
    semantic = SuccessAssessment(jobspec=_semantic_success())
    with pytest.raises(ValidationError, match="requires a runtime assessment"):
        ExecutionOutcome(
            mode="live_execution",
            job_spec_ref="jobspec:sha256:missing-runtime",
            success=semantic,
        )

    with pytest.raises(ValidationError, match="cannot claim RuntimeSuccess"):
        ExecutionOutcome(
            mode="offline_gold",
            job_spec_ref="jobspec:sha256:not-live",
            success=SuccessAssessment(
                jobspec=_semantic_success(),
                runtime=RuntimeAssessment(
                    submission_accepted=True,
                    execution_status=ExecutionStatus.COMPLETED,
                ),
            ),
        )
