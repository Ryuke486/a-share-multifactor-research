"""Crash-safe archival of one execution and all of its bound input directories."""

from __future__ import annotations

from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from typing import Iterator, Mapping

from ashare_multifactor.final_test.registry import (
    begin_execution_recovery,
    complete_execution_recovery,
    completed_execution_recovery_intents,
    pending_execution_recovery,
    validate_publication_id,
)
from ashare_multifactor.final_test.recovery_secure_fs import (
    assert_directory_entry as _assert_directory_entry,
    atomic_rename_no_replace_at as _atomic_rename_no_replace_at,
    directory_identity as _directory_identity,
    directory_records_at as _directory_records_at,
    json_bytes as _json_bytes,
    open_directory_at as _open_directory_at,
    opened_directory_at as _opened_directory_at,
    read_bytes_at as _read_bytes_at,
    read_json_at as _read_json_at,
    write_bytes_exclusive_at as _write_bytes_exclusive_at,
)
from ashare_multifactor.final_test.recovery_namespace import (
    assert_recovery_anchors as _assert_recovery_anchors,
    nested_directory_exists_at as _nested_directory_exists_at,
    opened_final_root as _opened_final_root,
    open_existing_recovery_archive_at as _open_existing_recovery_archive_at,
    open_recovery_archive_at as _open_recovery_archive_at,
    reject_unsafe_optional_directory_at as _reject_unsafe_optional_directory_at,
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


@dataclass(frozen=True)
class _SourceAnchor:
    anchor_fd: int | None
    parent_fd: int
    parent_name: str | None
    parent_identity: tuple[int, int]
    source_name: str


@dataclass(frozen=True)
class _MovedArtifact:
    source: _SourceAnchor
    archive_fd: int
    archive_name: str
    directory_identity: tuple[int, int]


def recover_interrupted_execution(
    final_root: Path,
    *,
    preflight: ResumePreflight,
    coverage_snapshot_manifest_sha256: str,
) -> Path | None:
    """Archive all directories bound to one interrupted execution, then audit it."""
    attempt_id = preflight.authorization.attempt_id
    registry_root = final_root / "attempts"
    identities = {
        "prepare_manifest_sha256": preflight.preparation.manifest_sha256,
        "security_event_coverage_sha256": preflight.security_event_coverage_sha256,
        "corporate_action_coverage_sha256": preflight.corporate_action_coverage_sha256,
    }
    with _opened_final_root(final_root) as (final_fd, final_identity):
        _reject_unsafe_optional_directory_at(
            final_fd,
            "interrupted_runs",
            label="interrupted_runs safe directory",
        )
        verified_completed = _verified_completed_archive_hashes_at(
            final_fd,
            final_identity=final_identity,
            registry_root=registry_root,
            attempt_id=attempt_id,
        )
        intent = pending_execution_recovery(
            registry_root,
            attempt_id,
            verified_completed_archive_manifest_sha256=verified_completed,
        )
        presence = (
            dict(intent["artifact_presence"])
            if intent is not None
            else _artifact_presence_at(final_fd, attempt_id=attempt_id)
        )
        if intent is None and not presence["attempt_run"]:
            return None
        with _open_source_anchors_at(
            final_fd,
            final_identity=final_identity,
            attempt_id=attempt_id,
            presence=presence,
        ) as source_anchors:
            if intent is None:
                identity = _load_and_verify_bound_artifacts_at(
                    source_anchors,
                    archive_fd=None,
                    presence=presence,
                    attempt_id=attempt_id,
                    allow_moved=False,
                )
                expected_identity = _expected_execution_identity(
                    preflight,
                    execution_id=identity.get("execution_id"),
                    coverage_snapshot_manifest_sha256=(
                        coverage_snapshot_manifest_sha256
                    ),
                )
                if identity != expected_identity:
                    raise ValueError(
                        "partial final-test execution identity schema or values differ"
                    )
                intent = begin_execution_recovery(
                    registry_root,
                    attempt_id=attempt_id,
                    execution_id=str(identity["execution_id"]),
                    identities=identities,
                    artifact_presence=presence,
                    verified_completed_archive_manifest_sha256=verified_completed,
                )
            with _open_recovery_archive_at(
                final_fd,
                final_identity=final_identity,
                attempt_id=attempt_id,
                intent=intent,
            ) as recovery:
                identity = _load_and_verify_bound_artifacts_at(
                    source_anchors,
                    archive_fd=recovery.archive_fd,
                    presence=presence,
                    attempt_id=attempt_id,
                    allow_moved=True,
                )
                expected_identity = _expected_execution_identity(
                    preflight,
                    execution_id=identity.get("execution_id"),
                    coverage_snapshot_manifest_sha256=(
                        coverage_snapshot_manifest_sha256
                    ),
                )
                if identity != expected_identity:
                    raise ValueError(
                        "partial final-test execution identity schema or values differ"
                    )
                execution_id = str(identity["execution_id"])
                if (
                    intent.get("execution_id") != execution_id
                    or intent.get("identities") != identities
                    or intent.get("artifact_presence") != presence
                ):
                    raise ValueError("pending execution recovery identity differs")
                moved: list[_MovedArtifact] = []
                try:
                    for label in (
                        "attempt_run",
                        "execution_input_sources",
                        "attempt_inputs",
                    ):
                        if not presence[label]:
                            continue
                        _assert_recovery_anchors(recovery)
                        _assert_source_anchors(source_anchors)
                        item = _move_source_directory_no_replace(
                            source_anchors[label],
                            destination_name=label,
                            archive_fd=recovery.archive_fd,
                        )
                        if item is not None:
                            moved.append(item)
                        _assert_recovery_anchors(recovery)
                        _assert_source_anchors(source_anchors)
                    _assert_recovery_anchors(recovery)
                    _assert_source_anchors(source_anchors)
                except BaseException:
                    rollback_error = _rollback_moved_artifacts(moved)
                    if rollback_error is not None:
                        raise RuntimeError(
                            "uncoordinated recovery rollback; evidence retained"
                        ) from rollback_error
                    raise
                archive_manifest_sha256 = _write_or_verify_archive_manifest_at(
                    recovery.recovery_fd,
                    recovery.archive_fd,
                    intent=intent,
                    presence=presence,
                )
                archive_manifest_bytes = _read_bytes_at(
                    recovery.recovery_fd,
                    "archive_manifest.json",
                    label="interrupted archive manifest",
                )
                _assert_recovery_anchors(recovery)
                complete_execution_recovery(
                    registry_root,
                    intent=intent,
                    verified_archive_manifest_sha256=archive_manifest_sha256,
                    verified_archive_manifest_bytes=archive_manifest_bytes,
                    verified_completed_archive_manifest_sha256=verified_completed,
                )
    return final_root / str(intent["archived_path"])


def has_recoverable_execution_archive(final_root: Path, *, attempt_id: str) -> bool:
    try:
        with _opened_final_root(final_root) as (final_fd, _final_identity):
            registry_root = final_root / "attempts"
            verified_completed = _verified_completed_archive_hashes_at(
                final_fd,
                final_identity=_final_identity,
                registry_root=registry_root,
                attempt_id=attempt_id,
            )
            intent = pending_execution_recovery(
                registry_root,
                attempt_id,
                verified_completed_archive_manifest_sha256=verified_completed,
            )
            if intent is None:
                return False
            if _nested_directory_exists_at(
                final_fd,
                parent_name="attempt_runs",
                child_name=attempt_id,
                label="partial execution source",
            ):
                return False
            recovery_id = validate_publication_id(
                str(intent.get("recovery_id", ""))
            )
            with _opened_directory_at(
                final_fd,
                "interrupted_runs",
                label="interrupted_runs safe directory",
            ) as interrupted_fd:
                with _opened_directory_at(
                    interrupted_fd,
                    attempt_id,
                    label="interrupted attempt directory",
                ) as attempt_fd:
                    with _opened_directory_at(
                        attempt_fd,
                        recovery_id,
                        label="recovery claim",
                    ) as recovery_fd:
                        if (
                            _read_json_at(
                                recovery_fd,
                                ".intent-claim.json",
                                label="recovery claim",
                            )
                            != intent
                        ):
                            return False
                        with _opened_directory_at(
                            recovery_fd,
                            "archive",
                            label="archive safe directory",
                        ) as archive_fd:
                            with _opened_directory_at(
                                archive_fd,
                                "attempt_run",
                                label="archived execution directory",
                            ):
                                return True
    except (FileNotFoundError, ValueError):
        return False


def _verified_completed_archive_hashes_at(
    final_fd: int,
    *,
    final_identity: tuple[int, int],
    registry_root: Path,
    attempt_id: str,
) -> dict[str, str]:
    verified: dict[str, str] = {}
    for intent in completed_execution_recovery_intents(registry_root, attempt_id):
        recovery_id = validate_publication_id(str(intent.get("recovery_id", "")))
        with _open_existing_recovery_archive_at(
            final_fd,
            final_identity=final_identity,
            attempt_id=attempt_id,
            intent=intent,
        ) as recovery:
            manifest_bytes = _read_bytes_at(
                recovery.recovery_fd,
                "archive_manifest.json",
                label="completed execution recovery archive manifest",
            )
            _assert_recovery_anchors(recovery)
            verified[recovery_id] = hashlib.sha256(manifest_bytes).hexdigest()
    return verified


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


def _artifact_presence_at(final_fd: int, *, attempt_id: str) -> dict[str, bool]:
    presence = {
        "execution_input_sources": _reject_unsafe_optional_directory_at(
            final_fd,
            "execution_input_sources",
            label="execution_input_sources source directory",
        )
    }
    for label, parent_name in (
        ("attempt_run", "attempt_runs"),
        ("attempt_inputs", "attempt_inputs"),
    ):
        if not _reject_unsafe_optional_directory_at(
            final_fd,
            parent_name,
            label=f"{label} source parent",
        ):
            presence[label] = False
            continue
        with _opened_directory_at(
            final_fd,
            parent_name,
            label=f"{label} source parent",
        ) as parent_fd:
            presence[label] = _reject_unsafe_optional_directory_at(
                parent_fd,
                attempt_id,
                label=f"{label} source directory",
            )
    return {key: presence[key] for key in sorted(presence)}


def _move_source_directory_no_replace(
    source: _SourceAnchor,
    *,
    destination_name: str,
    archive_fd: int,
) -> _MovedArtifact | None:
    if source.source_name in {"", ".", ".."} or destination_name in {"", ".", ".."}:
        raise ValueError("interrupted execution directory name is unsafe")
    try:
        source_fd = _open_directory_at(
            source.parent_fd,
            source.source_name,
            label="source execution directory",
        )
    except FileNotFoundError:
        with _opened_directory_at(
            archive_fd,
            destination_name,
            label="archived execution directory",
        ):
            return None
    try:
        source_identity = _directory_identity(source_fd)
        _assert_directory_entry(
            source.parent_fd,
            source.source_name,
            expected=source_identity,
            label="source execution directory",
        )
        _atomic_rename_no_replace_at(
            source.parent_fd,
            source.source_name,
            archive_fd,
            destination_name,
        )
        _assert_directory_entry(
            archive_fd,
            destination_name,
            expected=source_identity,
            label="archived execution directory",
        )
        return _MovedArtifact(
            source=source,
            archive_fd=archive_fd,
            archive_name=destination_name,
            directory_identity=source_identity,
        )
    finally:
        os.close(source_fd)


@contextmanager
def _open_source_anchors_at(
    final_fd: int,
    *,
    final_identity: tuple[int, int],
    attempt_id: str,
    presence: Mapping[str, bool],
) -> Iterator[dict[str, _SourceAnchor]]:
    with ExitStack() as stack:
        anchors: dict[str, _SourceAnchor] = {}
        if presence["execution_input_sources"]:
            anchors["execution_input_sources"] = _SourceAnchor(
                anchor_fd=None,
                parent_fd=final_fd,
                parent_name=None,
                parent_identity=final_identity,
                source_name="execution_input_sources",
            )
        for label, parent_name, source_name in (
            ("attempt_run", "attempt_runs", attempt_id),
            ("attempt_inputs", "attempt_inputs", attempt_id),
        ):
            if not presence[label]:
                continue
            parent_fd = stack.enter_context(
                _opened_directory_at(
                    final_fd,
                    parent_name,
                    label=f"{label} source parent",
                )
            )
            anchors[label] = _SourceAnchor(
                anchor_fd=final_fd,
                parent_fd=parent_fd,
                parent_name=parent_name,
                parent_identity=_directory_identity(parent_fd),
                source_name=source_name,
            )
        _assert_source_anchors(anchors)
        yield anchors


def _assert_source_anchors(anchors: Mapping[str, _SourceAnchor]) -> None:
    for anchor in anchors.values():
        if _directory_identity(anchor.parent_fd) != anchor.parent_identity:
            raise ValueError("source parent directory identity changed")
        if anchor.anchor_fd is not None and anchor.parent_name is not None:
            _assert_directory_entry(
                anchor.anchor_fd,
                anchor.parent_name,
                expected=anchor.parent_identity,
                label="source parent directory",
            )


def _load_and_verify_bound_artifacts_at(
    anchors: Mapping[str, _SourceAnchor],
    *,
    archive_fd: int | None,
    presence: Mapping[str, bool],
    attempt_id: str,
    allow_moved: bool,
) -> dict[str, object]:
    payloads: dict[str, dict[str, object]] = {}
    for label in (
        "attempt_run",
        "execution_input_sources",
        "attempt_inputs",
    ):
        if not presence[label]:
            continue
        anchor = anchors[label]
        with ExitStack() as stack:
            try:
                artifact_fd = stack.enter_context(
                    _opened_directory_at(
                        anchor.parent_fd,
                        anchor.source_name,
                        label=f"{label} source directory",
                    )
                )
            except FileNotFoundError as error:
                if not allow_moved or archive_fd is None:
                    raise ValueError(
                        "interrupted execution artifact is missing"
                    ) from error
                try:
                    artifact_fd = stack.enter_context(
                        _opened_directory_at(
                            archive_fd,
                            label,
                            label=f"archived {label} directory",
                        )
                    )
                except FileNotFoundError as archive_error:
                    raise ValueError(
                        "interrupted execution artifact is missing"
                    ) from archive_error
            manifest_name = {
                "attempt_run": "execution_identity.json",
                "execution_input_sources": "source_manifest.json",
                "attempt_inputs": "manifest.json",
            }[label]
            payloads[label] = _read_json_at(
                artifact_fd,
                manifest_name,
                label=f"{label} identity",
            )
    identity = payloads["attempt_run"]
    if set(identity) != _IDENTITY_KEYS:
        raise ValueError("partial final-test execution identity schema differs")
    execution_id = identity.get("execution_id")
    for label, payload in payloads.items():
        if payload.get("execution_id") != execution_id:
            raise ValueError("interrupted execution artifact identity differs")
        if label != "attempt_inputs" and payload.get("attempt_id") != attempt_id:
            raise ValueError("interrupted execution artifact attempt differs")
    return identity


def _rollback_moved_artifacts(
    moved: list[_MovedArtifact],
) -> BaseException | None:
    errors: list[BaseException] = []
    for item in reversed(moved):
        try:
            _atomic_rename_no_replace_at(
                item.archive_fd,
                item.archive_name,
                item.source.parent_fd,
                item.source.source_name,
            )
            _assert_directory_entry(
                item.source.parent_fd,
                item.source.source_name,
                expected=item.directory_identity,
                label="restored execution source",
            )
            try:
                os.stat(
                    item.archive_name,
                    dir_fd=item.archive_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise ValueError("rollback left source content in external archive")
        except BaseException as error:
            errors.append(error)
    return errors[0] if errors else None


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
