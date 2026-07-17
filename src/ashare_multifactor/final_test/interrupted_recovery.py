"""Crash-safe archival of one execution and all of its bound input directories."""

from __future__ import annotations

import ctypes
import errno
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Mapping
from uuid import uuid4

from ashare_multifactor.audit.records import file_record, sha256_file, verify_file_record
from ashare_multifactor.final_test.registry import (
    begin_execution_recovery,
    complete_execution_recovery,
    pending_execution_recovery,
    validate_publication_id,
)
from ashare_multifactor.final_test.resume import ResumePreflight


_IDENTITY_KEYS = {
    "execution_id",
    "attempt_id",
    "sealed_protocol_sha256",
    "prepare_manifest_sha256",
    "security_event_coverage_sha256",
    "corporate_action_coverage_sha256",
    "coverage_snapshot_manifest_sha256",
}


def recover_interrupted_execution(
    final_root: Path,
    *,
    preflight: ResumePreflight,
    coverage_snapshot_manifest_sha256: str,
) -> Path | None:
    """Archive all directories bound to one interrupted execution, then audit it."""
    attempt_id = preflight.authorization.attempt_id
    registry_root = final_root / "attempts"
    sources = _execution_artifact_sources(final_root, attempt_id=attempt_id)
    intent = pending_execution_recovery(registry_root, attempt_id)
    recovery_root = (
        final_root / str(intent["recovery_root"])
        if intent is not None
        else None
    )
    identity_root = sources["attempt_run"]
    if intent is not None and not identity_root.exists():
        identity_root = recovery_root / "archive" / "attempt_run"
    elif intent is None and not identity_root.exists() and not identity_root.is_symlink():
        return None
    identity = _load_execution_identity(identity_root)
    expected_identity = _expected_execution_identity(
        preflight,
        execution_id=identity.get("execution_id"),
        coverage_snapshot_manifest_sha256=coverage_snapshot_manifest_sha256,
    )
    if identity != expected_identity:
        raise ValueError("partial final-test execution identity schema or values differ")
    execution_id = str(identity["execution_id"])
    presence = (
        dict(intent["artifact_presence"])
        if intent is not None
        else {key: path.exists() for key, path in sources.items()}
    )
    _verify_bound_artifacts(
        sources,
        presence=presence,
        execution_id=execution_id,
        attempt_id=attempt_id,
        allow_moved=intent is not None,
    )
    identities = {
        "prepare_manifest_sha256": preflight.preparation.manifest_sha256,
        "security_event_coverage_sha256": preflight.security_event_coverage_sha256,
        "corporate_action_coverage_sha256": preflight.corporate_action_coverage_sha256,
    }
    if intent is None:
        intent = begin_execution_recovery(
            registry_root,
            attempt_id=attempt_id,
            execution_id=execution_id,
            identities=identities,
            artifact_presence=presence,
        )
        recovery_root = final_root / str(intent["recovery_root"])
    if (
        intent.get("execution_id") != execution_id
        or intent.get("identities") != identities
        or intent.get("artifact_presence") != presence
    ):
        raise ValueError("pending execution recovery identity differs")
    if recovery_root is None:
        raise ValueError("interrupted recovery root is missing")
    _resolve_or_publish_claim(recovery_root, intent=intent)
    archive = recovery_root / "archive"
    archive.mkdir(exist_ok=True)
    for label, source in sources.items():
        if not presence[label]:
            continue
        target = archive / label
        if source.exists() and target.exists():
            raise ValueError("interrupted archive target already contains data")
        if source.exists():
            _atomic_rename_no_replace(source, target)
        elif not target.is_dir() or target.is_symlink():
            raise ValueError("interrupted execution archive is missing after recovery intent")
    archived_sources = {label: archive / label for label in sources}
    if _load_execution_identity(archived_sources["attempt_run"]) != identity:
        raise ValueError("interrupted execution archive identity changed")
    _verify_bound_artifacts(
        archived_sources,
        presence=presence,
        execution_id=execution_id,
        attempt_id=attempt_id,
        allow_moved=False,
    )
    archive_manifest = _write_or_verify_archive_manifest(
        recovery_root,
        intent=intent,
        archived_sources=archived_sources,
        presence=presence,
    )
    complete_execution_recovery(
        registry_root,
        intent=intent,
        archive_manifest_sha256=sha256_file(archive_manifest),
    )
    return archive


def has_recoverable_execution_archive(final_root: Path, *, attempt_id: str) -> bool:
    try:
        intent = pending_execution_recovery(final_root / "attempts", attempt_id)
    except ValueError:
        return False
    if intent is None:
        return False
    partial = final_root / str(intent.get("source_path", ""))
    archive = final_root / str(intent.get("archived_path", "")) / "attempt_run"
    return not partial.exists() and archive.is_dir() and not archive.is_symlink()


