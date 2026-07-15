from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ValidationPaths:
    processed: Path
    artifacts: Path
    daily_panel: Path


def validation_paths(root: Path) -> ValidationPaths:
    processed = root / "processed/validation_evaluation"
    artifacts = root / "artifacts/validation_evaluation"
    return ValidationPaths(processed, artifacts, processed / "daily_panel")
