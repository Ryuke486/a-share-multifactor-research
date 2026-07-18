"""Immutable archival for a failed final-test attempt."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
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
from ashare_multifactor.final_test.gate import FINAL_TEST_END, FINAL_TEST_START
from ashare_multifactor.final_test.recovery_secure_fs import (
    assert_directory_entry,
    atomic_rename_no_replace_at,
    directory_identity,
    directory_records_at,
    json_bytes,
    open_directory_at,
    opened_directory,
    read_json_at,
    write_bytes_exclusive_at,
)
from ashare_multifactor.final_test.registry import (
    resolve_attempt_state_readonly,
    validate_publication_id,
)


_MANIFEST = "incident_manifest.json"
_MANIFEST_TEMP = re.compile(r"\.incident-manifest\.[0-9a-f]{32}\.tmp")


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
    source = processed_root / "final_test"
    archive_parent = processed_root / "final_test_incidents"
    destination = archive_parent / attempt_id
    if not source.exists():
        if destination.is_dir() and not destination.is_symlink():
            _verify_archive(destination, attempt_id=attempt_id)
            return destination
        raise FileNotFoundError(source)
    _assert_safe_directory(source, label="final-test incident source")
    if archive_parent.is_symlink():
        raise ValueError("final-test incident archive uses a symlink")
    archive_parent.mkdir(exist_ok=True)
    _assert_safe_directory(archive_parent, label="final-test incident archive")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("final-test incident destination already exists")

    with _locked_existing_preparation(source / "attempts", attempt_id=attempt_id):
        with opened_directory(processed_root, label="processed root") as processed_fd:
            with opened_directory(
                archive_parent, label="final-test incident archive"
            ) as archive_fd:
                source_fd = open_directory_at(
                    processed_fd,
                    "final_test",
                    label="final-test incident source",
                )
                try:
                    source_identity = directory_identity(source_fd)
                    _recover_orphan_manifest_temps(source_fd)
                    claim = read_json_at(
                        source_fd,
                        "data-build-claim.json",
                        label="final-test incident data claim",
                    )
                    if (
                        claim.get("attempt_id") != attempt_id
                        or claim.get("status") != "published"
                    ):
                        raise ValueError(
                            "final-test incident data claim is not a published attempt"
                        )
                    attempts_fd = open_directory_at(
                        source_fd,
                        "attempts",
                        label="final-test incident attempts",
                    )
                    try:
                        outcome = read_json_at(
                            attempts_fd,
                            f"{attempt_id}.outcome.json",
                            label="final-test incident outcome",
                        )
                    finally:
                        os.close(attempts_fd)
                    if (
                        outcome.get("attempt_id") != attempt_id
                        or outcome.get("status") != "failed"
                        or outcome.get("authoritative") is not False
                    ):
                        raise ValueError(
                            "only a non-authoritative failed attempt can be archived"
                        )
                    reason = outcome.get("reason")
                    if not isinstance(reason, str) or not reason:
                        raise ValueError("failed attempt outcome lacks an incident reason")
                    _validate_complete_evidence(
                        source, claim=claim, attempt_id=attempt_id
                    )
                    assert_directory_entry(
                        processed_fd,
                        "final_test",
                        expected=source_identity,
                        label="final-test incident source",
                    )
                    files = _evidence_records(source_fd)
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
                        raise ValueError(
                            "final-test incident evidence changed during archival"
                        )
                    assert_directory_entry(
                        processed_fd,
                        "final_test",
                        expected=source_identity,
                        label="final-test incident source",
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
                    os.fsync(processed_fd)
                    os.fsync(archive_fd)
                finally:
                    os.close(source_fd)
    _verify_archive(destination, attempt_id=attempt_id)
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
def _locked_existing_preparation(
    attempts_root: Path,
    *,
    attempt_id: str,
) -> Iterator[None]:
    try:
        descriptor = os.open(
            attempts_root / f"{attempt_id}.preparation.lock",
            os.O_RDWR | os.O_NOFOLLOW,
        )
    except OSError as error:
        raise ValueError("final-test incident preparation lock is missing or unsafe") from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
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


def _validate_complete_evidence(
    source: Path,
    *,
    claim: dict[str, object],
    attempt_id: str,
) -> None:
    allowed_entries = {
        "attempts",
        "daily_panel",
        "data-build-claim.json",
        "data-build-inputs",
        "data-staging",
        _MANIFEST,
    }
    unexpected = sorted(path.name for path in source.iterdir() if path.name not in allowed_entries)
    if unexpected:
        raise ValueError(f"final-test incident contains mixed or authoritative evidence: {unexpected}")
    staging = source / "data-staging"
    if staging.exists() and (
        staging.is_symlink()
        or not staging.is_dir()
        or any(staging.iterdir())
    ):
        raise ValueError("final-test incident contains active data staging evidence")
    attempts = source / "attempts"
    if attempts.is_symlink() or not attempts.is_dir():
        raise ValueError("final-test incident attempt evidence is incomplete")
    if any(not path.name.startswith(f"{attempt_id}.") for path in attempts.iterdir()):
        raise ValueError("final-test incident contains evidence from another attempt")
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
    if any(claim.get(field) != state.get(field) for field in identity_fields):
        raise ValueError("final-test incident claim and attempt identity differ")

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
    inventory_path = verify_file_identity(
        source, inventory, "data build input inventory"
    )
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


def _verify_archive(destination: Path, *, attempt_id: str) -> None:
    _assert_safe_directory(destination, label="final-test incident destination")
    with opened_directory(destination, label="final-test incident destination") as fd:
        manifest = read_json_at(fd, _MANIFEST, label="final-test incident manifest")
        _assert_manifest(
            manifest,
            attempt_id=attempt_id,
            files=_evidence_records(fd),
        )


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
