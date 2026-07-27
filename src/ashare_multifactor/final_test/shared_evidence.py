"""Resolve immutable evidence shared by sibling execution coverages."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from ashare_multifactor.audit.records import sha256_file


EXECUTION_COVERAGES_DIRECTORY = "official_execution_coverages"


def verify_shared_evidence_record(
    record: dict[str, object],
    *,
    coverage_root: Path,
    expected_role: str,
) -> Path:
    """Verify one file relative to the canonical attempt evidence root."""
    if (
        record.get("root") != "attempt_evidence"
        or record.get("role") != expected_role
        or not isinstance(record.get("size_bytes"), int)
    ):
        raise ValueError("shared evidence file record is invalid")
    path = resolve_shared_evidence_path(
        str(record.get("path", "")),
        coverage_root=coverage_root,
        expected_sha256=str(record.get("sha256", "")),
    )
    if path.stat().st_size != record["size_bytes"]:
        raise ValueError("shared evidence file size changed")
    return path


def resolve_shared_evidence_path(
    relative_path: str,
    *,
    coverage_root: Path,
    expected_sha256: str,
) -> Path:
    """Resolve a symlink-free file without copying multi-gigabyte evidence."""
    relative = PurePosixPath(relative_path)
    if (
        not isinstance(relative_path, str)
        or relative.is_absolute()
        or not relative.parts
        or ".." in relative.parts
        or len(expected_sha256) != 64
    ):
        raise ValueError("shared evidence path is invalid")
    attempt_root = attempt_evidence_root(coverage_root)
    candidate = attempt_root.joinpath(*relative.parts)
    _assert_no_symlink(candidate, root=attempt_root)
    if not candidate.is_file() or sha256_file(candidate) != expected_sha256:
        raise ValueError("shared evidence hash changed")
    return candidate


def attempt_evidence_root(coverage_root: Path) -> Path:
    """Derive the owning attempt root from the fixed pair-publication layout."""
    absolute = coverage_root.absolute()
    if (
        absolute.name not in {"corporate", "security"}
        or absolute.parent.parent.name != EXECUTION_COVERAGES_DIRECTORY
    ):
        raise ValueError("execution coverage root is not canonical")
    attempt_root = absolute.parent.parent.parent
    _assert_no_symlink(attempt_root, root=attempt_root)
    if not attempt_root.is_dir():
        raise ValueError("attempt evidence root is invalid")
    return attempt_root


def _assert_no_symlink(path: Path, *, root: Path) -> None:
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError as error:
        raise ValueError("shared evidence path escapes attempt root") from error
    current = root.absolute()
    for ancestor in (current, *current.parents):
        if ancestor.is_symlink():
            raise ValueError("shared evidence root uses a symlink")
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("shared evidence path uses a symlink")
