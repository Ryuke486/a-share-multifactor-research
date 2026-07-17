"""Crash-safe archival of one execution and all of its bound input directories."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Iterator, Mapping
from uuid import uuid4

from ashare_multifactor.final_test.registry import (
    begin_execution_recovery,
    complete_execution_recovery,
    pending_execution_recovery,
    validate_publication_id,
)
from ashare_multifactor.final_test.recovery_secure_fs import (
    assert_directory_entry as _assert_directory_entry,
    atomic_rename_no_replace as _atomic_rename_no_replace,
    atomic_rename_no_replace_at as _atomic_rename_no_replace_at,
    directory_identity as _directory_identity,
    directory_records_at as _directory_records_at,
    json_bytes as _json_bytes,
    open_directory_at as _open_directory_at,
    opened_directory as _opened_directory,
    opened_directory_at as _opened_directory_at,
    read_bytes_at as _read_bytes_at,
    read_json_at as _read_json_at,
    write_bytes_exclusive_at as _write_bytes_exclusive_at,
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
    with _open_recovery_archive(recovery_root, intent=intent) as (
        recovery_fd,
        archive_fd,
        archive_identity,
    ):
        for label, source in sources.items():
            if not presence[label]:
                continue
            _assert_archive_anchor(
                recovery_fd,
                archive_fd,
                expected=archive_identity,
            )
            _move_source_directory_no_replace(
                source,
                destination_name=label,
                archive_fd=archive_fd,
            )
            _assert_archive_anchor(
                recovery_fd,
                archive_fd,
                expected=archive_identity,
            )
        archive_manifest_sha256 = _write_or_verify_archive_manifest_at(
            recovery_fd,
            archive_fd,
            intent=intent,
            presence=presence,
        )
        complete_execution_recovery(
            registry_root,
            intent=intent,
            archive_manifest_sha256=archive_manifest_sha256,
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
    if recovery_root.exists() or recovery_root.is_symlink():
        with _opened_directory(recovery_root, label="recovery claim") as recovery_fd:
            try:
                payload = _read_json_at(
                    recovery_fd,
                    ".intent-claim.json",
                    label="recovery claim",
                )
            except ValueError as error:
                raise FileExistsError(
                    "recovery claim target already exists"
                ) from error
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


@contextmanager
def _open_recovery_archive(
    recovery_root: Path, *, intent: Mapping[str, object]
) -> Iterator[tuple[int, int, tuple[int, int]]]:
    with _opened_directory(recovery_root, label="recovery claim") as recovery_fd:
        claim = _read_json_at(
            recovery_fd,
            ".intent-claim.json",
            label="recovery claim",
        )
        if claim != intent:
            raise ValueError("interrupted archive target claim differs")
        try:
            os.mkdir("archive", mode=0o700, dir_fd=recovery_fd)
        except FileExistsError:
            pass
        with _opened_directory_at(
            recovery_fd,
            "archive",
            label="archive safe directory",
        ) as archive_fd:
            identity = _directory_identity(archive_fd)
            _assert_archive_anchor(recovery_fd, archive_fd, expected=identity)
            yield recovery_fd, archive_fd, identity


def _move_source_directory_no_replace(
    source: Path,
    *,
    destination_name: str,
    archive_fd: int,
) -> None:
    if source.name in {"", ".", ".."} or destination_name in {"", ".", ".."}:
        raise ValueError("interrupted execution directory name is unsafe")
    with _opened_directory(source.parent, label="source parent safe directory") as parent_fd:
        try:
            source_fd = _open_directory_at(
                parent_fd,
                source.name,
                label="source execution directory",
            )
        except FileNotFoundError:
            with _opened_directory_at(
                archive_fd,
                destination_name,
                label="archived execution directory",
            ):
                return
        try:
            source_identity = _directory_identity(source_fd)
            _assert_directory_entry(
                parent_fd,
                source.name,
                expected=source_identity,
                label="source execution directory",
            )
            _atomic_rename_no_replace_at(
                parent_fd,
                source.name,
                archive_fd,
                destination_name,
            )
            _assert_directory_entry(
                archive_fd,
                destination_name,
                expected=source_identity,
                label="archived execution directory",
            )
        finally:
            os.close(source_fd)


def _assert_archive_anchor(
    recovery_fd: int,
    archive_fd: int,
    *,
    expected: tuple[int, int],
) -> None:
    if _directory_identity(archive_fd) != expected:
        raise ValueError("archive safe directory identity changed")
    _assert_directory_entry(
        recovery_fd,
        "archive",
        expected=expected,
        label="archive safe directory",
    )


def _write_or_verify_archive_manifest_at(
    recovery_fd: int,
    archive_fd: int,
    *,
    intent: Mapping[str, object],
    presence: Mapping[str, bool],
) -> str:
    records: list[dict[str, object]] = []
    for label in sorted(presence):
        if not presence[label]:
            continue
        with _opened_directory_at(
            archive_fd,
            label,
            label="archived execution directory",
        ) as artifact_fd:
            records.extend(
                _directory_records_at(
                    artifact_fd,
                    prefix=f"archive/{label}",
                    role=f"interrupted_{label}",
                )
            )
    payload = {
        "attempt_id": intent["attempt_id"],
        "recovery_id": intent["recovery_id"],
        "execution_id": intent["execution_id"],
        "files": records,
    }
    expected_bytes = _json_bytes(payload)
    try:
        existing_bytes = _read_bytes_at(
            recovery_fd,
            "archive_manifest.json",
            label="interrupted archive manifest",
        )
    except FileNotFoundError:
        _write_bytes_exclusive_at(
            recovery_fd,
            "archive_manifest.json",
            expected_bytes,
        )
        existing_bytes = expected_bytes
    else:
        if existing_bytes != expected_bytes:
            raise ValueError("interrupted archive manifest identity differs")
    return hashlib.sha256(existing_bytes).hexdigest()


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
