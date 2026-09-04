"""Phase 2 benchmark manifest and contamination contracts.

The checked-in benchmark starts as an explicitly incomplete draft.  A
manifest may be labelled ``frozen`` only after it satisfies the pre-registered
60/30/60 split and 108/21/21 disposition counts and records reproducible split
hashes.
"""

from .contamination import (
    BenchmarkContaminationError,
    ContaminationFinding,
    ContaminationKind,
    ContaminationReport,
    EntityOverlap,
    assert_no_hard_contamination,
    normalize_request_text,
    scan_contamination,
)
from .loader import load_manifest, load_manifest_with_report
from .models import (
    BenchmarkCase,
    BenchmarkManifest,
    DecisionGold,
    Disposition,
    GenerateGold,
    ManifestStatus,
    Split,
    compute_split_hashes,
)

__all__ = [
    "BenchmarkCase",
    "BenchmarkContaminationError",
    "BenchmarkManifest",
    "ContaminationFinding",
    "ContaminationKind",
    "ContaminationReport",
    "DecisionGold",
    "Disposition",
    "EntityOverlap",
    "GenerateGold",
    "ManifestStatus",
    "Split",
    "assert_no_hard_contamination",
    "compute_split_hashes",
    "load_manifest",
    "load_manifest_with_report",
    "normalize_request_text",
    "scan_contamination",
]
