"""Isolated whole-root workspaces for atomic factor stage publication."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
from typing import Iterable
from uuid import uuid4

from ashare_multifactor.research.factor_outputs import replace_directory


def cleanup_orphan_stage_roots(factor_root: Path) -> None:
    pattern = f".{factor_root.name}-*.stage.tmp"
    for path in factor_root.parent.glob(pattern):
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)


def clone_stage_root(factor_root: Path, retained_files: Iterable[Path]) -> Path:
    """Hardlink only explicitly retained regular files into a new stage root."""
    root = factor_root.absolute()
    root_status = root.lstat()
    if stat.S_ISLNK(root_status.st_mode):
        raise ValueError(f"retained file path uses symlink: {root}")
    root_resolved = root.resolve(strict=True)
    retained: list[tuple[Path, Path]] = []
    seen: set[str] = set()

    for candidate in retained_files:
        source = candidate.absolute()
        try:
            relative = source.relative_to(root)
        except ValueError as error:
            raise ValueError(f"retained file escapes factor research root: {candidate}") from error
        relative_text = relative.as_posix()
        if (
            not relative.parts
            or relative.is_absolute()
            or ".." in relative.parts
            or "\\" in relative_text
        ):
            raise ValueError(f"invalid retained file path: {candidate}")
        if relative_text in seen:
            raise ValueError(f"duplicate retained file: {relative_text}")
        seen.add(relative_text)

        cursor = root
        try:
            for part in relative.parts:
                cursor /= part
                status = cursor.lstat()
                if stat.S_ISLNK(status.st_mode):
                    raise ValueError(f"retained file path uses symlink: {cursor}")
        except FileNotFoundError as error:
            raise ValueError(f"factor stage upstream is missing: {relative_text}") from error
        if not stat.S_ISREG(status.st_mode):
            raise ValueError(f"retained path is not a regular file: {relative_text}")
        if not source.resolve(strict=True).is_relative_to(root_resolved):
            raise ValueError(f"retained file escapes factor research root: {candidate}")
        retained.append((source, relative))

    staging = factor_root.parent / f".{factor_root.name}-{uuid4().hex}.stage.tmp"
    staging.mkdir()
    try:
        for source, relative in retained:
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            os.link(source, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


def publish_stage_root(staging: Path, factor_root: Path) -> None:
    replace_directory(staging, factor_root)


def discard_stage_root(staging: Path) -> None:
    shutil.rmtree(staging, ignore_errors=True)
