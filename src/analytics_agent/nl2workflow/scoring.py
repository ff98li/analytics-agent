"""Structural scoring of generated workflow DAGs against ground truth.

Phase-1 rubric (documented in ``nl-requests/ground_truth.json``): a workflow's
structure is its op set ``{(op_id, op_type)}``, its dependency edges
``{(src_id, dst_id)}`` (including ``outputs.ref -> output`` edges), and its
output declarations. Accuracy = 0.5 * op-set Jaccard + 0.5 * edge-set Jaccard;
``exact`` flags a perfect structural match.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def extract_structure(
    workflow: dict[str, Any],
) -> tuple[set[tuple[str, str]], set[tuple[str, str]], set[tuple[str, str]]]:
    """Return ``(ops, edges, outputs)`` for a workflow DAG dict."""
    ops: set[tuple[str, str]] = set()
    edges: set[tuple[str, str]] = set()
    for op in workflow.get("ops", []):
        ops.add((op["id"], op["op"]))
        for src in op.get("inputs", []):
            edges.add((src, op["id"]))
    outputs: set[tuple[str, str]] = set()
    for out in workflow.get("outputs", []):
        outputs.add((out["ref"], out["name"]))
        edges.add((out["ref"], f"out:{out['name']}"))
    return ops, edges, outputs


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def score_structure(
    generated: dict[str, Any], ground_truth: dict[str, Any]
) -> dict[str, Any]:
    """Score a generated DAG against a ground-truth structure entry."""
    g_ops, g_edges, g_out = extract_structure(generated)
    t_ops, t_edges, t_out = extract_structure(ground_truth)
    ops_j = _jaccard(g_ops, t_ops)
    edges_j = _jaccard(g_edges, t_edges)
    outputs_ok = g_out == t_out
    exact = g_ops == t_ops and g_edges == t_edges and outputs_ok
    score = 0.5 * ops_j + 0.5 * edges_j
    if not outputs_ok:
        score *= 0.9  # output mismatches are penalized but not fatal
    return {
        "ops_jaccard": round(ops_j, 4),
        "edges_jaccard": round(edges_j, 4),
        "outputs_match": outputs_ok,
        "exact": exact,
        "score": round(score, 4),
    }


def evaluate_set(
    ground_truth: list[dict[str, Any]],
    generator: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    """Score a generator over the whole ground-truth request set."""
    per_request: list[dict[str, Any]] = []
    for entry in ground_truth:
        generated = generator(entry["request"])
        result = score_structure(generated, entry["workflow"])
        per_request.append(
            {
                "id": entry["id"],
                "request": entry["request"],
                "intent": entry.get("intent"),
                **result,
            }
        )
    mean_score = (
        round(sum(r["score"] for r in per_request) / len(per_request), 4)
        if per_request
        else 0.0
    )
    exact_count = sum(1 for r in per_request if r["exact"])
    return {
        "requests_scored": len(per_request),
        "exact_matches": exact_count,
        "mean_score": mean_score,
        "per_request": per_request,
    }
