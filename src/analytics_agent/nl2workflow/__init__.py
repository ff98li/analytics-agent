"""nl2workflow — turn a natural-language analytics request into a workflow DAG.

Phase 1 baseline: deterministic, rule-based mapping (intent + symbol detection)
over canonical Lumilake-format DAG templates. Later phases layer memory and
LLM-driven generation on top of this plug point and measure the improvement
curve against the ground-truth set in ``nl-requests/ground_truth.json``.
"""

from typing import Any

from .baseline import generate_workflow, workflow_to_yaml

__all__ = ["nl2workflow", "generate_workflow", "workflow_to_yaml"]


def nl2workflow(nl_request: str, context: dict[str, Any] | None = None) -> dict:
    """Project plug-point: NL request -> runnable Lumilake workflow DAG.

    Phase 2 planning is canonical internally.  This function remains the
    DAG-returning compatibility facade and therefore deliberately discards the
    rest of the :class:`~analytics_agent.models.JobSpec`.

    Context was entirely ignored in Phase 1.  Preserve that behavior by
    carrying the mapping only as inert ``PlanningContext.extra`` data.  Phase 2
    experiments call ``plan()`` directly with a typed context.
    """

    # Import lazily: ``analytics_agent.api`` imports the baseline generator
    # while constructing the complete JobSpec contract.
    from analytics_agent.api import PlanningContext, plan

    if context is None:
        planning_context = None
    else:
        planning_context = PlanningContext(extra=dict(context))

    return plan(nl_request, planning_context).workflow
