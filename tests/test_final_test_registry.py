from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
from threading import Event

import pytest

from ashare_multifactor.final_test.registry import (
    append_attempt_outcome,
    append_attempt_state,
    register_attempt,
    resolve_attempt_state,
)
from ashare_multifactor.final_test import registry as registry_module


def _register(registry: Path, attempt_id: str = "attempt-001") -> None:
    register_attempt(
        registry,
        attempt_id=attempt_id,
        git_commit="a" * 40,
        git_tree="b" * 40,
        token_sha256="c" * 64,
        sealed_protocol_sha256="d" * 64,
        robustness_release="stage8-release",
        approval_id="approval-001",
        robustness_manifest_sha256="e" * 64,
        robustness_lineage_sha256="f" * 64,
    )


def _awaiting_official_evidence(registry: Path) -> None:
    append_attempt_state(registry, attempt_id="attempt-001", state="preparing")
    append_attempt_state(
        registry,
        attempt_id="attempt-001",
        state="awaiting_official_evidence",
        identities={"prepare_manifest_sha256": "a" * 64},
    )


def test_readonly_attempt_state_rejects_missing_lock_without_creating_files(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "attempts"
    _register(registry)
    before = {path.name: path.read_bytes() for path in registry.iterdir()}

    with pytest.raises(ValueError, match="lock"):
        registry_module.resolve_attempt_state_readonly(registry, "attempt-001")

    assert {path.name: path.read_bytes() for path in registry.iterdir()} == before


def test_readonly_attempt_state_opens_existing_lock_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "attempts"
    _register(registry)
    _awaiting_official_evidence(registry)
    original_open = os.open
    lock_flags: list[int] = []

    def capture_open(path: Path, flags: int, *args: object) -> int:
        if Path(path).name == "attempt-001.lock":
            lock_flags.append(flags)
        return original_open(path, flags, *args)

    monkeypatch.setattr(registry_module.os, "open", capture_open)

    state = registry_module.resolve_attempt_state_readonly(registry, "attempt-001")

    assert state["state"] == "awaiting_official_evidence"
    assert lock_flags == [os.O_RDONLY]


def test_attempt_state_events_are_append_only_and_resume_claim_is_exclusive(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "attempts"
    _register(registry)
    _awaiting_official_evidence(registry)
    append_attempt_state(
        registry,
        attempt_id="attempt-001",
        state="executing",
        identities={
            "prepare_manifest_sha256": "a" * 64,
            "security_event_coverage_sha256": "b" * 64,
            "corporate_action_coverage_sha256": "c" * 64,
        },
    )

    assert resolve_attempt_state(registry, "attempt-001")["state"] == "executing"
    with pytest.raises(ValueError, match="state transition"):
        append_attempt_state(
            registry,
            attempt_id="attempt-001",
            state="executing",
            identities={"prepare_manifest_sha256": "a" * 64},
        )


def test_attempt_state_rejects_skipping_preparing(tmp_path: Path) -> None:
    registry = tmp_path / "attempts"
    _register(registry)

    with pytest.raises(ValueError, match="state transition"):
        append_attempt_state(
            registry,
            attempt_id="attempt-001",
            state="awaiting_official_evidence",
            identities={"prepare_manifest_sha256": "a" * 64},
        )


def test_attempt_state_rejects_returning_to_preparing(tmp_path: Path) -> None:
    registry = tmp_path / "attempts"
    _register(registry)
    _awaiting_official_evidence(registry)

    with pytest.raises(ValueError, match="state transition"):
        append_attempt_state(registry, attempt_id="attempt-001", state="preparing")


def test_attempt_state_rejects_append_after_terminal_outcome(tmp_path: Path) -> None:
    registry = tmp_path / "attempts"
    _register(registry)
    append_attempt_outcome(
        registry,
        attempt_id="attempt-001",
        status="failed",
        authoritative=False,
        reason="preparation failed",
    )

    assert resolve_attempt_state(registry, "attempt-001")["state"] == "failed"
    with pytest.raises(ValueError, match="state transition"):
        append_attempt_state(registry, attempt_id="attempt-001", state="preparing")


def test_attempt_state_rejects_incomplete_identity_hashes(tmp_path: Path) -> None:
    registry = tmp_path / "attempts"
    _register(registry)
    append_attempt_state(registry, attempt_id="attempt-001", state="preparing")

    with pytest.raises(ValueError, match="identity"):
        append_attempt_state(
            registry,
            attempt_id="attempt-001",
            state="awaiting_official_evidence",
            identities={"prepare_manifest_sha256": "not-a-sha256"},
        )


def test_attempt_state_rejects_modified_event_contents(tmp_path: Path) -> None:
    registry = tmp_path / "attempts"
    _register(registry)
    append_attempt_state(registry, attempt_id="attempt-001", state="preparing")
    event = registry / "attempt-001.state.01-preparing.json"
    payload = json.loads(event.read_text(encoding="utf-8"))
    payload["state"] = "awaiting_official_evidence"
    event.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="state event"):
        resolve_attempt_state(registry, "attempt-001")


def test_attempt_state_rejects_duplicate_or_missing_sequences(tmp_path: Path) -> None:
    registry = tmp_path / "attempts"
    _register(registry)
    append_attempt_state(registry, attempt_id="attempt-001", state="preparing")
    duplicate = registry / "attempt-001.state.01-awaiting_official_evidence.json"
    duplicate.write_text(
        json.dumps(
            {
                "attempt_id": "attempt-001",
                "sequence": 1,
                "state": "preparing",
                "recorded_at": "2026-07-17T00:00:00+00:00",
                "identities": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate state sequence"):
        resolve_attempt_state(registry, "attempt-001")

    duplicate.unlink()
    (registry / "attempt-001.state.01-preparing.json").unlink()
    missing = registry / "attempt-001.state.02-awaiting_official_evidence.json"
    missing.write_text(
        json.dumps(
            {
                "attempt_id": "attempt-001",
                "sequence": 2,
                "state": "awaiting_official_evidence",
                "recorded_at": "2026-07-17T00:00:00+00:00",
                "identities": {"prepare_manifest_sha256": "a" * 64},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing state sequence"):
        resolve_attempt_state(registry, "attempt-001")


def test_attempt_state_rejects_illegal_event_filename_and_terminal_conflict(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "attempts"
    _register(registry)
    (registry / "attempt-001.state.1-preparing.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="state event filename"):
        resolve_attempt_state(registry, "attempt-001")

    (registry / "attempt-001.state.1-preparing.json").unlink()
    outcome = registry / "attempt-001.outcome.json"
    outcome.write_text(
        json.dumps(
            {
                "attempt_id": "attempt-001",
                "status": "succeeded",
                "authoritative": False,
                "reason": "tampered terminal outcome",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="terminal outcome"):
        resolve_attempt_state(registry, "attempt-001")


def test_attempt_outcome_rejects_invalid_release_hash_before_writing(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "attempts"
    _register(registry)

    with pytest.raises(ValueError, match="terminal outcome"):
        append_attempt_outcome(
            registry,
            attempt_id="attempt-001",
            status="succeeded",
            authoritative=True,
            reason="published",
            release_run_id="final-release",
            release_manifest_sha256="not-a-sha256",
        )

    assert not (registry / "attempt-001.outcome.json").exists()


def test_executing_state_requires_awaiting_prepare_manifest_identity(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "attempts"
    _register(registry)
    _awaiting_official_evidence(registry)

    with pytest.raises(ValueError, match="prepare manifest"):
        append_attempt_state(
            registry,
            attempt_id="attempt-001",
            state="executing",
            identities={
                "prepare_manifest_sha256": "d" * 64,
                "security_event_coverage_sha256": "b" * 64,
                "corporate_action_coverage_sha256": "c" * 64,
            },
        )

    assert not (registry / "attempt-001.state.03-executing.json").exists()


def test_attempt_state_rejects_tampered_executing_prepare_manifest_identity(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "attempts"
    _register(registry)
    _awaiting_official_evidence(registry)
    append_attempt_state(
        registry,
        attempt_id="attempt-001",
        state="executing",
        identities={
            "prepare_manifest_sha256": "a" * 64,
            "security_event_coverage_sha256": "b" * 64,
            "corporate_action_coverage_sha256": "c" * 64,
        },
    )
    event = registry / "attempt-001.state.03-executing.json"
    payload = json.loads(event.read_text(encoding="utf-8"))
    payload["identities"]["prepare_manifest_sha256"] = "d" * 64
    event.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="prepare manifest"):
        resolve_attempt_state(registry, "attempt-001")


def test_attempt_state_rejects_sequence_beyond_fixed_state_table(tmp_path: Path) -> None:
    registry = tmp_path / "attempts"
    _register(registry)
    _awaiting_official_evidence(registry)
    append_attempt_state(
        registry,
        attempt_id="attempt-001",
        state="executing",
        identities={
            "prepare_manifest_sha256": "a" * 64,
            "security_event_coverage_sha256": "b" * 64,
            "corporate_action_coverage_sha256": "c" * 64,
        },
    )
    (registry / "attempt-001.state.04-executing.json").write_text(
        json.dumps(
            {
                "attempt_id": "attempt-001",
                "sequence": 4,
                "state": "executing",
                "recorded_at": "2026-07-17T00:00:00+00:00",
                "identities": {
                    "prepare_manifest_sha256": "a" * 64,
                    "security_event_coverage_sha256": "b" * 64,
                    "corporate_action_coverage_sha256": "c" * 64,
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid final-test state event"):
        resolve_attempt_state(registry, "attempt-001")


@pytest.mark.parametrize("state", ["preparing", "awaiting"])
def test_attempt_outcome_rejects_succeeded_before_executing(
    tmp_path: Path, state: str
) -> None:
    registry = tmp_path / "attempts"
    _register(registry)
    if state == "preparing":
        append_attempt_state(registry, attempt_id="attempt-001", state="preparing")
    elif state == "awaiting":
        _awaiting_official_evidence(registry)

    with pytest.raises(ValueError, match="succeeded outcome requires executing"):
        append_attempt_outcome(
            registry,
            attempt_id="attempt-001",
            status="succeeded",
            authoritative=True,
            reason="published",
            release_run_id="final-release",
            release_manifest_sha256="a" * 64,
        )

    assert not (registry / "attempt-001.outcome.json").exists()


def test_legacy_attempt_without_state_events_accepts_succeeded_outcome(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "attempts"
    _register(registry)

    append_attempt_outcome(
        registry,
        attempt_id="attempt-001",
        status="succeeded",
        authoritative=True,
        reason="legacy publication",
        release_run_id="final-release",
        release_manifest_sha256="a" * 64,
    )

    assert resolve_attempt_state(registry, "attempt-001")["state"] == "published"


@pytest.mark.parametrize("state", ["registered", "awaiting"])
def test_attempt_outcome_accepts_early_failed_state(tmp_path: Path, state: str) -> None:
    registry = tmp_path / "attempts"
    _register(registry)
    if state == "awaiting":
        _awaiting_official_evidence(registry)

    append_attempt_outcome(
        registry,
        attempt_id="attempt-001",
        status="failed",
        authoritative=False,
        reason="identity verification failed",
    )

    assert resolve_attempt_state(registry, "attempt-001")["state"] == "failed"


def test_attempt_state_rejects_injected_succeeded_outcome_before_executing(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "attempts"
    _register(registry)
    _awaiting_official_evidence(registry)
    (registry / "attempt-001.outcome.json").write_text(
        json.dumps(
            {
                "attempt_id": "attempt-001",
                "recorded_at": "2026-07-17T00:00:00+00:00",
                "status": "succeeded",
                "authoritative": True,
                "reason": "published",
                "release_run_id": "final-release",
                "release_manifest_sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="succeeded outcome requires executing"):
        resolve_attempt_state(registry, "attempt-001")


def test_attempt_transition_serializes_legacy_success_and_first_state_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "attempts"
    _register(registry)
    original = registry_module._is_legacy_attempt_without_state_events
    original_resolve = registry_module._resolve_attempt_state_unlocked
    legacy_checked = Event()
    release_legacy_write = Event()
    preparing_entered = Event()

    def pause_after_legacy_check(registry_root: Path, attempt_id: str) -> bool:
        legacy = original(registry_root, attempt_id)
        legacy_checked.set()
        if not release_legacy_write.wait(timeout=3):
            raise RuntimeError("concurrent legacy attempt did not resume")
        return legacy

    monkeypatch.setattr(
        registry_module,
        "_is_legacy_attempt_without_state_events",
        pause_after_legacy_check,
    )

    def signal_preparing_resolution(registry_root: Path, attempt_id: str) -> dict:
        preparing_entered.set()
        return original_resolve(registry_root, attempt_id)

    monkeypatch.setattr(
        registry_module, "_resolve_attempt_state_unlocked", signal_preparing_resolution
    )

    def append_legacy_success() -> str:
        try:
            append_attempt_outcome(
                registry,
                attempt_id="attempt-001",
                status="succeeded",
                authoritative=True,
                reason="legacy publication",
                release_run_id="final-release",
                release_manifest_sha256="a" * 64,
            )
        except ValueError:
            return "rejected"
        return "succeeded"

    def append_preparing() -> str:
        try:
            append_attempt_state(
                registry, attempt_id="attempt-001", state="preparing"
            )
        except ValueError:
            return "rejected"
        return "preparing"

    with ThreadPoolExecutor(max_workers=2) as executor:
        legacy = executor.submit(append_legacy_success)
        assert legacy_checked.wait(timeout=3)
        preparing = executor.submit(append_preparing)
        try:
            assert not preparing_entered.wait(timeout=0.2)
        finally:
            release_legacy_write.set()
        results = {legacy.result(timeout=3), preparing.result(timeout=3)}

    assert results == {"succeeded", "rejected"}
    assert resolve_attempt_state(registry, "attempt-001")["state"] == "published"


def test_attempt_transition_lock_releases_after_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "attempts"
    _register(registry)

    def fail_write(path: Path, payload: bytes) -> None:
        raise OSError("synthetic write failure")

    monkeypatch.setattr(registry_module, "_write_exclusive", fail_write)
    with pytest.raises(OSError, match="synthetic write failure"):
        append_attempt_state(registry, attempt_id="attempt-001", state="preparing")

    descriptor = os.open(registry / "attempt-001.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_resolve_attempt_state_waits_for_in_progress_state_event_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "attempts"
    _register(registry)
    event_created = Event()
    release_write = Event()
    resolver_finished = Event()

    def paused_exclusive_write(path: Path, payload: bytes) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            event_created.set()
            if not release_write.wait(timeout=3):
                raise RuntimeError("state event write did not resume")
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    monkeypatch.setattr(registry_module, "_write_exclusive", paused_exclusive_write)

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(
            append_attempt_state,
            registry,
            attempt_id="attempt-001",
            state="preparing",
        )
        assert event_created.wait(timeout=3)

        def resolve_after_write() -> dict[str, object]:
            try:
                return resolve_attempt_state(registry, "attempt-001")
            finally:
                resolver_finished.set()

        resolver = executor.submit(resolve_after_write)
        try:
            assert not resolver_finished.wait(timeout=0.2)
        finally:
            release_write.set()
        assert writer.result(timeout=3)["state"] == "preparing"
        assert resolver.result(timeout=3)["state"] == "preparing"
