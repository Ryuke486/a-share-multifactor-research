from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
from uuid import uuid4


_ATTEMPT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_STATE_FILENAME = re.compile(
    r"(?P<attempt_id>[A-Za-z0-9][A-Za-z0-9_.-]{0,127})"
    r"\.state\.(?P<sequence>\d{2})-(?P<state>[a-z_]+)\.json"
)
_STATE_SEQUENCE = {
    "registered": 0,
    "preparing": 1,
    "awaiting_official_evidence": 2,
    "executing": 3,
}
_STATE_BY_SEQUENCE = {sequence: state for state, sequence in _STATE_SEQUENCE.items()}
_STATE_IDENTITIES = {
    "preparing": frozenset(),
    "awaiting_official_evidence": frozenset({"prepare_manifest_sha256"}),
    "executing": frozenset(
        {
            "prepare_manifest_sha256",
            "security_event_coverage_sha256",
            "corporate_action_coverage_sha256",
        }
    ),
}

AttemptStateEvent = dict[str, Any]


def validate_publication_id(value: str) -> str:
    if (
        _ATTEMPT_ID.fullmatch(value) is None
        or value in {".", ".."}
        or ".." in value
        or "/" in value
        or "\\" in value
    ):
        raise ValueError("invalid final-test publication identifier")
    return value


@contextmanager
def _attempt_lock(registry_root: Path, attempt_id: str, operation: int):
    """Hold a shared or exclusive lock for one validated attempt."""
    registry_root.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        registry_root / f"{attempt_id}.lock", os.O_WRONLY | os.O_CREAT, 0o600
    )
    try:
        fcntl.flock(descriptor, operation)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@contextmanager
def _attempt_transition_lock(registry_root: Path, attempt_id: str):
    """Serialize state transitions while retaining exclusive event writes."""
    with _attempt_lock(registry_root, attempt_id, fcntl.LOCK_EX):
        yield


@contextmanager
def _attempt_read_lock(registry_root: Path, attempt_id: str):
    """Share an existing attempt lock without creating any filesystem entry."""
    try:
        descriptor = os.open(registry_root / f"{attempt_id}.lock", os.O_RDONLY)
    except FileNotFoundError as error:
        raise ValueError("final-test attempt lock is missing") from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@contextmanager
def claim_attempt_execution(
    registry_root: Path,
    *,
    attempt_id: str,
    identities: dict[str, str],
):
    """Exclusively claim or recover one execution until its terminal write."""
    validate_publication_id(attempt_id)
    registry_root.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        registry_root / f"{attempt_id}.execution.lock",
        os.O_WRONLY | os.O_CREAT,
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        with _attempt_transition_lock(registry_root, attempt_id):
            current = _resolve_attempt_state_unlocked(registry_root, attempt_id)
            if current["state"] == "awaiting_official_evidence":
                _append_attempt_state_unlocked(
                    registry_root,
                    attempt_id=attempt_id,
                    state="executing",
                    identities=identities,
                )
                recovered = False
            elif current["state"] == "executing":
                validated = _validate_state_identities("executing", identities)
                if current["identities"] != validated:
                    raise ValueError("executing state identity differs from resume preflight")
                recovered = True
            else:
                raise ValueError("invalid final-test state transition")
        yield recovered
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def append_attempt_state(
    registry_root: Path,
    *,
    attempt_id: str,
    state: str,
    identities: dict[str, str] | None = None,
) -> AttemptStateEvent:
    """Append the next immutable state event for one final-test attempt."""
    validate_publication_id(attempt_id)
    with _attempt_transition_lock(registry_root, attempt_id):
        return _append_attempt_state_unlocked(
            registry_root,
            attempt_id=attempt_id,
            state=state,
            identities=identities or {},
        )


def _append_attempt_state_unlocked(
    registry_root: Path,
    *,
    attempt_id: str,
    state: str,
    identities: dict[str, str],
) -> AttemptStateEvent:
    current = _resolve_attempt_state_unlocked(registry_root, attempt_id)
    expected = _STATE_SEQUENCE.get(str(current["state"]), -1) + 1
    if _STATE_SEQUENCE.get(state) != expected:
        raise ValueError("invalid final-test state transition")
    validated_identities = _validate_state_identities(state, identities)
    _validate_state_identity_inheritance(
        state, current["identities"], validated_identities
    )
    payload: AttemptStateEvent = {
        "attempt_id": attempt_id,
        "sequence": expected,
        "state": state,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "identities": validated_identities,
    }
    try:
        _write_exclusive(
            registry_root / f"{attempt_id}.state.{expected:02d}-{state}.json",
            _json_bytes(payload),
        )
    except FileExistsError as exc:
        raise ValueError("invalid final-test state transition") from exc
    return payload


