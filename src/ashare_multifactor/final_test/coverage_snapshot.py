"""Attempt-bound immutable snapshots of official final-test evidence."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

from ashare_multifactor.audit.records import file_record, sha256_file, verify_file_record
from ashare_multifactor.final_test.execution_sources import (
    validate_final_execution_coverages,
)
from ashare_multifactor.final_test.preparation import FinalTestPreparation
from ashare_multifactor.final_test.registry import validate_publication_id
from ashare_multifactor.final_test.resume import load_bound_symbol_scope


@dataclass(frozen=True)
class ExecutionCoverageSnapshot:
    root: Path
    manifest_path: Path
    manifest_sha256: str
    security_event_coverage_path: Path
    corporate_action_coverage_root: Path
    symbols: tuple[str, ...]
    prepare_manifest_sha256: str
    security_event_coverage_sha256: str
    corporate_action_coverage_sha256: str


def snapshot_execution_coverages(
    final_root: Path,
    *,
    attempt_id: str,
    preparation: FinalTestPreparation,
    security_event_coverage_path: Path,
    corporate_action_coverage_root: Path,
    expected_security_sha256: str,
    expected_corporate_sha256: str,
) -> ExecutionCoverageSnapshot:
    """Copy, atomically publish, and revalidate exact validator dependencies."""
    validate_publication_id(attempt_id)
    if preparation.attempt_id != attempt_id:
        raise ValueError("coverage snapshot attempt differs from preparation")
    symbols = load_bound_symbol_scope(preparation)
    parent = final_root / "execution_coverage_snapshots"
    destination = parent / attempt_id
    if parent.is_symlink() or destination.is_symlink():
        raise ValueError("execution coverage snapshot path uses a symlink")
    if destination.exists():
        return _verify_snapshot(
            destination,
            attempt_id=attempt_id,
            preparation=preparation,
            symbols=symbols,
            expected_security_sha256=expected_security_sha256,
            expected_corporate_sha256=expected_corporate_sha256,
        )

    security, corporate = validate_final_execution_coverages(
        symbols=symbols,
        security_event_coverage_path=security_event_coverage_path,
        corporate_action_coverage_root=corporate_action_coverage_root,
    )
    parent.mkdir(parents=True, exist_ok=True)
    temporary = parent / f".{attempt_id}.{uuid4().hex}.tmp"
    temporary.mkdir()
    try:
        security_root = _required_root(security, "security coverage")
        corporate_root = _required_root(corporate, "corporate coverage")
        security_files = _security_dependency_paths(security)
        corporate_files = _corporate_dependency_paths(corporate)
        _copy_dependencies(security_files, root=security_root, destination=temporary / "security")
        _copy_dependencies(
            corporate_files,
            root=corporate_root,
            destination=temporary / "corporate",
        )
        snapshot_security = temporary / "security" / _relative_manifest(
            security, security_root
        )
        records = [
            file_record(path, root=temporary, role="official_coverage_snapshot").to_dict()
            for path in sorted(item for item in temporary.rglob("*") if item.is_file())
        ]
        _write_json(
            temporary / "snapshot_manifest.json",
            {
                "attempt_id": attempt_id,
                "prepare_manifest_sha256": preparation.manifest_sha256,
                "security_event_coverage_sha256": expected_security_sha256,
                "corporate_action_coverage_sha256": expected_corporate_sha256,
                "symbols_sha256": hashlib.sha256(
                    ("\n".join(symbols) + "\n").encode()
                ).hexdigest(),
                "symbol_count": len(symbols),
                "security_manifest": str(snapshot_security.relative_to(temporary)),
                "corporate_root": "corporate",
                "files": records,
            },
        )
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("execution coverage snapshot already exists")
        os.rename(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return _verify_snapshot(
        destination,
        attempt_id=attempt_id,
        preparation=preparation,
        symbols=symbols,
        expected_security_sha256=expected_security_sha256,
        expected_corporate_sha256=expected_corporate_sha256,
    )


def _verify_snapshot(
    root: Path,
    *,
    attempt_id: str,
    preparation: FinalTestPreparation,
    symbols: list[str],
    expected_security_sha256: str,
    expected_corporate_sha256: str,
) -> ExecutionCoverageSnapshot:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("execution coverage snapshot is missing or uses a symlink")
    manifest_path = root / "snapshot_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError("invalid execution coverage snapshot manifest") from error
    expected = {
        "attempt_id": attempt_id,
        "prepare_manifest_sha256": preparation.manifest_sha256,
        "security_event_coverage_sha256": expected_security_sha256,
        "corporate_action_coverage_sha256": expected_corporate_sha256,
        "symbols_sha256": hashlib.sha256(("\n".join(symbols) + "\n").encode()).hexdigest(),
        "symbol_count": len(symbols),
    }
    if (
        not isinstance(manifest, dict)
        or any(manifest.get(key) != value for key, value in expected.items())
        or manifest.get("corporate_root") != "corporate"
        or not isinstance(manifest.get("security_manifest"), str)
        or not isinstance(manifest.get("files"), list)
    ):
        raise ValueError("execution coverage snapshot identity differs")
    for record in manifest["files"]:
        verify_file_record(record, root=root)
    security_path = root / str(manifest["security_manifest"])
    corporate_root = root / "corporate"
    security, corporate = validate_final_execution_coverages(
        symbols=symbols,
        security_event_coverage_path=security_path,
        corporate_action_coverage_root=corporate_root,
    )
    current_security = str(security.get("coverage_manifest_sha256", ""))
    current_corporate = str(corporate.get("coverage_manifest_sha256", ""))
    if (
        current_security != expected_security_sha256
        or current_corporate != expected_corporate_sha256
    ):
        raise ValueError("coverage snapshot identity differs from executing claim")
    return ExecutionCoverageSnapshot(
        root=root,
        manifest_path=manifest_path,
        manifest_sha256=sha256_file(manifest_path),
        security_event_coverage_path=security_path,
        corporate_action_coverage_root=corporate_root,
        symbols=tuple(symbols),
        prepare_manifest_sha256=preparation.manifest_sha256,
        security_event_coverage_sha256=current_security,
        corporate_action_coverage_sha256=current_corporate,
    )


def _required_root(value: dict[str, object], label: str) -> Path:
    root = value.get("coverage_root")
    if not isinstance(root, Path):
        raise ValueError(f"verified {label} root is invalid")
    return root


def _relative_manifest(value: dict[str, object], root: Path) -> Path:
    path = value.get("coverage_manifest_path")
    if not isinstance(path, Path):
        raise ValueError("verified coverage manifest path is invalid")
    return path.relative_to(root)


def _security_dependency_paths(value: dict[str, object]) -> set[Path]:
    paths = {
        value.get("coverage_manifest_path"),
        *value.get("evidence_paths", []),
        *value.get("coverage_paths", []),
    }
    if isinstance(value.get("events_file"), Path):
        paths.add(value["events_file"])
    return _path_set(paths, "security coverage")


def _corporate_dependency_paths(value: dict[str, object]) -> set[Path]:
    paths = {
        value.get("coverage_manifest_path"),
        value.get("candidate_file"),
        value.get("coverage_file"),
        value.get("evidence_index_file"),
        value.get("official_actions_file"),
        value.get("diff_file"),
        *value.get("evidence_paths", []),
    }
    return _path_set(paths, "corporate coverage")


def _path_set(values: set[object], label: str) -> set[Path]:
    if any(not isinstance(value, Path) for value in values):
        raise ValueError(f"verified {label} dependency is invalid")
    return {value for value in values if isinstance(value, Path)}


def _copy_dependencies(paths: set[Path], *, root: Path, destination: Path) -> None:
    destination.mkdir()
    for source in sorted(paths):
        relative = source.relative_to(root)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if sha256_file(target) != sha256_file(source):
                raise ValueError("coverage snapshot dependency path collision")
            continue
        shutil.copy2(source, target)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
