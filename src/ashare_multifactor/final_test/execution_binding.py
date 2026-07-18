"""Resolve execution inputs only through append-only attempt bindings."""

from __future__ import annotations

import json
from pathlib import Path

from ashare_multifactor.audit.records import sha256_file
from ashare_multifactor.final_test.action_source_contract import (
    verify_execution_input_manifest,
)
from ashare_multifactor.final_test.execution_identity import (
    assert_execution_identity_authorized,
)
from ashare_multifactor.final_test.gate import FinalTestAuthorization
from ashare_multifactor.final_test.registry import (
    resolve_execution_binding,
    resolve_execution_input_manifest_hash,
)


def resolve_bound_execution_inputs(
    final_root: Path,
    authorization: FinalTestAuthorization,
) -> dict[str, Path]:
    """Verify the registry, identity, snapshot, and manifest before Parquet reads."""
    attempt_id = authorization.attempt_id
    identity = assert_execution_identity_authorized(
        resolve_execution_binding(final_root / "attempts", attempt_id=attempt_id),
        authorization,
    )
    identity_path = (
        final_root / "attempt_runs" / attempt_id / "execution_identity.json"
    )
    stored_identity = _read_safe_json(
        identity_path,
        root=final_root,
        label="execution identity",
    )
    if stored_identity != identity:
        raise ValueError("execution identity differs from registry binding")

    snapshot_manifest = (
        final_root
        / "execution_coverage_snapshots"
        / attempt_id
        / "snapshot_manifest.json"
    )
    snapshot = _read_safe_json(
        snapshot_manifest,
        root=final_root,
        label="coverage snapshot manifest",
    )
    snapshot_expected = {
        "attempt_id": identity["attempt_id"],
        "prepare_manifest_sha256": identity["prepare_manifest_sha256"],
        "security_event_coverage_sha256": identity[
            "security_event_coverage_sha256"
        ],
        "corporate_action_coverage_sha256": identity[
            "corporate_action_coverage_sha256"
        ],
    }
    if (
        sha256_file(snapshot_manifest)
        != identity["coverage_snapshot_manifest_sha256"]
        or any(snapshot.get(key) != value for key, value in snapshot_expected.items())
    ):
        raise ValueError("coverage snapshot manifest differs from registry binding")

    manifest_path = final_root / "attempt_inputs" / attempt_id / "manifest.json"
    _assert_safe_file(manifest_path, root=final_root, label="execution-input manifest")
    expected_manifest_sha256 = resolve_execution_input_manifest_hash(
        final_root / "attempts",
        identity=identity,
    )
    if sha256_file(manifest_path) != expected_manifest_sha256:
        raise ValueError("execution-input manifest differs from registry binding")
    return verify_execution_input_manifest(
        manifest_path,
        authorization,
        execution_identity=identity,
    )


def _read_safe_json(path: Path, *, root: Path, label: str) -> dict[str, object]:
    _assert_safe_file(path, root=root, label=label)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid {label}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"invalid {label}")
    return payload


def _assert_safe_file(path: Path, *, root: Path, label: str) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ValueError(f"{label} escapes final-test root") from error
    current = root
    if root.is_symlink():
        raise ValueError(f"{label} path uses a symlink")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{label} path uses a symlink")
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"{label} is missing or escapes final-test root")
