"""Bounded recovery for verified transient collector interruptions."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import signal
from typing import Protocol, TypeVar
from uuid import uuid4

from ashare_multifactor.final_test.official_query_coverage import canonical_json_bytes
from ashare_multifactor.final_test.registry import validate_publication_id


T = TypeVar("T")
_RECOVERY_LOG_NAME = "collection_process_recovery.json"
_IDENTITY_FIELDS = {
    "attempt_id",
    "git_commit",
    "git_tree",
    "sealed_protocol_sha256",
    "prepare_manifest_sha256",
}
_GIT_OBJECT = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class TransientCollectionInterruption(RuntimeError):
    """A network or process interruption eligible for same-identity recovery."""


@dataclass(frozen=True)
class RecoveryEvent:
    restart_number: int
    reason: str


@dataclass(frozen=True)
class ControlledRecoveryResult:
    value: object
    restart_count: int
    events: tuple[RecoveryEvent, ...]


class CollectionProcess(Protocol):
    """Minimal child-process interface required by the supervisor."""

    pid: int

    def wait(self) -> int: ...


@dataclass(frozen=True)
class ProcessRecoveryEvent:
    kind: str
    pid: int
    predecessor_pid: int | None
    returncode: int | None
    reason: str
    observed_at: datetime


@dataclass(frozen=True)
class ProcessRecoveryResult:
    final_pid: int
    restart_count: int
    events: tuple[ProcessRecoveryEvent, ...]


class CollectionProcessExit(RuntimeError):
    """A collector process exit that the bounded supervisor must not retry."""

    def __init__(
        self,
        *,
        pid: int,
        returncode: int,
        restart_count: int,
        events: tuple[ProcessRecoveryEvent, ...],
    ) -> None:
        super().__init__(
            f"official collector process {pid} exited with status {returncode}"
        )
        self.pid = pid
        self.returncode = returncode
        self.restart_count = restart_count
        self.events = events


def run_with_controlled_recovery(
    operation: Callable[[], T],
    *,
    verify_identity: Callable[[], None],
    maximum_restarts: int = 2,
) -> ControlledRecoveryResult:
    """Retry only declared transient interruptions after exact identity revalidation."""
    if (
        not callable(operation)
        or not callable(verify_identity)
        or not isinstance(maximum_restarts, int)
        or isinstance(maximum_restarts, bool)
        or maximum_restarts != 2
    ):
        raise ValueError("official collection recovery policy is invalid")
    events: list[RecoveryEvent] = []
    while True:
        try:
            value = operation()
        except TransientCollectionInterruption as error:
            if len(events) >= maximum_restarts:
                raise
            verify_identity()
            events.append(
                RecoveryEvent(
                    restart_number=len(events) + 1,
                    reason=str(error),
                )
            )
            continue
        return ControlledRecoveryResult(
            value=value,
            restart_count=len(events),
            events=tuple(events),
        )


def run_with_process_recovery(
    start_process: Callable[[], CollectionProcess],
    *,
    verify_identity: Callable[[], None],
    event_observer: Callable[[ProcessRecoveryEvent], None] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    maximum_restarts: int = 2,
) -> ProcessRecoveryResult:
    """Restart a signal-terminated collector after exact identity revalidation."""
    if (
        not callable(start_process)
        or not callable(verify_identity)
        or (event_observer is not None and not callable(event_observer))
        or not callable(now)
        or not isinstance(maximum_restarts, int)
        or isinstance(maximum_restarts, bool)
        or maximum_restarts != 2
    ):
        raise ValueError("official collection process recovery policy is invalid")

    events: list[ProcessRecoveryEvent] = []

    def observe(event: ProcessRecoveryEvent) -> None:
        events.append(event)
        if event_observer is not None:
            event_observer(event)

    process = _start_verified_process(start_process)
    observe(
        ProcessRecoveryEvent(
            kind="launch",
            pid=process.pid,
            predecessor_pid=None,
            returncode=None,
            reason="initial_launch",
            observed_at=_aware(now()),
        )
    )
    restart_count = 0
    while True:
        returncode = _returncode(process.wait())
        observe(
            ProcessRecoveryEvent(
                kind="exit",
                pid=process.pid,
                predecessor_pid=None,
                returncode=returncode,
                reason="completed" if returncode == 0 else f"exit_status:{returncode}",
                observed_at=_aware(now()),
            )
        )
        if returncode == 0:
            return ProcessRecoveryResult(
                final_pid=process.pid,
                restart_count=restart_count,
                events=tuple(events),
            )
        if (
            not _eligible_signal_termination(returncode)
            or restart_count >= maximum_restarts
        ):
            raise CollectionProcessExit(
                pid=process.pid,
                returncode=returncode,
                restart_count=restart_count,
                events=tuple(events),
            )

        verify_identity()
        predecessor_pid = process.pid
        process = _start_verified_process(start_process)
        restart_count += 1
        observe(
            ProcessRecoveryEvent(
                kind="launch",
                pid=process.pid,
                predecessor_pid=predecessor_pid,
                returncode=None,
                reason=f"signal_recovery:{-returncode}",
                observed_at=_aware(now()),
            )
        )


def write_process_recovery_log(
    data_root: Path,
    *,
    attempt_id: str,
    identity: dict[str, str],
    status: str,
    restart_count: int,
    events: Sequence[ProcessRecoveryEvent],
) -> Path:
    """Atomically persist the supervisor history outside immutable evidence."""
    validate_publication_id(attempt_id)
    if status not in {"running", "interrupted", "complete", "failed"}:
        raise ValueError("official collection process status is invalid")
    if (
        not isinstance(restart_count, int)
        or isinstance(restart_count, bool)
        or restart_count < 0
        or restart_count > 2
    ):
        raise ValueError("official collection process restart count is invalid")
    validated_identity = _validated_identity(identity, attempt_id=attempt_id)
    serialized_events = [_serialized_event(event) for event in events]
    payload = canonical_json_bytes(
        {
            "schema_version": "1",
            "role": "official_collection_process_recovery",
            "identity": validated_identity,
            "status": status,
            "restart_count": restart_count,
            "events": serialized_events,
        }
    )

    processed_fd = os.open(
        data_root / "processed",
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    runtime_fd: int | None = None
    attempt_fd: int | None = None
    try:
        runtime_fd = _open_or_create_directory(processed_fd, "final_test_runtime")
        attempt_fd = _open_or_create_directory(runtime_fd, attempt_id)
        temporary = f".{_RECOVERY_LOG_NAME}.{uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=attempt_fd,
        )
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.replace(
                temporary,
                _RECOVERY_LOG_NAME,
                src_dir_fd=attempt_fd,
                dst_dir_fd=attempt_fd,
            )
            os.fsync(attempt_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=attempt_fd)
            except FileNotFoundError:
                pass
    finally:
        if attempt_fd is not None:
            os.close(attempt_fd)
        if runtime_fd is not None:
            os.close(runtime_fd)
        os.close(processed_fd)
    return (
        data_root
        / "processed/final_test_runtime"
        / attempt_id
        / _RECOVERY_LOG_NAME
    )


def _start_verified_process(
    start_process: Callable[[], CollectionProcess],
) -> CollectionProcess:
    process = start_process()
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise TypeError("official collection process identity is invalid")
    if not callable(getattr(process, "wait", None)):
        raise TypeError("official collection process interface is invalid")
    return process


def _returncode(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("official collection process return code is invalid")
    return value


def _eligible_signal_termination(returncode: int) -> bool:
    eligible = {
        signal.SIGHUP,
        signal.SIGKILL,
        signal.SIGPIPE,
        signal.SIGTERM,
    }
    return returncode < 0 and -returncode in eligible


def _open_or_create_directory(parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    try:
        return os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except OSError as error:
        raise ValueError("official collection runtime directory is unsafe") from error


def _validated_identity(
    identity: dict[str, str],
    *,
    attempt_id: str,
) -> dict[str, str]:
    if not isinstance(identity, dict) or set(identity) != _IDENTITY_FIELDS:
        raise ValueError("official collection process identity is invalid")
    if identity.get("attempt_id") != attempt_id:
        raise ValueError("official collection process identity differs")
    if _GIT_OBJECT.fullmatch(identity.get("git_commit", "")) is None:
        raise ValueError("official collection process identity is invalid")
    if _GIT_OBJECT.fullmatch(identity.get("git_tree", "")) is None:
        raise ValueError("official collection process identity is invalid")
    if _SHA256.fullmatch(identity.get("sealed_protocol_sha256", "")) is None:
        raise ValueError("official collection process identity is invalid")
    if _SHA256.fullmatch(identity.get("prepare_manifest_sha256", "")) is None:
        raise ValueError("official collection process identity is invalid")
    return dict(identity)


def _serialized_event(event: ProcessRecoveryEvent) -> dict[str, object]:
    if event.kind not in {"launch", "exit"}:
        raise ValueError("official collection process event is invalid")
    if not isinstance(event.pid, int) or isinstance(event.pid, bool) or event.pid <= 0:
        raise ValueError("official collection process event is invalid")
    if event.predecessor_pid is not None and (
        not isinstance(event.predecessor_pid, int)
        or isinstance(event.predecessor_pid, bool)
        or event.predecessor_pid <= 0
    ):
        raise ValueError("official collection process event is invalid")
    if event.returncode is not None and (
        not isinstance(event.returncode, int) or isinstance(event.returncode, bool)
    ):
        raise ValueError("official collection process event is invalid")
    if not isinstance(event.reason, str) or not event.reason:
        raise ValueError("official collection process event is invalid")
    return {
        "kind": event.kind,
        "pid": event.pid,
        "predecessor_pid": event.predecessor_pid,
        "returncode": event.returncode,
        "reason": event.reason,
        "observed_at": _aware(event.observed_at).isoformat(),
    }


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("official collection process time is invalid")
    return value