def resolve_attempt_state(registry_root: Path, attempt_id: str) -> dict[str, Any]:
    """Rebuild an attempt state from immutable registration, events, and outcome."""
    validate_publication_id(attempt_id)
    with _attempt_lock(registry_root, attempt_id, fcntl.LOCK_SH):
        return _resolve_attempt_state_unlocked(registry_root, attempt_id)


def resolve_attempt_state_readonly(
    registry_root: Path, attempt_id: str
) -> dict[str, Any]:
    """Resolve an initialized attempt without creating its directory or lock."""
    validate_publication_id(attempt_id)
    with _attempt_read_lock(registry_root, attempt_id):
        return _resolve_attempt_state_unlocked(registry_root, attempt_id)


def _resolve_attempt_state_unlocked(
    registry_root: Path, attempt_id: str
) -> dict[str, Any]:
    """Rebuild attempt state while the caller owns the appropriate attempt lock."""
    record = _read_registry_json(registry_root / f"{attempt_id}.json")
    if (
        record.get("attempt_id") != attempt_id
        or record.get("status") != "registered"
        or record.get("authoritative") is not False
    ):
        raise ValueError("invalid final-test attempt registration")

    events = _load_state_events(registry_root, attempt_id)
    state = "registered"
    identities: dict[str, str] = {}
    for expected_sequence, event in enumerate(events, start=1):
        expected_state = _STATE_BY_SEQUENCE.get(expected_sequence)
        if expected_state is None:
            raise ValueError("invalid final-test state event")
        if event["sequence"] != expected_sequence:
            raise ValueError("missing state sequence")
        if event["state"] != expected_state:
            raise ValueError("invalid final-test state event")
        _validate_state_identity_inheritance(
            expected_state, identities, event["identities"]
        )
        state = expected_state
        identities = event["identities"]

    outcome_path = registry_root / f"{attempt_id}.outcome.json"
    if outcome_path.exists():
        outcome = _read_registry_json(outcome_path)
        _validate_terminal_outcome(outcome, attempt_id)
        if (
            outcome["status"] == "succeeded"
            and state != "executing"
        ):
            raise ValueError("succeeded outcome requires executing state")
        state = "published" if outcome["status"] == "succeeded" else "failed"
    resolved = dict(record)
    resolved.update(
        {
            "state": state,
            "sequence": _STATE_SEQUENCE.get(state, len(events)),
            "identities": identities,
        }
    )
    return resolved


def _load_state_events(registry_root: Path, attempt_id: str) -> list[AttemptStateEvent]:
    events_by_sequence: dict[int, AttemptStateEvent] = {}
    prefix = f"{attempt_id}.state."
    paths_by_sequence: dict[int, tuple[Path, re.Match[str]]] = {}
    for path in sorted(registry_root.glob(f"{prefix}*")):
        match = _STATE_FILENAME.fullmatch(path.name)
        if match is None or match["attempt_id"] != attempt_id:
            raise ValueError("invalid final-test state event filename")
        sequence = int(match["sequence"])
        if sequence in paths_by_sequence:
            raise ValueError("duplicate state sequence")
        paths_by_sequence[sequence] = (path, match)
    for sequence, (path, match) in sorted(paths_by_sequence.items()):
        event = _read_registry_json(path)
        if (
            event.get("attempt_id") != attempt_id
            or event.get("sequence") != sequence
            or event.get("state") != match["state"]
            or not isinstance(event.get("recorded_at"), str)
            or not event["recorded_at"]
            or not isinstance(event.get("identities"), dict)
        ):
            raise ValueError("invalid final-test state event")
        event["identities"] = _validate_state_identities(
            str(event["state"]), event["identities"]
        )
        events_by_sequence[sequence] = event
    return [events_by_sequence[sequence] for sequence in sorted(events_by_sequence)]


def _validate_state_identities(
    state: str, identities: dict[str, str]
) -> dict[str, str]:
    required = _STATE_IDENTITIES.get(state)
    if required is None or set(identities) != required:
        raise ValueError("invalid final-test state identity")
    if any(
        not isinstance(value, str) or _SHA256.fullmatch(value) is None
        for value in identities.values()
    ):
        raise ValueError("invalid final-test state identity")
    return dict(identities)


