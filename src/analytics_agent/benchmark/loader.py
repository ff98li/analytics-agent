"""File loading helpers for versioned benchmark manifests."""

from __future__ import annotations

from pathlib import Path

from .contamination import (
    ContaminationReport,
    assert_no_hard_contamination,
    scan_contamination,
)
from .models import BenchmarkManifest


def load_manifest(path: str | Path) -> BenchmarkManifest:
    """Load and validate one UTF-8 JSON manifest."""

    manifest_path = Path(path)
    return BenchmarkManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )


def load_manifest_with_report(
    path: str | Path,
    *,
    require_clean: bool = True,
) -> tuple[BenchmarkManifest, ContaminationReport]:
    """Load a manifest and run the independent contamination gate."""

    manifest = load_manifest(path)
    report = scan_contamination(manifest.cases)
    if require_clean:
        assert_no_hard_contamination(report)
    return manifest, report
