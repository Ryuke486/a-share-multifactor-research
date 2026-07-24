from __future__ import annotations

from dataclasses import dataclass
import signal

import pytest


def test_recovery_retries_only_two_verified_transient_interruptions() -> None:
    from ashare_multifactor.final_test.official_collection_recovery import (
        TransientCollectionInterruption,
        run_with_controlled_recovery,
    )

    outcomes: list[object] = [
        TransientCollectionInterruption("connection reset"),
        TransientCollectionInterruption("process exited"),
        "complete",
    ]
    identity_checks: list[int] = []

    def operation() -> str:
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return str(outcome)

    result = run_with_controlled_recovery(
        operation,
        verify_identity=lambda: identity_checks.append(1),
    )

    assert result.value == "complete"
    assert result.restart_count == 2
    assert len(result.events) == 2
    assert len(identity_checks) == 2


def test_recovery_stops_on_the_third_interruption_or_unknown_error() -> None:
    from ashare_multifactor.final_test.official_collection_recovery import (
        TransientCollectionInterruption,
        run_with_controlled_recovery,
    )

    def transient() -> None:
        raise TransientCollectionInterruption("still disconnected")

    with pytest.raises(TransientCollectionInterruption, match="still disconnected"):
        run_with_controlled_recovery(transient, verify_identity=lambda: None)

    def unknown() -> None:
        raise RuntimeError("collector correctness defect")

    with pytest.raises(RuntimeError, match="correctness defect"):
        run_with_controlled_recovery(unknown, verify_identity=lambda: None)


@dataclass
class _Process:
    pid: int
    returncode: int

    def wait(self) -> int:
        return self.returncode


def test_process_supervisor_restarts_two_signal_terminated_collectors() -> None:
    from ashare_multifactor.final_test.official_collection_recovery import (
        run_with_process_recovery,
    )

    processes = iter(
        [
            _Process(pid=101, returncode=-signal.SIGKILL),
            _Process(pid=102, returncode=-signal.SIGTERM),
            _Process(pid=103, returncode=0),
        ]
    )
    identity_checks: list[int] = []
    observed: list[object] = []

    result = run_with_process_recovery(
        lambda: next(processes),
        verify_identity=lambda: identity_checks.append(1),
        event_observer=observed.append,
    )

    assert result.restart_count == 2
    assert result.final_pid == 103
    assert len(identity_checks) == 2
    assert [
        (event.kind, event.pid, event.predecessor_pid, event.returncode)
        for event in observed
    ] == [
        ("launch", 101, None, None),
        ("exit", 101, None, -signal.SIGKILL),
        ("launch", 102, 101, None),
        ("exit", 102, None, -signal.SIGTERM),
        ("launch", 103, 102, None),
        ("exit", 103, None, 0),
    ]


@pytest.mark.parametrize("returncode", [1, -signal.SIGINT])
def test_process_supervisor_never_restarts_unknown_or_user_interrupt_exit(
    returncode: int,
) -> None:
    from ashare_multifactor.final_test.official_collection_recovery import (
        CollectionProcessExit,
        run_with_process_recovery,
    )

    starts: list[int] = []
    identity_checks: list[int] = []

    def start() -> _Process:
        starts.append(1)
        return _Process(pid=201, returncode=returncode)

    with pytest.raises(CollectionProcessExit) as captured:
        run_with_process_recovery(
            start,
            verify_identity=lambda: identity_checks.append(1),
        )

    assert captured.value.pid == 201
    assert captured.value.returncode == returncode
    assert len(starts) == 1
    assert identity_checks == []


def test_process_supervisor_stops_after_third_signal_termination() -> None:
    from ashare_multifactor.final_test.official_collection_recovery import (
        CollectionProcessExit,
        run_with_process_recovery,
    )

    processes = iter(
        [
            _Process(pid=301, returncode=-signal.SIGKILL),
            _Process(pid=302, returncode=-signal.SIGKILL),
            _Process(pid=303, returncode=-signal.SIGKILL),
        ]
    )
    identity_checks: list[int] = []

    with pytest.raises(CollectionProcessExit) as captured:
        run_with_process_recovery(
            lambda: next(processes),
            verify_identity=lambda: identity_checks.append(1),
        )

    assert captured.value.pid == 303
    assert captured.value.returncode == -signal.SIGKILL
    assert len(identity_checks) == 2
