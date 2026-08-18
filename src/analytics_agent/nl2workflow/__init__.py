"""nl2workflow — turn a natural-language analytics request into a workflow DAG.

Phase 1 baseline: deterministic, rule-based mapping (intent + symbol detection)
over canonical Lumilake-format DAG templates. Later phases layer memory and
LLM-driven generation on top of this plug point and measure the improvement
curve against the ground-truth set in ``nl-requests/ground_truth.json``.
"""

from .baseline import generate_workflow, workflow_to_yaml

__all__ = ["nl2workflow", "generate_workflow", "workflow_to_yaml"]


def nl2workflow(nl_request: str, context: dict | None = None) -> dict:
    """Project plug-point: NL request -> runnable Lumilake workflow DAG.

    ``context`` is reserved for memory/inference-seam context in later phases;
    the Phase-1 baseline ignores it.
    """
    return generate_workflow(nl_request, context=context)
