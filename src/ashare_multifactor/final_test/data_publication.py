from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from uuid import uuid4

from ashare_multifactor.final_test.data_inventory import (
    fsync_directory,
    verify_file_identity,
    verify_final_test_data_panel,
    write_json,
)
from ashare_multifactor.final_test.gate import FinalTestAuthorization
from ashare_multifactor.final_test.recovery_secure_fs import atomic_rename_no_replace


@dataclass(frozen=True)
class FinalTestDataResolution:
    root: Path
    claim_status: str
    requires_recovery: bool
    data_manifest_sha256: str


def claim_build(
    final_root: Path,
    authorization: FinalTestAuthorization,
    *,
    input_inventory: dict[str, object] | None = None,
    staging_relative_path: str | None = None,
) -> Path:
    final_root.mkdir(parents=True, exist_ok=True)
    claim_path = final_root / "data-build-claim.json"
    payload = _claim_payload(
        authorization,
        status="claimed",
        input_inventory=input_inventory,
        staging_relative_path=staging_relative_path,
    )
    temporary = final_root / f".data-build-claim.{uuid4().hex}.tmp"
    temporary_created = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        temporary_created = True
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            atomic_rename_no_replace(temporary, claim_path)
        except FileExistsError as error:
            raise ValueError("final-test data build is already claimed") from error
        fsync_directory(final_root)
    finally:
        if temporary_created:
            temporary.unlink(missing_ok=True)
    return claim_path


def update_claim(
    path: Path,
    authorization: FinalTestAuthorization,
    *,
    status: str,
    error: BaseException | None = None,
    data_manifest: dict[str, object] | None = None,
) -> None:
    current = json.loads(path.read_text(encoding="utf-8"))
    current_status = current.get("status") if isinstance(current, dict) else None
    allowed = {
        "claimed": {"publishing", "failed"},
        "publishing": {"published", "failed"},
    }
    if status not in allowed.get(str(current_status), set()):
        raise ValueError(
            f"invalid final-test data claim transition: {current_status}->{status}"
        )
    if data_manifest is None and isinstance(current, dict):
        existing_identity = current.get("data_manifest")
        if isinstance(existing_identity, dict):
            data_manifest = existing_identity
    input_inventory = current.get("input_inventory") if isinstance(current, dict) else None
    staging_relative_path = (
        current.get("staging_relative_path") if isinstance(current, dict) else None
    )
    payload = _claim_payload(
        authorization,
        status=status,
        data_manifest=data_manifest,
        input_inventory=(input_inventory if isinstance(input_inventory, dict) else None),
        staging_relative_path=(
            staging_relative_path if isinstance(staging_relative_path, str) else None
        ),
    )
    if error is not None:
        payload["error_type"] = type(error).__name__
        payload["error"] = str(error)
    write_json(path, payload)


def resolve_final_test_data_panel(final_root: Path) -> FinalTestDataResolution:
    """Resolve published data, including a post-rename publishing recovery state."""
    claim_path = final_root / "data-build-claim.json"
    if final_root.is_symlink() or claim_path.is_symlink():
        raise ValueError("final-test data publication path uses a symlink")
    try:
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError("invalid final-test data publication claim") from error
    status = claim.get("status") if isinstance(claim, dict) else None
    if status not in {"publishing", "published"}:
        raise ValueError("final-test data publication is not resolvable")
    identity = claim.get("data_manifest")
    if not isinstance(identity, dict):
        raise ValueError("final-test data publication lacks manifest identity")
    manifest_path = verify_file_identity(final_root, identity, "data manifest")
    root = final_root / "daily_panel"
    if manifest_path != root / "data_manifest.json":
        raise ValueError("final-test data manifest path is not canonical")
    verify_final_test_data_panel(root)
    return FinalTestDataResolution(
        root=root,
        claim_status=str(status),
        requires_recovery=status == "publishing",
        data_manifest_sha256=str(identity["sha256"]),
    )


def _claim_payload(
    authorization: FinalTestAuthorization,
    *,
    status: str,
    data_manifest: dict[str, object] | None = None,
    input_inventory: dict[str, object] | None = None,
    staging_relative_path: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "attempt_id": authorization.attempt_id,
        "approval_id": authorization.approval_id,
        "git_commit": authorization.git_commit,
        "git_tree": authorization.git_tree,
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "robustness_release": authorization.robustness_release,
        "robustness_manifest_sha256": authorization.robustness_manifest_sha256,
        "robustness_lineage_sha256": authorization.robustness_lineage_sha256,
        "status": status,
    }
    if data_manifest is not None:
        payload["data_manifest"] = data_manifest
    if input_inventory is not None:
        payload["input_inventory"] = input_inventory
    if staging_relative_path is not None:
        payload["staging_relative_path"] = staging_relative_path
    return payload
