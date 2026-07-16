from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any


_ATTEMPT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


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
    registry_root.mkdir(parents=True, exist_ok=True)
    destination = registry_root / f"{attempt_id}.outcome.json"
    try:
        _write_exclusive(
            destination,
            (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
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
    payload = {
        "attempt_id": attempt_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "status": "prepared",
        "authoritative": False,
        "release_run_id": release_run_id,
        "sealed_protocol_sha256": sealed_protocol_sha256,
    }
    registry_root.mkdir(parents=True, exist_ok=True)
    _write_exclusive(
        registry_root / f"{attempt_id}.prepared.json",
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(),
    )
    return payload


def recover_prepared_publication(
    registry_root: Path,
    *,
    attempt_id: str,
    current: dict[str, object],
) -> dict[str, Any]:
    validate_publication_id(attempt_id)
    prepared = json.loads(
        (registry_root / f"{attempt_id}.prepared.json").read_text(encoding="utf-8")
    )
    if (
        prepared.get("status") != "prepared"
        or prepared.get("attempt_id") != attempt_id
        or current.get("run_id") != prepared.get("release_run_id")
        or not re.fullmatch(r"[0-9a-f]{64}", str(current.get("manifest_sha256", "")))
    ):
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
