"""Create a NEW offline adaptation bundle; never submit or overwrite artifacts."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from analytics_agent.demo_intake import DEFAULT_START_DATE, DemoIntakeError, adapt_workflow


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--ticker", required=True, choices=("AAPL", "NVDA"))
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--output-directory", type=Path, required=True,
                        help="A new directory; existing paths, including symlinks, are refused")
    args = parser.parse_args(argv)
    try:
        with args.original.open("rb") as source:
            raw = source.read(53_710)
        adapted = adapt_workflow(raw, ticker=args.ticker, start_date=args.start_date)
        # mkdir is an atomic no-clobber check, including dangling symlinks.
        # On any subsequent failure retain the new partial bundle for inspection.
        args.output_directory.mkdir(mode=0o700, parents=False, exist_ok=False)
        for name, data in (("workflow.json", adapted.workflow_json),
                           ("manifest.json", adapted.manifest_json),
                           ("parser-payload.json", adapted.parser_payload_json)):
            fd = os.open(args.output_directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(data)
    except (OSError, DemoIntakeError) as exc:
        code = exc.code if isinstance(exc, DemoIntakeError) else type(exc).__name__
        parser.exit(1, f"Refused/failed: {code}; no existing file overwritten; any new partial bundle retained.\n")
    print("Created offline workflow/manifest/parser-payload bundle; runtime remains blocked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