def _validate_state_identity_inheritance(
    state: str,
    previous: dict[str, str],
    identities: dict[str, str],
) -> None:
    if state == "executing" and (
        identities["prepare_manifest_sha256"]
        != previous.get("prepare_manifest_sha256")
    ):
        raise ValueError("executing state prepare manifest identity differs")


def _validate_terminal_outcome(outcome: dict[str, Any], attempt_id: str) -> None:
    status = outcome.get("status")
    authoritative = outcome.get("authoritative")
    if (
        outcome.get("attempt_id") != attempt_id
        or status not in {"failed", "succeeded"}
        or authoritative is not (status == "succeeded")
        or not isinstance(outcome.get("reason"), str)
        or not outcome["reason"].strip()
    ):
        raise ValueError("invalid final-test terminal outcome")
    release_run_id = outcome.get("release_run_id")
    release_manifest_sha256 = outcome.get("release_manifest_sha256")
    if (
        release_run_id is not None
        and (not isinstance(release_run_id, str) or not release_run_id)
    ) or (
        release_manifest_sha256 is not None
        and (
            not isinstance(release_manifest_sha256, str)
            or _SHA256.fullmatch(release_manifest_sha256) is None
        )
    ):
        raise ValueError("invalid final-test terminal outcome")
    if status == "succeeded" and (
        release_run_id is None or release_manifest_sha256 is None
    ):
        raise ValueError("invalid final-test terminal outcome")


