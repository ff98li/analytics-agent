#!/usr/bin/env python3
"""Generate Lumilake workflow YAMLs from NL requests (baseline nl2workflow).

Usage: uv run python scripts/gen_nl2workflow_yaml.py <output_dir>
Writes one YAML per request in the ground-truth set plus two demo requests.
"""

from __future__ import annotations

import sys
from pathlib import Path

from analytics_agent.nl2workflow import nl2workflow, workflow_to_yaml

DEMO_REQUESTS = [
    ("gen-profile", "Give me Apple's company profile."),
    ("gen-trading", "Should I buy AAPL right now?"),
]


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("generated-workflows")
    out_dir.mkdir(parents=True, exist_ok=True)
    for stem, request in DEMO_REQUESTS:
        workflow = nl2workflow(request)
        path = out_dir / f"{stem}.yaml"
        path.write_text(workflow_to_yaml(workflow), encoding="utf-8")
        print(f"wrote {path}  ({len(workflow['ops'])} ops)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
