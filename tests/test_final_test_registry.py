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
    bind_execution_identity,
    bind_execution_input_manifest_hash,
    bind_execution_output_intent,
    register_attempt,
    resolve_execution_binding,
    resolve_execution_input_manifest_hash,
    resolve_execution_output_intent,
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


def _execution_identity() -> dict[str, str]:
    return {
        "attempt_id": "attempt-001",
        "execution_id": "execution-001",
        "sealed_protocol_sha256": "d" * 64,
        "prepare_manifest_sha256": "a" * 64,
        "security_event_coverage_sha256": "b" * 64,
        "corporate_action_coverage_sha256": "c" * 64,
        "coverage_snapshot_manifest_sha256": "e" * 64,
    }


def _executing(registry: Path) -> None:
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


def test_execution_binding_is_append_only_and_matches_executing_state(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "attempts"
    _register(registry)
    _executing(registry)
    identity = _execution_identity()

    assert bind_execution_identity(registry, identity=identity) == identity
    assert resolve_execution_binding(registry, attempt_id="attempt-001") == identity
    outputs = {
        "corporate_actions.parquet": {"sha256": "1" * 64, "size_bytes": 10},
        "security_events.parquet": {"sha256": "2" * 64, "size_bytes": 20},
    }
    assert bind_execution_output_intent(
        registry, identity=identity, outputs=outputs
    ) == outputs
    assert resolve_execution_output_intent(
        registry, identity=identity
    ) == outputs
    manifest_sha256 = "f" * 64
    assert bind_execution_input_manifest_hash(
        registry,
        identity=identity,
        manifest_sha256=manifest_sha256,
    ) == manifest_sha256
    assert resolve_execution_input_manifest_hash(
        registry,
        identity=identity,
    ) == manifest_sha256

    changed = {**identity, "execution_id": "other-execution"}
    with pytest.raises(ValueError, match="binding"):
        bind_execution_identity(registry, identity=changed)
    with pytest.raises(ValueError, match="binding"):
        bind_execution_input_manifest_hash(
            registry,
            identity=identity,
            manifest_sha256="0" * 64,
        )
    changed_outputs = {
        **outputs,
        "corporate_actions.parquet": {
            "sha256": "3" * 64,
            "size_bytes": 10,
        },
    }
    with pytest.raises(ValueError, match="intent"):
        bind_execution_output_intent(
            registry,
            identity=identity,
            outputs=changed_outputs,
        )
    assert resolve_execution_binding(registry, attempt_id="attempt-001") == identity


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
    with pytest.raises(ValueError, match="terminal"):
        append_attempt_state(registry, attempt_id="attempt-001", state="preparing")

    with pytest.raises(ValueError, match="terminal"):
        append_attempt_state(registry, attempt_id="attempt-001", state="registered")


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


@pytest.mark.parametrize("state", ["registered", "preparing", "awaiting"])
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
