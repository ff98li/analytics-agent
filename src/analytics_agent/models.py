"""Versioned Phase 2 contracts shared by planning, execution, and evaluation.

The public ``nl2workflow`` function remains a DAG-returning compatibility
facade.  Phase 2 internals use :class:`JobSpec` so bindings, output policy,
and planner metadata cannot be lost between generation and execution.
"""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """Base contract: reject misspelled or unversioned experimental fields."""

    model_config = ConfigDict(extra="forbid")


class Disposition(str, Enum):
    GENERATE = "generate"
    CLARIFY = "clarify"
    REJECT = "reject"


class ExecutionStatus(str, Enum):
    NOT_RUN = "NOT_RUN"
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


class OutputContractStatus(str, Enum):
    NOT_EVALUATED = "NOT_EVALUATED"
    PASS = "PASS"
    FAIL = "FAIL"


class LogicalPlan(StrictModel):
    template_id: str = Field(min_length=1)
    edit_spec: list[dict[str, Any]] = Field(default_factory=list)


class JobOutput(StrictModel):
    storage_class: Literal["USER_PRIVATE", "PROJECT_PRIVATE"] = "USER_PRIVATE"
    uri: str = Field(pattern=r"^s3://[A-Za-z0-9][A-Za-z0-9.-]*(?:/.*)?$")


class PolicyMetadata(StrictModel):
    policy_version: str = "privacy-v0.2"
    execution_domain: Literal["LOCAL_TRUSTED"] = "LOCAL_TRUSTED"
    forbidden_capabilities: list[str] = Field(
        default_factory=lambda: ["external_network", "shell", "host_path"]
    )


class PlannerMetadata(StrictModel):
    generator_id: str = Field(min_length=1)
    model_artifact: str | None = None
    prompt_version: str | None = None
    catalog_version: str = Field(min_length=1)
    memory_snapshot: str | None = None


class JobSpec(StrictModel):
    """Complete internal planning result for one analytics request."""

    schema_version: Literal["0.2"] = "0.2"
    request_id: str = Field(min_length=1)
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    nl_request: str = Field(min_length=1)
    disposition: Disposition
    decision_reason: str | None = None
    logical_plan: LogicalPlan | None = None
    bindings: dict[str, Any] = Field(default_factory=dict)
    output: JobOutput | None = None
    rendered_workflow: dict[str, Any] | None = None
    policy: PolicyMetadata = Field(default_factory=PolicyMetadata)
    planner_metadata: PlannerMetadata

    @model_validator(mode="after")
    def require_generate_payload(self) -> JobSpec:
        if self.disposition is Disposition.GENERATE:
            required = (self.logical_plan, self.output, self.rendered_workflow)
            if any(value is None for value in required):
                raise ValueError(
                    "generate JobSpec requires logical_plan, output, and "
                    "rendered_workflow"
                )
        else:
            if not self.decision_reason:
                raise ValueError("clarify/reject JobSpec requires decision_reason")
            forbidden = (self.logical_plan, self.output, self.rendered_workflow)
            if any(value is not None for value in forbidden):
                raise ValueError(
                    "clarify/reject JobSpec cannot carry an executable payload"
                )
            if self.bindings:
                raise ValueError("clarify/reject JobSpec cannot carry bindings")
        return self

    @property
    def workflow(self) -> dict[str, Any]:
        """Return the DAG exposed by the legacy ``nl2workflow`` facade."""

        if self.rendered_workflow is None:
            raise ValueError(f"{self.disposition.value} JobSpec has no workflow DAG")
        return self.rendered_workflow


class JobSpecAssessment(StrictModel):
    """Semantic/static correctness components for ``JobSpecSuccess``."""

    correct_disposition: bool
    correct_logical_plan: bool
    correct_rendered_workflow: bool
    correct_bindings: bool
    correct_planned_output_spec: bool
    correct_policy: bool
    static_validation_pass: bool

    @property
    def success(self) -> bool:
        return all(
            (
                self.correct_disposition,
                self.correct_logical_plan,
                self.correct_rendered_workflow,
                self.correct_bindings,
                self.correct_planned_output_spec,
                self.correct_policy,
                self.static_validation_pass,
            )
        )


class RuntimeAssessment(StrictModel):
    """Admission and terminal-state components for ``RuntimeSuccess``."""

    submission_accepted: bool
    execution_status: ExecutionStatus

    @model_validator(mode="after")
    def require_consistent_admission_state(self) -> RuntimeAssessment:
        if (
            not self.submission_accepted
            and self.execution_status is not ExecutionStatus.NOT_RUN
        ):
            raise ValueError("a rejected submission must have execution_status=NOT_RUN")
        if (
            self.submission_accepted
            and self.execution_status is ExecutionStatus.NOT_RUN
        ):
            raise ValueError("an accepted submission cannot have execution_status=NOT_RUN")
        return self

    @property
    def success(self) -> bool | None:
        if self.execution_status in {ExecutionStatus.PENDING, ExecutionStatus.RUNNING}:
            return None
        return self.submission_accepted and self.execution_status is ExecutionStatus.COMPLETED


class SuccessAssessment(StrictModel):
    """Keep semantic, runtime, and end-to-end outcomes unambiguous."""

    jobspec: JobSpecAssessment
    runtime: RuntimeAssessment | None = None
    observed_output_contract: OutputContractStatus = OutputContractStatus.NOT_EVALUATED

    @property
    def jobspec_success(self) -> bool:
        return self.jobspec.success

    @property
    def runtime_success(self) -> bool | None:
        return None if self.runtime is None else self.runtime.success

    @property
    def end_to_end_success(self) -> bool | None:
        if self.runtime is None or self.runtime_success is None:
            return None
        if not self.jobspec_success or not self.runtime_success:
            return False
        if self.observed_output_contract is OutputContractStatus.NOT_EVALUATED:
            return None
        return self.observed_output_contract is OutputContractStatus.PASS


class ExecutionOutcome(StrictModel):
    schema_version: Literal["0.2"] = "0.2"
    mode: Literal["offline_gold", "parser_preview", "live_execution"]
    job_spec_ref: str = Field(min_length=1)
    success: SuccessAssessment
    latency_ms: int | None = Field(default=None, ge=0)
    lumilake_job_id: str | None = None
    flowmesh_workflow_refs: list[str] = Field(default_factory=list)
    provenance_ref: str | None = None
    error_taxonomy: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def match_evaluation_mode(self) -> ExecutionOutcome:
        if self.mode == "live_execution":
            if self.success.runtime is None:
                raise ValueError("live_execution requires a runtime assessment")
        elif self.success.runtime is not None:
            raise ValueError(
                "offline_gold/parser_preview cannot claim RuntimeSuccess"
            )
        return self


def request_fingerprint(nl_request: str) -> str:
    """Return the stable request identity used across independently named runs."""

    return hashlib.sha256(nl_request.encode("utf-8")).hexdigest()
