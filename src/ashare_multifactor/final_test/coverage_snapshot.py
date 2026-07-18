"""Attempt-bound immutable snapshots of official final-test evidence."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import shutil
from uuid import uuid4

from ashare_multifactor.audit.records import file_record, sha256_file, verify_file_record
from ashare_multifactor.final_test.execution_sources import (
    validate_final_execution_coverages,
)
from ashare_multifactor.final_test.execution_binding import _read_stable_tree
from ashare_multifactor.final_test.recovery_secure_fs import opened_directory
from ashare_multifactor.final_test.official_query_coverage import OfficialQueryScope
from ashare_multifactor.final_test.official_query_index import (
    copy_validated_official_query_coverage,
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
        _copy_official_query_coverage(
            security,
            destination=temporary / "security",
            label="security coverage",
        )
        _copy_dependencies(
            corporate_files,
            root=corporate_root,
            destination=temporary / "corporate",
        )
        _copy_official_query_coverage(
            corporate,
            destination=temporary / "corporate",
            label="corporate coverage",
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


def materialize_bound_coverage_snapshot(
    final_root: Path,
    *,
    attempt_id: str,
    destination: Path,
    expected_manifest_sha256: str,
) -> list[dict[str, object]]:
    """Copy one verified attempt snapshot into its immutable execution inputs."""
    validate_publication_id(attempt_id)
    source = final_root / "execution_coverage_snapshots" / attempt_id
    expected_source = (
        final_root.resolve() / "execution_coverage_snapshots" / attempt_id
    )
    if (
        source.is_symlink()
        or source.resolve() != expected_source
        or destination.is_symlink()
        or not destination.parent.resolve().is_relative_to(final_root.resolve())
    ):
        raise ValueError("coverage snapshot materialization path is unsafe")
    with opened_directory(source, label="bound coverage snapshot") as source_fd:
        frozen = _read_stable_tree(source_fd)
    _verify_snapshot_bytes(
        frozen,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    destination.mkdir(exist_ok=True)
    for relative, payload in sorted(frozen.items()):
        target = destination.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_resumable_snapshot_bytes(target, payload)
    with opened_directory(
        destination, label="materialized coverage snapshot"
    ) as destination_fd:
        copied = _read_stable_tree(destination_fd)
    if copied != frozen:
        raise ValueError("coverage snapshot changed during materialization")
    return [
        file_record(
            path,
            root=destination.parent,
            role="official_coverage_snapshot",
        ).to_dict()
        for path in sorted(item for item in destination.rglob("*") if item.is_file())
    ]


def _write_resumable_snapshot_bytes(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise ValueError("coverage snapshot destination uses a symlink")
    if path.exists():
        if not path.is_file():
            raise ValueError("coverage snapshot destination is not a file")
        existing = path.read_bytes()
        if not payload.startswith(existing):
            raise ValueError("coverage snapshot partial bytes differ")
        if existing == payload:
            return
        mode = "ab"
        remainder = payload[len(existing) :]
    else:
        mode = "xb"
        remainder = payload
    with path.open(mode) as stream:
        stream.write(remainder)
        stream.flush()
        os.fsync(stream.fileno())


def _verify_snapshot_bytes(
    files: dict[str, bytes],
    *,
    expected_manifest_sha256: str,
) -> None:
    manifest_bytes = files.get("snapshot_manifest.json")
    if (
        manifest_bytes is None
        or hashlib.sha256(manifest_bytes).hexdigest()
        != expected_manifest_sha256
    ):
        raise ValueError("coverage snapshot manifest hash differs")
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as error:
        raise ValueError("invalid coverage snapshot manifest") from error
    records = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(records, list):
        raise ValueError("coverage snapshot file inventory is missing")
    recorded: dict[str, dict[str, object]] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ValueError("invalid coverage snapshot file record")
        relative = _safe_snapshot_relative(str(record["path"]))
        path = relative.as_posix()
        if relative.parts[0] not in {"security", "corporate"} or path in recorded:
            raise ValueError("invalid coverage snapshot file record")
        recorded[path] = record
    if set(files) - {"snapshot_manifest.json"} != set(recorded):
        raise ValueError("coverage snapshot file inventory differs")
    for path, record in recorded.items():
        payload = files[path]
        if (
            record.get("size_bytes") != len(payload)
            or record.get("sha256") != hashlib.sha256(payload).hexdigest()
        ):
            raise ValueError("coverage snapshot file digest differs")


def _verify_snapshot_tree(
    root: Path,
    *,
    expected_manifest_sha256: str,
) -> None:
    items = list(root.rglob("*")) if root.is_dir() else []
    if root.is_symlink() or not root.is_dir() or any(path.is_symlink() for path in items):
        raise ValueError("coverage snapshot tree is missing or uses a symlink")
    manifest_path = root / "snapshot_manifest.json"
    if (
        not manifest_path.is_file()
        or sha256_file(manifest_path) != expected_manifest_sha256
    ):
        raise ValueError("coverage snapshot manifest hash differs")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid coverage snapshot manifest") from error
    records = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(records, list):
        raise ValueError("coverage snapshot file inventory is missing")
    recorded_paths: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ValueError("invalid coverage snapshot file record")
        relative = _safe_snapshot_relative(str(record["path"]))
        if relative.parts[0] not in {"security", "corporate"}:
            raise ValueError("coverage snapshot file escapes evidence roots")
        if relative.as_posix() in recorded_paths:
            raise ValueError("duplicate coverage snapshot file record")
        recorded_paths.add(relative.as_posix())
        verify_file_record(record, root=root)
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in items
        if path.is_file() and path != manifest_path
    }
    if actual_paths != recorded_paths:
        raise ValueError("coverage snapshot file inventory differs")


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
    if manifest_path.is_symlink():
        raise ValueError("execution coverage snapshot manifest uses a symlink")
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
    manifest_keys = {
        *expected,
        "security_manifest",
        "corporate_root",
        "files",
    }
    if (
        not isinstance(manifest, dict)
        or set(manifest) != manifest_keys
        or any(manifest.get(key) != value for key, value in expected.items())
        or manifest.get("corporate_root") != "corporate"
        or not isinstance(manifest.get("security_manifest"), str)
        or not isinstance(manifest.get("files"), list)
    ):
        raise ValueError("execution coverage snapshot identity differs")
    recorded_paths: set[str] = set()
    for record in manifest["files"]:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ValueError("invalid execution coverage snapshot file record")
        relative = _safe_snapshot_relative(str(record["path"]))
        if relative.parts[0] not in {"security", "corporate"}:
            raise ValueError("coverage snapshot file escapes evidence roots")
        if relative.as_posix() in recorded_paths:
            raise ValueError("duplicate execution coverage snapshot file record")
        recorded_paths.add(relative.as_posix())
        verify_file_record(record, root=root)
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path != manifest_path
    }
    if actual_paths != recorded_paths:
        raise ValueError("execution coverage snapshot file inventory differs")
    security_relative = _safe_snapshot_relative(str(manifest["security_manifest"]))
    if (
        security_relative.parts[0] != "security"
        or security_relative.as_posix() not in recorded_paths
    ):
        raise ValueError("security coverage snapshot manifest is unsafe or unrecorded")
    security_path = (root / security_relative).resolve()
    corporate_root = root / "corporate"
    if (
        security_path.parent == root.resolve()
        or not security_path.is_relative_to((root / "security").resolve())
        or not corporate_root.resolve().is_relative_to(root.resolve())
    ):
        raise ValueError("coverage snapshot path escapes evidence roots")
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


def _safe_snapshot_relative(value: str) -> PurePosixPath:
    relative = PurePosixPath(value)
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ValueError("coverage snapshot path is not a canonical relative path")
    return relative


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


def _copy_official_query_coverage(
    value: dict[str, object],
    *,
    destination: Path,
    label: str,
) -> None:
    index = value.get("official_query_coverage_file")
    scopes = value.get("official_query_scopes")
    if (
        not isinstance(index, Path)
        or not isinstance(scopes, tuple)
        or any(not isinstance(scope, OfficialQueryScope) for scope in scopes)
    ):
        raise ValueError(f"verified {label} official query coverage is invalid")
    copy_validated_official_query_coverage(
        index,
        expected_scopes=scopes,
        destination_root=destination,
    )


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