def _expected_execution_identity(
    preflight: ResumePreflight,
    *,
    execution_id: object,
    coverage_snapshot_manifest_sha256: str,
) -> dict[str, object]:
    if not isinstance(execution_id, str):
        raise ValueError("partial final-test execution identity is incomplete")
    validate_publication_id(execution_id)
    return {
        "execution_id": execution_id,
        "attempt_id": preflight.authorization.attempt_id,
        "sealed_protocol_sha256": preflight.authorization.sealed_protocol_sha256,
        "prepare_manifest_sha256": preflight.preparation.manifest_sha256,
        "security_event_coverage_sha256": preflight.security_event_coverage_sha256,
        "corporate_action_coverage_sha256": preflight.corporate_action_coverage_sha256,
        "coverage_snapshot_manifest_sha256": coverage_snapshot_manifest_sha256,
    }


def _execution_artifact_sources(
    final_root: Path, *, attempt_id: str
) -> dict[str, Path]:
    return {
        "attempt_run": final_root / "attempt_runs" / attempt_id,
        "execution_input_sources": final_root / "execution_input_sources",
        "attempt_inputs": final_root / "attempt_inputs" / attempt_id,
    }


def _verify_bound_artifacts(
    roots: Mapping[str, Path],
    *,
    presence: Mapping[str, bool],
    execution_id: str,
    attempt_id: str,
    allow_moved: bool,
) -> None:
    for label, root in roots.items():
        if not presence[label]:
            if root.exists() and not allow_moved:
                raise ValueError("unexpected interrupted execution artifact")
            continue
        if not root.exists():
            if allow_moved:
                continue
            raise ValueError("interrupted execution artifact is missing")
        if root.is_symlink() or not root.is_dir():
            raise ValueError("interrupted execution artifact uses a symlink")
        manifest_name = {
            "attempt_run": "execution_identity.json",
            "execution_input_sources": "source_manifest.json",
            "attempt_inputs": "manifest.json",
        }[label]
        payload = _read_json(root / manifest_name, label=f"{label} identity")
        if payload.get("execution_id") != execution_id:
            raise ValueError("interrupted execution artifact identity differs")
        if label != "attempt_inputs" and payload.get("attempt_id") != attempt_id:
            raise ValueError("interrupted execution artifact attempt differs")


def _resolve_or_publish_claim(recovery_root: Path, *, intent: dict[str, object]) -> None:
    claim = recovery_root / ".intent-claim.json"
    if recovery_root.exists() or recovery_root.is_symlink():
        if recovery_root.is_symlink() or not recovery_root.is_dir():
            raise FileExistsError("recovery claim target already exists")
        try:
            payload = json.loads(claim.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as error:
            raise FileExistsError("recovery claim target already exists") from error
        if payload != intent:
            raise ValueError("interrupted archive target claim differs")
        return
    recovery_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = recovery_root.parent / f".{recovery_root.name}.{uuid4().hex}.tmp"
    temporary.mkdir()
    try:
        _write_json_exclusive(temporary / ".intent-claim.json", intent)
        _atomic_rename_no_replace(temporary, recovery_root)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _atomic_rename_no_replace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform == "darwin":
        result = libc.renamex_np(source_bytes, destination_bytes, ctypes.c_uint(0x00000004))
    elif sys.platform.startswith("linux"):
        result = libc.renameat2(
            ctypes.c_int(-100),
            source_bytes,
            ctypes.c_int(-100),
            destination_bytes,
            ctypes.c_uint(1),
        )
    else:
        raise RuntimeError("atomic no-replace directory publication is unsupported")
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError("recovery claim target already exists")
    raise OSError(error_number, os.strerror(error_number), str(destination))


def _write_or_verify_archive_manifest(
    recovery_root: Path,
    *,
    intent: Mapping[str, object],
    archived_sources: Mapping[str, Path],
    presence: Mapping[str, bool],
) -> Path:
    destination = recovery_root / "archive_manifest.json"
    records = [
        file_record(path, root=recovery_root, role=f"interrupted_{label}").to_dict()
        for label, root in sorted(archived_sources.items())
        if presence[label]
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    ]
    payload = {
        "attempt_id": intent["attempt_id"],
        "recovery_id": intent["recovery_id"],
        "execution_id": intent["execution_id"],
        "files": records,
    }
    if destination.exists():
        existing = _read_json(destination, label="interrupted archive manifest")
        if existing != payload:
            raise ValueError("interrupted archive manifest identity differs")
    else:
        _write_json_exclusive(destination, payload)
    for record in records:
        verify_file_record(record, root=recovery_root)
    return destination


def _load_execution_identity(root: Path) -> dict[str, object]:
    value = _read_json(root / "execution_identity.json", label="execution identity")
    if set(value) != _IDENTITY_KEYS:
        raise ValueError("partial final-test execution identity schema differs")
    return value


def _read_json(path: Path, *, label: str) -> dict[str, object]:
    if path.is_symlink():
        raise ValueError(f"{label} uses a symlink")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is missing or invalid") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} is missing or invalid")
    return value


def _write_json_exclusive(path: Path, payload: Mapping[str, object]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
