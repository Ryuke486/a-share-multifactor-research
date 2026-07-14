from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ashare_multifactor.config import ResearchConfig


@dataclass(frozen=True)
class CombinationPaths:
    processed_root: Path
    artifact_root: Path


def combination_paths(config: ResearchConfig) -> CombinationPaths:
    processed = config.paths.processed / "factor_combination"
    artifacts = config.paths.artifacts / "factor_combination"
    raw = (config.paths.raw_unadjusted.resolve(), config.paths.raw_backward_adjusted.resolve())
    for output in (processed.resolve(), artifacts.resolve()):
        if any(output == source or source in output.parents or output in source.parents for source in raw):
            raise ValueError("factor combination output overlaps raw data")
    if processed.resolve() == (config.paths.processed / "factor_research").resolve():
        raise ValueError("factor combination cannot overwrite factor research")
    return CombinationPaths(processed, artifacts)
