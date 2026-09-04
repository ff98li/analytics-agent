"""Stable public facade and complete internal Phase 2 planning interface."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from pydantic import Field

from analytics_agent.models import (
    Disposition,
    JobOutput,
    JobSpec,
    LogicalPlan,
    PlannerMetadata,
    StrictModel,
    request_fingerprint,
)
from analytics_agent.nl2workflow.baseline import (
    classify_intent,
    extract_symbols,
    generate_workflow,
)


class PlanningContext(StrictModel):
    """Provider-neutral context; external team seams can map into this model."""

    request_id: str | None = None
    generator_id: str = "b0"
    catalog_version: str = "phase1-b0-frozen"
    model_artifact: str | None = None
    prompt_version: str | None = None
    memory_snapshot: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


def _request_id(nl_request: str) -> str:
    """Create a run-unique ID while retaining a recognizable request prefix."""

    digest = hashlib.sha256(nl_request.encode("utf-8")).hexdigest()[:12]
    return f"b0-{digest}-{uuid.uuid4().hex[:12]}"


def plan(
    nl_request: str,
    context: PlanningContext | dict[str, Any] | None = None,
) -> JobSpec:
    """Return a complete Phase 2 ``JobSpec`` using the frozen B0 generator.

    This establishes the internal contract without changing B0 behavior.  B1
    and B2 will provide alternative planners behind the same interface once
    the real local inference G0 contract is available.
    """

    if isinstance(context, dict):
        planning_context = PlanningContext.model_validate(context)
    else:
        planning_context = context or PlanningContext()

    if (
        planning_context.generator_id != "b0"
        or planning_context.catalog_version != "phase1-b0-frozen"
        or planning_context.model_artifact is not None
        or planning_context.prompt_version is not None
        or planning_context.memory_snapshot is not None
    ):
        raise ValueError(
            "the frozen B0 planner cannot claim B1/B2 model, prompt, catalog, "
            "or memory provenance"
        )

    intent = classify_intent(nl_request)
    symbols = extract_symbols(nl_request)
    workflow = generate_workflow(nl_request)
    request_id = planning_context.request_id or _request_id(nl_request)
    # The frozen B0 compare template supplies these defaults when no entity is
    # named.  JobSpec bindings must describe the rendered DAG, not merely the
    # literal mentions extracted from the request.
    bound_symbols = symbols or (["AAPL", "NVDA"] if intent == "compare" else [])

    return JobSpec(
        request_id=request_id,
        request_fingerprint=request_fingerprint(nl_request),
        nl_request=nl_request,
        disposition=Disposition.GENERATE,
        logical_plan=LogicalPlan(template_id=f"project.{intent}-b0"),
        bindings={"symbols": {"kind": "literal_list", "values": bound_symbols}},
        output=JobOutput(
            uri=f"s3://lumilake-private/runs/{request_id}/",
        ),
        rendered_workflow=workflow,
        planner_metadata=PlannerMetadata(
            generator_id="b0",
            model_artifact=None,
            prompt_version=None,
            catalog_version="phase1-b0-frozen",
            memory_snapshot=None,
        ),
    )
