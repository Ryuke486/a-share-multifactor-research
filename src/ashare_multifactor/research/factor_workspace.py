"""Isolated whole-root workspaces for atomic factor stage publication."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
from uuid import uuid4

from ashare_multifactor.research.factor_outputs import (
    FACTOR_OUTPUT_NAMES,
    replace_directory,
)


def cleanup_orphan_stage_roots(factor_root: Path) -> None:
    pattern = f".{factor_root.name}-*.stage.tmp"
    for path in factor_root.parent.glob(pattern):
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)


def clone_stage_root(factor_root: Path, stage: str) -> Path:
    retained = {
        "factors": ("daily_panel", "data_readiness.json", "lineage.json"),
        "evaluate": (
            "daily_panel",
            "data_readiness.json",
            *FACTOR_OUTPUT_NAMES,
            "lineage.json",
        ),
    }
    try:
        names = retained[stage]
    except KeyError as error:
        raise ValueError(f"unsupported staged factor stage: {stage}") from error
    staging = factor_root.parent / f".{factor_root.name}-{uuid4().hex}.stage.tmp"
    staging.mkdir()
    try:
        for name in names:
            source = factor_root / name
            target = staging / name
            if source.is_dir():
                shutil.copytree(source, target, copy_function=os.link)
            elif source.is_file():
                os.link(source, target)
            else:
                raise ValueError(f"factor stage upstream is missing: {name}")
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


def publish_stage_root(staging: Path, factor_root: Path) -> None:
    replace_directory(staging, factor_root)


def discard_stage_root(staging: Path) -> None:
    shutil.rmtree(staging, ignore_errors=True)
