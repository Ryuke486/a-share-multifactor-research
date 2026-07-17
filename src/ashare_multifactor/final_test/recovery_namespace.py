"""Anchored directory namespace for interrupted final-test recovery."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Iterator, Mapping
from uuid import uuid4

from ashare_multifactor.final_test.recovery_secure_fs import (
    assert_directory_entry,
    atomic_rename_no_replace_at,
    directory_identity,
    json_bytes,
    open_directory_at,
    opened_directory,
    opened_directory_at,
    read_json_at,
    write_bytes_exclusive_at,
)
from ashare_multifactor.final_test.registry import validate_publication_id


@dataclass(frozen=True)
class RecoveryAnchors:
    final_fd: int
    final_identity: tuple[int, int]
    interrupted_fd: int
    interrupted_identity: tuple[int, int]
    attempt_fd: int
    attempt_name: str
    attempt_identity: tuple[int, int]
    recovery_fd: int
    recovery_name: str
    recovery_identity: tuple[int, int]
    archive_fd: int
    archive_identity: tuple[int, int]


@contextmanager
def opened_final_root(final_root: Path) -> Iterator[tuple[int, tuple[int, int]]]:
    try:
        canonical = final_root.resolve(strict=True)
    except OSError as error:
        raise ValueError("final-test root is not a safe canonical directory") from error
    if canonical != final_root.absolute():
        raise ValueError("final-test root is not a safe canonical directory")
    with opened_directory(final_root, label="final-test root") as final_fd:
        identity = directory_identity(final_fd)
        metadata = os.stat(final_root, follow_symlinks=False)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != identity
        ):
            raise ValueError("final-test root identity changed")
        yield final_fd, identity


def reject_unsafe_optional_directory_at(
    parent_fd: int,
    name: str,
    *,
    label: str,
) -> bool:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} uses a symlink or is not a safe directory")
    return True


def nested_directory_exists_at(
    parent_fd: int,
    *,
    parent_name: str,
    child_name: str,
    label: str,
) -> bool:
    if not reject_unsafe_optional_directory_at(
        parent_fd,
        parent_name,
        label=f"{label} parent",
    ):
        return False
    with opened_directory_at(
        parent_fd,
        parent_name,
        label=f"{label} parent",
    ) as nested_fd:
        return reject_unsafe_optional_directory_at(
            nested_fd,
            child_name,
            label=label,
        )


def assert_recovery_anchors(anchors: RecoveryAnchors) -> None:
    for descriptor, expected, label in (
        (anchors.final_fd, anchors.final_identity, "final-test root"),
        (
            anchors.interrupted_fd,
            anchors.interrupted_identity,
            "interrupted_runs safe directory",
        ),
        (anchors.attempt_fd, anchors.attempt_identity, "interrupted attempt directory"),
        (anchors.recovery_fd, anchors.recovery_identity, "recovery claim"),
        (anchors.archive_fd, anchors.archive_identity, "archive safe directory"),
    ):
        if directory_identity(descriptor) != expected:
            raise ValueError(f"{label} identity changed")
    assert_directory_entry(
        anchors.final_fd,
        "interrupted_runs",
        expected=anchors.interrupted_identity,
        label="interrupted_runs safe directory",
    )
    assert_directory_entry(
        anchors.interrupted_fd,
        anchors.attempt_name,
        expected=anchors.attempt_identity,
        label="interrupted attempt directory",
    )
    assert_directory_entry(
        anchors.attempt_fd,
        anchors.recovery_name,
        expected=anchors.recovery_identity,
        label="recovery claim",
    )
    assert_directory_entry(
        anchors.recovery_fd,
        "archive",
        expected=anchors.archive_identity,
        label="archive safe directory",
    )


@contextmanager
def open_recovery_archive_at(
    final_fd: int,
    *,
    final_identity: tuple[int, int],
    attempt_id: str,
    intent: Mapping[str, object],
) -> Iterator[RecoveryAnchors]:
    recovery_name = validate_publication_id(str(intent.get("recovery_id", "")))
    with _opened_or_created_directory_at(
        final_fd,
        "interrupted_runs",
        label="interrupted_runs safe directory",
    ) as (interrupted_fd, interrupted_identity):
        with _opened_or_created_directory_at(
            interrupted_fd,
            attempt_id,
            label="interrupted attempt directory",
        ) as (attempt_fd, attempt_identity):
            _resolve_or_publish_claim_at(
                attempt_fd,
                recovery_name,
                intent=intent,
            )
            with opened_directory_at(
                attempt_fd,
                recovery_name,
                label="recovery claim",
            ) as recovery_fd:
                recovery_identity = directory_identity(recovery_fd)
                assert_directory_entry(
                    attempt_fd,
                    recovery_name,
                    expected=recovery_identity,
                    label="recovery claim",
                )
                claim = read_json_at(
                    recovery_fd,
                    ".intent-claim.json",
                    label="recovery claim",
                )
                if claim != intent:
                    raise ValueError("interrupted archive target claim differs")
                with _opened_or_created_directory_at(
                    recovery_fd,
                    "archive",
                    label="archive safe directory",
                ) as (archive_fd, archive_identity):
                    anchors = RecoveryAnchors(
                        final_fd=final_fd,
                        final_identity=final_identity,
                        interrupted_fd=interrupted_fd,
                        interrupted_identity=interrupted_identity,
                        attempt_fd=attempt_fd,
                        attempt_name=attempt_id,
                        attempt_identity=attempt_identity,
                        recovery_fd=recovery_fd,
                        recovery_name=recovery_name,
                        recovery_identity=recovery_identity,
                        archive_fd=archive_fd,
                        archive_identity=archive_identity,
                    )
                    assert_recovery_anchors(anchors)
                    yield anchors


@contextmanager
def open_existing_recovery_archive_at(
    final_fd: int,
    *,
    final_identity: tuple[int, int],
    attempt_id: str,
    intent: Mapping[str, object],
) -> Iterator[RecoveryAnchors]:
    """Open an existing complete recovery without creating any path component."""
    recovery_name = validate_publication_id(str(intent.get("recovery_id", "")))
    with opened_directory_at(
        final_fd,
        "interrupted_runs",
        label="interrupted_runs safe directory",
    ) as interrupted_fd:
        interrupted_identity = directory_identity(interrupted_fd)
        with opened_directory_at(
            interrupted_fd,
            attempt_id,
            label="interrupted attempt directory",
        ) as attempt_fd:
            attempt_identity = directory_identity(attempt_fd)
            with opened_directory_at(
                attempt_fd,
                recovery_name,
                label="recovery claim",
            ) as recovery_fd:
                recovery_identity = directory_identity(recovery_fd)
                claim = read_json_at(
                    recovery_fd,
                    ".intent-claim.json",
                    label="recovery claim",
                )
                if claim != intent:
                    raise ValueError("interrupted archive target claim differs")
                with opened_directory_at(
                    recovery_fd,
                    "archive",
                    label="archive safe directory",
                ) as archive_fd:
                    anchors = RecoveryAnchors(
                        final_fd=final_fd,
                        final_identity=final_identity,
                        interrupted_fd=interrupted_fd,
                        interrupted_identity=interrupted_identity,
                        attempt_fd=attempt_fd,
                        attempt_name=attempt_id,
                        attempt_identity=attempt_identity,
                        recovery_fd=recovery_fd,
                        recovery_name=recovery_name,
                        recovery_identity=recovery_identity,
                        archive_fd=archive_fd,
                        archive_identity=directory_identity(archive_fd),
                    )
                    assert_recovery_anchors(anchors)
                    yield anchors


def _resolve_or_publish_claim_at(
    attempt_fd: int,
    recovery_name: str,
    *,
    intent: Mapping[str, object],
) -> None:
    if reject_unsafe_optional_directory_at(
        attempt_fd,
        recovery_name,
        label="recovery claim",
    ):
        with opened_directory_at(
            attempt_fd,
            recovery_name,
            label="recovery claim",
        ) as recovery_fd:
            try:
                payload = read_json_at(
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
    temporary_name = f".{recovery_name}.{uuid4().hex}.tmp"
    os.mkdir(temporary_name, mode=0o700, dir_fd=attempt_fd)
    temporary_fd = open_directory_at(
        attempt_fd,
        temporary_name,
        label="temporary recovery claim",
    )
    published = False
    try:
        write_bytes_exclusive_at(
            temporary_fd,
            ".intent-claim.json",
            json_bytes(intent),
        )
        atomic_rename_no_replace_at(
            attempt_fd,
            temporary_name,
            attempt_fd,
            recovery_name,
        )
        published = True
    finally:
        if not published:
            try:
                os.unlink(".intent-claim.json", dir_fd=temporary_fd)
            except FileNotFoundError:
                pass
        os.close(temporary_fd)
        if not published:
            try:
                os.rmdir(temporary_name, dir_fd=attempt_fd)
            except FileNotFoundError:
                pass


@contextmanager
def _opened_or_created_directory_at(
    parent_fd: int,
    name: str,
    *,
    label: str,
) -> Iterator[tuple[int, tuple[int, int]]]:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    with opened_directory_at(parent_fd, name, label=label) as descriptor:
        identity = directory_identity(descriptor)
        assert_directory_entry(
            parent_fd,
            name,
            expected=identity,
            label=label,
        )
        yield descriptor, identity
