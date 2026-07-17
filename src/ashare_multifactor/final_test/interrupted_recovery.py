"""Crash-safe archival of interrupted final-test attempt directories."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

from ashare_multifactor.final_test.registry import (
    begin_execution_recovery,
    complete_execution_recovery,
    pending_execution_recovery,
    validate_publication_id,
)
from ashare_multifactor.final_test.resume import ResumePreflight


def recover_interrupted_execution(
    final_root: Path,
    *,
    preflight: ResumePreflight,
) -> Path | None:
    """Reconcile an intent, archive without replacement, then append completion."""
    attempt_id = preflight.authorization.attempt_id
    partial = final_root / "attempt_runs" / attempt_id
    registry_root = final_root / "attempts"
    intent = pending_execution_recovery(registry_root, attempt_id)
    identity_root = partial
    if intent is not None:
        identity_root = final_root / str(intent.get("archived_path", ""))
        if partial.exists():
            identity_root = partial
    elif not partial.exists() and not partial.is_symlink():
        return None
    if identity_root.is_symlink() or not identity_root.is_dir():
        raise ValueError("partial final-test execution is missing or uses a symlink")
    identity = _load_execution_identity(identity_root)
    expected = {
        "attempt_id": attempt_id,
        "prepare_manifest_sha256": preflight.preparation.manifest_sha256,
        "security_event_coverage_sha256": preflight.security_event_coverage_sha256,
        "corporate_action_coverage_sha256": preflight.corporate_action_coverage_sha256,
    }
    execution_id = identity.get("execution_id")
    if (
        not isinstance(execution_id, str)
        or not execution_id
        or any(identity.get(key) != value for key, value in expected.items())
    ):
        raise ValueError("partial final-test execution identity is incomplete or differs")
    validate_publication_id(execution_id)
    identities = {key: value for key, value in expected.items() if key != "attempt_id"}
    if intent is None:
        intent = begin_execution_recovery(
            registry_root,
            attempt_id=attempt_id,
            execution_id=execution_id,
            identities=identities,
        )
    if intent.get("execution_id") != execution_id or intent.get("identities") != identities:
        raise ValueError("pending execution recovery identity differs")
    recovery_root = final_root / str(intent["recovery_root"])
    archive = final_root / str(intent["archived_path"])
    if recovery_root.is_symlink() or archive.parent != recovery_root:
        raise ValueError("interrupted final-test archive uses a symlink")
    claim = recovery_root / ".intent-claim.json"
    if recovery_root.exists():
        try:
            claim_payload = json.loads(claim.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as error:
            raise ValueError("interrupted archive target is already occupied") from error
        if claim_payload != intent:
            raise ValueError("interrupted archive target claim differs")
    else:
        recovery_root.parent.mkdir(parents=True, exist_ok=True)
        recovery_root.mkdir()
        _write_json_exclusive(claim, intent)
    if partial.exists() and archive.exists():
        raise ValueError("interrupted archive target already contains data")
    if partial.exists():
        os.rename(partial, archive)
    elif not archive.is_dir() or archive.is_symlink():
        raise ValueError("interrupted execution archive is missing after recovery intent")
    if _load_execution_identity(archive) != identity:
        raise ValueError("interrupted execution archive identity changed")
    complete_execution_recovery(registry_root, intent=intent)
    return archive


def has_recoverable_execution_archive(final_root: Path, *, attempt_id: str) -> bool:
    try:
        intent = pending_execution_recovery(final_root / "attempts", attempt_id)
    except ValueError:
        return False
    if intent is None:
        return False
    partial = final_root / str(intent.get("source_path", ""))
    archive = final_root / str(intent.get("archived_path", ""))
    return not partial.exists() and archive.is_dir() and not archive.is_symlink()


def _load_execution_identity(root: Path) -> dict[str, object]:
    path = root / "execution_identity.json"
    if path.is_symlink():
        raise ValueError("partial final-test execution identity uses a symlink")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError("partial final-test execution identity is incomplete") from error
    if not isinstance(value, dict):
        raise ValueError("partial final-test execution identity is incomplete")
    return value


def _write_json_exclusive(path: Path, payload: Mapping[str, object]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
