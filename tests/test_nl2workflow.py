"""Tests for the Phase-1 nl2workflow baseline and its scorer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from analytics_agent.nl2workflow import nl2workflow, workflow_to_yaml
from analytics_agent.nl2workflow.baseline import classify_intent, extract_symbols
from analytics_agent.nl2workflow.scoring import evaluate_set, score_structure

GROUND_TRUTH_PATH = (
    Path(__file__).resolve().parents[1] / "nl-requests" / "ground_truth.json"
)


def load_ground_truth() -> list[dict]:
    with open(GROUND_TRUTH_PATH, encoding="utf-8") as f:
        return json.load(f)["requests"]


GROUND_TRUTH = load_ground_truth()
REQUESTS = {entry["id"]: entry for entry in GROUND_TRUTH}


def test_extract_symbols() -> None:
    assert extract_symbols("Give me Apple's company profile.") == ["AAPL"]
    assert extract_symbols("What about NVDA?") == ["NVDA"]
    assert extract_symbols("Nvidia and Apple both matter") == ["NVDA", "AAPL"]
    assert extract_symbols("AAPL AAPL NVDA") == ["AAPL", "NVDA"]
    assert extract_symbols("no symbols here") == []


def test_classify_intent_for_every_request() -> None:
    for entry in GROUND_TRUTH:
        assert classify_intent(entry["request"]) == entry["intent"], entry["id"]


def test_baseline_matches_ground_truth_exactly() -> None:
    for entry in GROUND_TRUTH:
        generated = nl2workflow(entry["request"])
        result = score_structure(generated, entry["workflow"])
        assert result["exact"] is True, (
            f"{entry['id']}: {result} generated={generated!r} "
            f"gt={entry['workflow']!r}"
        )
        assert result["score"] == 1.0


def test_scorer_penalizes_missing_edge() -> None:
    entry = REQUESTS["r-trading-a"]
    generated = nl2workflow(entry["request"])
    # Drop one dependency edge: Bull Rebuttal no longer reads Bear Researcher.
    for op in generated["ops"]:
        if op["id"] == "Bull Rebuttal":
            op["inputs"] = [src for src in op["inputs"] if src != "Bear Researcher"]
    result = score_structure(generated, entry["workflow"])
    assert result["exact"] is False
    assert result["score"] < 1.0


def test_scorer_penalizes_wrong_op_type() -> None:
    entry = REQUESTS["r-profile-a"]
    generated = nl2workflow(entry["request"])
    generated["ops"][0]["op"] = "LLMChatOp"
    result = score_structure(generated, entry["workflow"])
    assert result["exact"] is False
    assert result["ops_jaccard"] < 1.0


def test_yaml_roundtrip_preserves_structure() -> None:
    generated = nl2workflow(REQUESTS["r-trading-a"]["request"])
    text = workflow_to_yaml(generated)
    reparsed = yaml.safe_load(text)
    assert reparsed == generated


def test_evaluate_set_overall_score_is_one() -> None:
    report = evaluate_set(GROUND_TRUTH, nl2workflow)
    assert report["requests_scored"] == len(GROUND_TRUTH)
    assert report["exact_matches"] == len(GROUND_TRUTH)
    assert report["mean_score"] == 1.0
