"""Path-boundary validation for isolated factor research outputs."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from ashare_multifactor.config import ResearchConfig


@dataclass(frozen=True)
class FactorResearchPaths:
    processed_root: Path
    artifact_root: Path


def validate_factor_paths(config: ResearchConfig) -> FactorResearchPaths:
    """Reject output aliases and every raw/output directory overlap."""
    logical = {
        "processed": _logical(config.paths.processed),
        "artifacts": _logical(config.paths.artifacts),
        "raw_unadjusted": _logical(config.paths.raw_unadjusted),
        "raw_backward_adjusted": _logical(config.paths.raw_backward_adjusted),
    }
    resolved = {
        "processed": config.paths.processed.resolve(),
        "artifacts": config.paths.artifacts.resolve(),
        "raw_unadjusted": config.paths.raw_unadjusted.resolve(),
        "raw_backward_adjusted": config.paths.raw_backward_adjusted.resolve(),
    }
    output_pairs = (
        ("processed", "artifacts"),
        ("processed", "raw_unadjusted"),
        ("processed", "raw_backward_adjusted"),
        ("artifacts", "raw_unadjusted"),
        ("artifacts", "raw_backward_adjusted"),
    )
    for left, right in output_pairs:
        if _overlap(logical[left], logical[right]) or _overlap(resolved[left], resolved[right]):
            raise ValueError(f"configured {left} path overlaps {right} path")

    factor_root = config.paths.processed / "factor_research"
    artifact_root = config.paths.artifacts / "factor_research"
    _validate_direct_child(config.paths.processed, factor_root, "factor_research")
    _validate_direct_child(config.paths.artifacts, artifact_root, "artifact factor_research")
    _validate_direct_child(factor_root, factor_root / "daily_panel", "daily_panel")
    return FactorResearchPaths(factor_root, artifact_root)


def _validate_direct_child(parent: Path, child: Path, label: str) -> None:
    expected_logical = _logical(parent) / child.name
    if _logical(child) != expected_logical:
        raise ValueError(f"configured {label} path is not a direct child")
    expected_resolved = parent.resolve() / child.name
    if child.resolve() != expected_resolved:
        raise ValueError(f"configured {label} path uses a symlink alias")


def _logical(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents
