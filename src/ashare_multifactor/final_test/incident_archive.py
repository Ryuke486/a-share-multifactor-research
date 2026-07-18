"""Immutable archival for a failed final-test attempt."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Iterator
from uuid import uuid4

from ashare_multifactor.config import Period
from ashare_multifactor.data.manifest import validate_panel_source
from ashare_multifactor.final_test.data_inventory import verify_file_identity
from ashare_multifactor.final_test.data_publication import resolve_final_test_data_panel
from ashare_multifactor.final_test.gate import (
    FINAL_TEST_END,
    FINAL_TEST_START,
    FinalTestAuthorization,
)
from ashare_multifactor.final_test.incident_snapshot import (
    copied_directory_snapshot,
    working_directory_at,
)
from ashare_multifactor.final_test.preparation import verify_preparation
from ashare_multifactor.final_test.recovery_secure_fs import (
    assert_directory_entry,
    atomic_rename_no_replace_at,
    directory_identity,
    directory_records_at,
    json_bytes,
    open_directory_at,
    opened_directory,
    opened_directory_at,
    read_json_at,
    write_bytes_exclusive_at,
)
from ashare_multifactor.final_test.registry import (
    resolve_attempt_state_readonly,
    validate_publication_id,
)


_MANIFEST = "incident_manifest.json"
_MANIFEST_TEMP = re.compile(r"\.incident-manifest\.[0-9a-f]{32}\.tmp")
_GIT_OBJECT = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ATTEMPT_REGISTRATION_FIELDS = frozenset(
    {
        "attempt_id",
        "registered_at",
        "git_commit",
        "git_tree",
        "token_sha256",
        "sealed_protocol_sha256",
        "robustness_release",
        "robustness_manifest_sha256",
        "robustness_lineage_sha256",
        "approval_id",
        "status",
        "authoritative",
    }
)


def _publish_manifest_atomic(
    source_fd: int,
    payload: bytes,
) -> None:
    temporary = f".incident-manifest.{uuid4().hex}.tmp"
    try:
        write_bytes_exclusive_at(source_fd, temporary, payload)
        descriptor = os.open(
            temporary,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=source_fd,
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        atomic_rename_no_replace_at(
            source_fd,
            temporary,
            source_fd,
            _MANIFEST,
        )
        os.fsync(source_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=source_fd)
        except FileNotFoundError:
            pass


def archive_failed_attempt(
    processed_root: Path,
    *,
    attempt_id: str,
) -> Path:
    """Move one terminal failed attempt into a no-replace sibling archive."""
    validate_publication_id(attempt_id)
    _assert_safe_directory(processed_root, label="processed root")
    archive_parent = processed_root / "final_test_incidents"
    destination = archive_parent / attempt_id
    with opened_directory(processed_root, label="processed root") as processed_fd:
        try:
            os.mkdir("final_test_incidents", mode=0o700, dir_fd=processed_fd)
        except FileExistsError:
            pass
        with opened_directory_at(
            processed_fd,
            "final_test_incidents",
            label="final-test incident archive",
        ) as archive_fd:
            archive_identity = directory_identity(archive_fd)
            assert_directory_entry(
                processed_fd,
                "final_test_incidents",
                expected=archive_identity,
                label="final-test incident archive",
            )
            try:
                source_fd = open_directory_at(
                    processed_fd,
                    "final_test",
                    label="final-test incident source",
                )
            except FileNotFoundError:
                _verify_archive_at(archive_fd, attempt_id=attempt_id)
                assert_directory_entry(
                    processed_fd,
                    "final_test_incidents",
                    expected=archive_identity,
                    label="final-test incident archive",
                )
                return destination
            try:
                source_identity = directory_identity(source_fd)
                with opened_directory_at(
                    source_fd,
                    "attempts",
                    label="final-test incident attempts",
                ) as attempts_fd:
                    attempts_identity = directory_identity(attempts_fd)
                    with _locked_existing_attempt_at(
                        attempts_fd,
                        attempt_id=attempt_id,
                    ):
                        assert_directory_entry(
                            processed_fd,
                            "final_test",
                            expected=source_identity,
                            label="final-test incident source",
                        )
                        assert_directory_entry(
                            source_fd,
                            "attempts",
                            expected=attempts_identity,
                            label="final-test incident attempts",
                        )
                        _recover_orphan_manifest_temps(source_fd)
                        with copied_directory_snapshot(
                            source_fd,
                            archive_fd,
                            label="final-test incident evidence",
                        ) as snapshot:
                            files = _evidence_records(snapshot.descriptor)
                            if _evidence_records(source_fd) != files:
                                raise ValueError(
                                    "final-test incident evidence changed during snapshot"
                                )
                            with working_directory_at(snapshot.descriptor):
                                snapshot_root = Path(".")
                                claim = read_json_at(
                                    snapshot.descriptor,
                                    "data-build-claim.json",
                                    label="final-test incident data claim",
                                )
                                reason = _failed_outcome_reason(
                                    snapshot_root,
                                    attempt_id=attempt_id,
                                )
                                _validate_complete_evidence(
                                    snapshot_root,
                                    claim=claim,
                                    attempt_id=attempt_id,
                                )
                            if _evidence_records(source_fd) != files:
                                raise ValueError(
                                    "final-test incident evidence changed during archival"
                                )
                        assert_directory_entry(
                            processed_fd,
                            "final_test",
                            expected=source_identity,
                            label="final-test incident source",
                        )
                        assert_directory_entry(
                            source_fd,
                            "attempts",
                            expected=attempts_identity,
                            label="final-test incident attempts",
                        )
                        manifest = {
                            "schema_version": "1",
                            "attempt_id": attempt_id,
                            "status": "archived_failed_non_authoritative_attempt",
                            "reason": reason,
                            "source_relative_path": "final_test",
                            "archived_at": datetime.now(timezone.utc).isoformat(),
                            "files": files,
                        }
                        try:
                            _publish_manifest_atomic(source_fd, json_bytes(manifest))
                        except FileExistsError:
                            existing = read_json_at(
                                source_fd,
                                _MANIFEST,
                                label="final-test incident manifest",
                            )
                            _assert_manifest(existing, attempt_id=attempt_id, files=files)
                        os.fsync(source_fd)
                        if _evidence_records(source_fd) != files:
                            raise ValueError("final-test incident evidence changed during archival")
                        assert_directory_entry(
                            processed_fd,
                            "final_test",
                            expected=source_identity,
                            label="final-test incident source",
                        )
                        assert_directory_entry(
                            source_fd,
                            "attempts",
                            expected=attempts_identity,
                            label="final-test incident attempts",
                        )
                        assert_directory_entry(
                            processed_fd,
                            "final_test_incidents",
                            expected=archive_identity,
                            label="final-test incident archive",
                        )
                        atomic_rename_no_replace_at(
                            processed_fd,
                            "final_test",
                            archive_fd,
                            attempt_id,
                        )
                        assert_directory_entry(
                            archive_fd,
                            attempt_id,
                            expected=source_identity,
                            label="final-test incident destination",
                        )
                        assert_directory_entry(
                            processed_fd,
                            "final_test_incidents",
                            expected=archive_identity,
                            label="final-test incident archive",
                        )
                        _verify_archive_at(
                            archive_fd,
                            attempt_id=attempt_id,
                            validate_evidence=False,
                        )
                        os.fsync(processed_fd)
                        os.fsync(archive_fd)
                        assert_directory_entry(
                            processed_fd,
                            "final_test_incidents",
                            expected=archive_identity,
                            label="final-test incident archive",
                        )
            finally:
                os.close(source_fd)
    return destination


def _recover_orphan_manifest_temps(source_fd: int) -> None:
    removed = False
    for name in sorted(os.listdir(source_fd)):
        if _MANIFEST_TEMP.fullmatch(name) is None:
            continue
        metadata = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("final-test incident manifest temp is unsafe")
        os.unlink(name, dir_fd=source_fd)
        removed = True
    if removed:
        os.fsync(source_fd)


@contextmanager
def _locked_existing_attempt_at(
    attempts_fd: int,
    *,
    attempt_id: str,
) -> Iterator[None]:
    descriptors: list[int] = []
    try:
        for suffix, label in (
            ("preparation.lock", "preparation"),
            ("lock", "state"),
        ):
            try:
                descriptor = os.open(
                    f"{attempt_id}.{suffix}",
                    os.O_RDWR | os.O_NOFOLLOW,
                    dir_fd=attempts_fd,
                )
            except OSError as error:
                raise ValueError(
                    f"final-test incident {label} lock is missing or unsafe"
                ) from error
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            descriptors.append(descriptor)
        yield
    finally:
        for descriptor in reversed(descriptors):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _evidence_records(directory_fd: int) -> list[dict[str, object]]:
    return [
        record
        for record in directory_records_at(
            directory_fd,
            prefix="final_test",
            role="failed_final_test_evidence",
        )
        if record["path"] != f"final_test/{_MANIFEST}"
    ]


def _failed_outcome_reason(source: Path, *, attempt_id: str) -> str:
    attempts = source / "attempts"
    outcome_path = attempts / f"{attempt_id}.outcome.json"
    if outcome_path.is_symlink():
        raise ValueError("final-test incident outcome uses a symlink")
    try:
        outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError("final-test incident outcome is missing or invalid") from error
    if (
        not isinstance(outcome, dict)
        or outcome.get("attempt_id") != attempt_id
        or outcome.get("status") != "failed"
        or outcome.get("authoritative") is not False
    ):
        raise ValueError("only a non-authoritative failed attempt can be archived")
    reason = outcome.get("reason")
    if not isinstance(reason, str) or not reason:
        raise ValueError("failed attempt outcome lacks an incident reason")
    return reason


def _validate_complete_evidence(
    source: Path,
    *,
    claim: dict[str, object],
    attempt_id: str,
) -> None:
    if claim.get("attempt_id") != attempt_id or claim.get("status") != "published":
        raise ValueError("final-test incident data claim is not a published attempt")
    allowed_entries = {
        "attempts",
        "daily_panel",
        "data-build-claim.json",
        "data-build-inputs",
        "data-staging",
        ".data-claim.publication.lock",
        "preparations",
        _MANIFEST,
    }
    unexpected = sorted(path.name for path in source.iterdir() if path.name not in allowed_entries)
    if unexpected:
        raise ValueError(
            f"final-test incident contains mixed or authoritative evidence: {unexpected}"
        )
    staging = source / "data-staging"
    if staging.exists() and (
        staging.is_symlink() or not staging.is_dir() or any(staging.iterdir())
    ):
        raise ValueError("final-test incident contains active data staging evidence")
    attempts = source / "attempts"
    if attempts.is_symlink() or not attempts.is_dir():
        raise ValueError("final-test incident attempt evidence is incomplete")
    if any(not path.name.startswith(f"{attempt_id}.") for path in attempts.iterdir()):
        raise ValueError("final-test incident contains evidence from another attempt")
    record = _validated_attempt_registration(attempts, attempt_id=attempt_id)
    _verify_token_snapshot(attempts, attempt_id=attempt_id, record=record)
    state = resolve_attempt_state_readonly(attempts, attempt_id)
    if state.get("state") != "failed" or state.get("authoritative") is not False:
        raise ValueError("final-test incident attempt is not terminal failed")
    identity_fields = (
        "approval_id",
        "git_commit",
        "git_tree",
        "sealed_protocol_sha256",
        "robustness_release",
        "robustness_manifest_sha256",
        "robustness_lineage_sha256",
    )
    if any(claim.get(field) != record[field] for field in identity_fields):
        raise ValueError("final-test incident claim and attempt identity differ")

    preparations = source / "preparations"
    expected_preparation = preparations / attempt_id
    has_preparation_identity = "prepare_manifest_sha256" in state["identities"]
    if has_preparation_identity:
        if (
            preparations.is_symlink()
            or not preparations.is_dir()
            or set(preparations.iterdir()) != {expected_preparation}
        ):
            raise ValueError("final-test incident preparation evidence is incomplete")
        verify_preparation(
            source,
            attempt_id=attempt_id,
            authorization=_authorization_from_attempt_record(record),
            expected_state="failed",
        )
    elif preparations.exists() or preparations.is_symlink():
        raise ValueError("unbound final-test preparation evidence cannot be archived")

    resolution = resolve_final_test_data_panel(source)
    validate_panel_source(
        resolution.root,
        Period(FINAL_TEST_START, FINAL_TEST_END),
    )
    inventory = claim.get("input_inventory")
    if not isinstance(inventory, dict):
        raise ValueError("final-test incident input evidence is incomplete")
    inventory_root = source / "data-build-inputs"
    expected_inventory = inventory_root / f"{attempt_id}.json"
    inventory_path = verify_file_identity(source, inventory, "data build input inventory")
    if inventory_path != expected_inventory:
        raise ValueError("final-test incident input inventory path is not canonical")
    if inventory_path.read_bytes() != (resolution.root / "input_files.json").read_bytes():
        raise ValueError("final-test incident inventory and panel identity differ")
    if (
        inventory_root.is_symlink()
        or not expected_inventory.is_file()
        or set(inventory_root.iterdir()) != {expected_inventory}
    ):
        raise ValueError("final-test incident input evidence is incomplete")


def _validated_attempt_registration(
    attempts: Path,
    *,
    attempt_id: str,
) -> dict[str, str | bool]:
    path = attempts / f"{attempt_id}.json"
    if path.is_symlink():
        raise ValueError("final-test incident registration uses a symlink")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError("invalid final-test incident registration") from error
    string_fields = {
        "attempt_id",
        "registered_at",
        "git_commit",
        "git_tree",
        "token_sha256",
        "sealed_protocol_sha256",
        "robustness_release",
        "robustness_manifest_sha256",
        "robustness_lineage_sha256",
        "approval_id",
        "status",
    }
    if (
        not isinstance(record, dict)
        or set(record) != _ATTEMPT_REGISTRATION_FIELDS
        or any(
            not isinstance(record.get(field), str) or not record[field] for field in string_fields
        )
        or record.get("attempt_id") != attempt_id
        or record.get("status") != "registered"
        or record.get("authoritative") is not False
        or _GIT_OBJECT.fullmatch(record["git_commit"]) is None
        or _GIT_OBJECT.fullmatch(record["git_tree"]) is None
        or any(
            _SHA256.fullmatch(record[field]) is None
            for field in (
                "token_sha256",
                "sealed_protocol_sha256",
                "robustness_manifest_sha256",
                "robustness_lineage_sha256",
            )
        )
    ):
        raise ValueError("invalid final-test incident registration")
    validate_publication_id(record["robustness_release"])
    return record


def _verify_token_snapshot(
    attempts: Path,
    *,
    attempt_id: str,
    record: dict[str, str | bool],
) -> None:
    token = attempts / f"{attempt_id}.token"
    if token.is_symlink() or not token.is_file():
        raise ValueError("final-test incident token snapshot is missing or unsafe")
    if hashlib.sha256(token.read_bytes()).hexdigest() != record["token_sha256"]:
        raise ValueError("final-test incident token snapshot identity differs")


def _authorization_from_attempt_record(
    record: dict[str, str | bool],
) -> FinalTestAuthorization:
    return FinalTestAuthorization(
        attempt_id=record["attempt_id"],
        approval_id=record["approval_id"],
        registered_at=record["registered_at"],
        git_commit=record["git_commit"],
        git_tree=record["git_tree"],
        sealed_protocol_sha256=record["sealed_protocol_sha256"],
        robustness_release=record["robustness_release"],
        robustness_manifest_sha256=record["robustness_manifest_sha256"],
        robustness_lineage_sha256=record["robustness_lineage_sha256"],
        test_period=(FINAL_TEST_START, FINAL_TEST_END),
    )


def _verify_archive_at(
    archive_fd: int,
    *,
    attempt_id: str,
    validate_evidence: bool = True,
) -> None:
    with opened_directory_at(
        archive_fd,
        attempt_id,
        label="final-test incident destination",
    ) as fd:
        manifest = read_json_at(fd, _MANIFEST, label="final-test incident manifest")
        _assert_manifest(
            manifest,
            attempt_id=attempt_id,
            files=_evidence_records(fd),
        )
        if validate_evidence:
            with working_directory_at(fd):
                root = Path(".")
                claim = read_json_at(
                    fd,
                    "data-build-claim.json",
                    label="final-test incident data claim",
                )
                reason = _failed_outcome_reason(root, attempt_id=attempt_id)
                _validate_complete_evidence(
                    root,
                    claim=claim,
                    attempt_id=attempt_id,
                )
            if manifest["reason"] != reason:
                raise ValueError("final-test incident manifest reason differs from outcome")


def _assert_manifest(
    manifest: dict[str, object],
    *,
    attempt_id: str,
    files: list[dict[str, object]],
) -> None:
    if (
        manifest.get("schema_version") != "1"
        or manifest.get("attempt_id") != attempt_id
        or manifest.get("status") != "archived_failed_non_authoritative_attempt"
        or manifest.get("source_relative_path") != "final_test"
        or not isinstance(manifest.get("reason"), str)
        or not isinstance(manifest.get("archived_at"), str)
        or manifest.get("files") != files
    ):
        raise ValueError("final-test incident manifest identity mismatch")


def _assert_safe_directory(path: Path, *, label: str) -> None:
    if path.is_symlink() or not path.is_dir() or path.resolve() != path.absolute():
        raise ValueError(f"{label} is not a safe canonical directory")