def _read_registry_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid final-test registry record: {path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"invalid final-test registry record: {path.name}")
    return value


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def assert_no_authoritative_success(registry_root: Path) -> None:
    if not registry_root.exists():
        return
    for path in sorted(registry_root.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid final-test registry record: {path.name}") from exc
        if record.get("status") == "succeeded" and record.get("authoritative") is True:
            raise ValueError("authoritative final-test run already succeeded")


def append_attempt_outcome(
    registry_root: Path,
    *,
    attempt_id: str,
    status: str,
    authoritative: bool,
    reason: str,
    release_run_id: str | None = None,
    release_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Append one terminal outcome without rewriting the opening attempt record."""
    validate_publication_id(attempt_id)
    with _attempt_transition_lock(registry_root, attempt_id):
        if status not in {"failed", "succeeded"}:
            raise ValueError("invalid final-test attempt outcome")
        if authoritative != (status == "succeeded"):
            raise ValueError("only a succeeded final-test outcome can be authoritative")
        if not reason.strip():
            raise ValueError("final-test attempt outcome reason is required")
        if authoritative and (not release_run_id or not release_manifest_sha256):
            raise ValueError("authoritative outcome requires release identity")
        payload: dict[str, Any] = {
            "attempt_id": attempt_id,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "authoritative": authoritative,
            "reason": reason.strip(),
        }
        if release_run_id is not None:
            payload["release_run_id"] = release_run_id
        if release_manifest_sha256 is not None:
            payload["release_manifest_sha256"] = release_manifest_sha256
        _validate_terminal_outcome(payload, attempt_id)
        if (
            status == "succeeded"
            and _resolve_attempt_state_unlocked(registry_root, attempt_id)["state"]
            != "executing"
        ):
            raise ValueError("succeeded outcome requires executing state")
        destination = registry_root / f"{attempt_id}.outcome.json"
        try:
            _write_exclusive(
                destination,
                _json_bytes(payload),
            )
        except FileExistsError as exc:
            raise ValueError("final-test attempt outcome is already recorded") from exc
        return payload


def register_attempt(
    registry_root: Path,
    *,
    attempt_id: str,
    git_commit: str,
    sealed_protocol_sha256: str,
    robustness_release: str,
    approval_id: str,
    git_tree: str,
    token_sha256: str,
    robustness_manifest_sha256: str,
    robustness_lineage_sha256: str,
) -> dict[str, Any]:
    """Create one immutable attempt record; existing records are never rewritten."""
    validate_publication_id(attempt_id)
    assert_no_authoritative_success(registry_root)
    registry_root.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "attempt_id": attempt_id,
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit,
        "git_tree": git_tree,
        "token_sha256": token_sha256,
        "sealed_protocol_sha256": sealed_protocol_sha256,
        "robustness_release": robustness_release,
        "robustness_manifest_sha256": robustness_manifest_sha256,
        "robustness_lineage_sha256": robustness_lineage_sha256,
        "approval_id": approval_id,
        "status": "registered",
        "authoritative": False,
    }
    destination = registry_root / f"{attempt_id}.json"
    try:
        _write_exclusive(
            destination,
            (json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
        )
    except FileExistsError as exc:
        raise ValueError("final-test attempt ID is already registered") from exc
    return record


def append_prepared_publication(
    registry_root: Path,
    *,
    attempt_id: str,
    release_run_id: str,
    sealed_protocol_sha256: str,
) -> dict[str, Any]:
    validate_publication_id(attempt_id)
    validate_publication_id(release_run_id)
    if _SHA256.fullmatch(sealed_protocol_sha256) is None:
        raise ValueError("prepared publication identity differs")
    destination = registry_root / f"{attempt_id}.prepared.json"
    expected = {
        "attempt_id": attempt_id,
        "status": "prepared",
        "authoritative": False,
        "release_run_id": release_run_id,
        "sealed_protocol_sha256": sealed_protocol_sha256,
    }
    if destination.exists():
        existing = _read_registry_json(destination)
        if _prepared_publication_matches(existing, expected):
            return existing
        raise ValueError("prepared publication identity differs")
    payload = {
        **expected,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    registry_root.mkdir(parents=True, exist_ok=True)
    try:
        _write_exclusive(
            destination,
            (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(),
        )
    except FileExistsError:
        existing = _read_registry_json(destination)
        if _prepared_publication_matches(existing, expected):
            return existing
        raise ValueError("prepared publication identity differs") from None
    return payload


def _prepared_publication_matches(
    payload: dict[str, Any], expected: dict[str, Any]
) -> bool:
    return (
        set(payload) == {*expected, "recorded_at"}
        and all(payload.get(key) == value for key, value in expected.items())
        and isinstance(payload.get("recorded_at"), str)
        and bool(payload["recorded_at"])
    )


def resolve_prepared_publication(
    registry_root: Path,
    *,
    attempt_id: str,
    release_run_id: str,
    sealed_protocol_sha256: str,
) -> dict[str, Any]:
    """Resolve one prepared record only when its complete immutable schema matches."""
    validate_publication_id(attempt_id)
    validate_publication_id(release_run_id)
    expected = {
        "attempt_id": attempt_id,
        "status": "prepared",
        "authoritative": False,
        "release_run_id": release_run_id,
        "sealed_protocol_sha256": sealed_protocol_sha256,
    }
    try:
        prepared = _read_registry_json(
            registry_root / f"{attempt_id}.prepared.json"
        )
    except FileNotFoundError as error:
        raise ValueError("prepared final-test publication is missing") from error
    if (
        _SHA256.fullmatch(sealed_protocol_sha256) is None
        or not _prepared_publication_matches(prepared, expected)
    ):
        raise ValueError("prepared final-test publication identity differs")
    return prepared


def begin_execution_recovery(
    registry_root: Path,
    *,
    attempt_id: str,
    execution_id: str,
    identities: dict[str, str],
) -> dict[str, Any]:
    """Claim one immutable recovery intent, or return its incomplete predecessor."""
    validate_publication_id(attempt_id)
    validate_publication_id(execution_id)
    validated = _validate_state_identities("executing", identities)
    with _attempt_transition_lock(registry_root, attempt_id):
        state = _resolve_attempt_state_unlocked(registry_root, attempt_id)
        if state["state"] != "executing" or state["identities"] != validated:
            raise ValueError("execution recovery identity differs from executing attempt")
        pending = _pending_execution_recovery_unlocked(registry_root, attempt_id)
        if pending is not None:
            if (
                pending.get("execution_id") != execution_id
                or pending.get("identities") != validated
            ):
                raise ValueError("pending execution recovery identity differs")
            return pending
        recovery_id = uuid4().hex
        payload: dict[str, Any] = {
            "attempt_id": attempt_id,
            "recovery_id": recovery_id,
            "execution_id": execution_id,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "source_path": f"attempt_runs/{attempt_id}",
            "recovery_root": f"interrupted_runs/{attempt_id}/{recovery_id}",
            "archived_path": f"interrupted_runs/{attempt_id}/{recovery_id}/archive",
            "identities": validated,
            "status": "intent",
        }
        _write_exclusive(
            registry_root / f"{attempt_id}.recovery.{recovery_id}.intent.json",
            _json_bytes(payload),
        )
        return payload


def pending_execution_recovery(
    registry_root: Path, attempt_id: str
) -> dict[str, Any] | None:
    validate_publication_id(attempt_id)
    with _attempt_lock(registry_root, attempt_id, fcntl.LOCK_SH):
        return _pending_execution_recovery_unlocked(registry_root, attempt_id)


def _pending_execution_recovery_unlocked(
    registry_root: Path, attempt_id: str
) -> dict[str, Any] | None:
    pending: list[dict[str, Any]] = []
    for path in sorted(registry_root.glob(f"{attempt_id}.recovery.*.intent.json")):
        payload = _read_registry_json(path)
        recovery_id = _validate_execution_recovery_intent(
            payload, attempt_id=attempt_id, path=path
        )
        complete = registry_root / f"{attempt_id}.recovery.{recovery_id}.complete.json"
        if complete.exists():
            continue
        pending.append(payload)
    if len(pending) > 1:
        raise ValueError("multiple incomplete execution recovery intents")
    return pending[0] if pending else None


def _validate_execution_recovery_intent(
    payload: dict[str, Any], *, attempt_id: str, path: Path
) -> str:
    expected_keys = {
        "attempt_id",
        "recovery_id",
        "execution_id",
        "recorded_at",
        "source_path",
        "recovery_root",
        "archived_path",
        "identities",
        "status",
    }
    try:
        recovery_id = validate_publication_id(str(payload.get("recovery_id", "")))
        validate_publication_id(str(payload.get("execution_id", "")))
        identities = _validate_state_identities("executing", payload["identities"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid execution recovery intent") from error
    expected_paths = {
        "source_path": f"attempt_runs/{attempt_id}",
        "recovery_root": f"interrupted_runs/{attempt_id}/{recovery_id}",
        "archived_path": f"interrupted_runs/{attempt_id}/{recovery_id}/archive",
    }
    if (
        set(payload) != expected_keys
        or payload.get("attempt_id") != attempt_id
        or payload.get("status") != "intent"
        or payload.get("identities") != identities
        or not isinstance(payload.get("recorded_at"), str)
        or not payload["recorded_at"]
        or any(payload.get(key) != value for key, value in expected_paths.items())
        or path.name != f"{attempt_id}.recovery.{recovery_id}.intent.json"
    ):
        raise ValueError("invalid execution recovery intent")
    return recovery_id


def complete_execution_recovery(
    registry_root: Path,
    *,
    intent: dict[str, Any],
) -> dict[str, Any]:
    """Append the terminal audit event for an already moved partial directory."""
    attempt_id = validate_publication_id(str(intent.get("attempt_id", "")))
    recovery_id = validate_publication_id(str(intent.get("recovery_id", "")))
    with _attempt_transition_lock(registry_root, attempt_id):
        pending = _pending_execution_recovery_unlocked(registry_root, attempt_id)
        destination = registry_root / f"{attempt_id}.recovery.{recovery_id}.complete.json"
        payload: dict[str, Any] = {
            "attempt_id": attempt_id,
            "recovery_id": recovery_id,
            "execution_id": intent.get("execution_id"),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "archived_path": intent.get("archived_path"),
            "identities": intent.get("identities"),
            "status": "complete",
        }
        if destination.exists():
            existing = _read_registry_json(destination)
            comparable = {key: value for key, value in payload.items() if key != "recorded_at"}
            if all(existing.get(key) == value for key, value in comparable.items()):
                return existing
            raise ValueError("completed execution recovery identity differs")
        if pending != intent:
            raise ValueError("execution recovery intent changed before completion")
        _write_exclusive(destination, _json_bytes(payload))
        return payload


def recover_prepared_publication(
    registry_root: Path,
    *,
    attempt_id: str,
    current: dict[str, object],
) -> dict[str, Any]:
    validate_publication_id(attempt_id)
    run_id = str(current.get("run_id", ""))
    raw = _read_registry_json(registry_root / f"{attempt_id}.prepared.json")
    resolve_prepared_publication(
        registry_root,
        attempt_id=attempt_id,
        release_run_id=run_id,
        sealed_protocol_sha256=str(raw.get("sealed_protocol_sha256", "")),
    )
    if not re.fullmatch(r"[0-9a-f]{64}", str(current.get("manifest_sha256", ""))):
        raise ValueError("prepared final-test publication differs from CURRENT")
    return append_attempt_outcome(
        registry_root,
        attempt_id=attempt_id,
        status="succeeded",
        authoritative=True,
        reason="recovered verified prepared final-test publication",
        release_run_id=str(current["run_id"]),
        release_manifest_sha256=str(current["manifest_sha256"]),
    )


def save_token_snapshot(
    registry_root: Path,
    *,
    attempt_id: str,
    token_bytes: bytes,
    expected_sha256: str,
) -> Path:
    if hashlib.sha256(token_bytes).hexdigest() != expected_sha256:
        raise ValueError("final-test token snapshot hash changed")
    destination = registry_root / f"{attempt_id}.token"
    _write_exclusive(destination, token_bytes)
    return destination


def _write_exclusive(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
