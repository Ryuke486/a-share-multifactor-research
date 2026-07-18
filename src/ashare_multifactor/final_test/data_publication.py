from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from uuid import uuid4

from ashare_multifactor.final_test.data_inventory import (
    verify_file_identity,
    verify_final_test_data_panel,
)
from ashare_multifactor.final_test.gate import FinalTestAuthorization
from ashare_multifactor.final_test.recovery_secure_fs import (
    assert_directory_entry,
    atomic_rename_no_replace_at,
    directory_identity,
    opened_directory,
    opened_directory_at,
    read_bytes_at,
    write_bytes_exclusive_at,
)


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
    temporary_name = f".data-build-claim.{uuid4().hex}.tmp"
    claim_bytes = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    with opened_directory(
        final_root.parent, label="final-test processed parent"
    ) as parent_fd:
        with opened_directory_at(
            parent_fd, final_root.name, label="final-test root"
        ) as final_fd:
            final_identity = directory_identity(final_fd)
            try:
                write_bytes_exclusive_at(final_fd, temporary_name, claim_bytes)
                try:
                    atomic_rename_no_replace_at(
                        final_fd,
                        temporary_name,
                        final_fd,
                        claim_path.name,
                    )
                except FileExistsError as error:
                    raise ValueError("final-test data build is already claimed") from error
            finally:
                try:
                    os.unlink(temporary_name, dir_fd=final_fd)
                except FileNotFoundError:
                    pass
            assert_directory_entry(
                parent_fd,
                final_root.name,
                expected=final_identity,
                label="final-test root",
            )
            os.fsync(final_fd)
    return claim_path


def update_claim(
    path: Path,
    authorization: FinalTestAuthorization,
    *,
    status: str,
    error: BaseException | None = None,
    data_manifest: dict[str, object] | None = None,
) -> None:
    with opened_directory(
        path.parent.parent, label="final-test processed parent"
    ) as parent_fd:
        with opened_directory_at(
            parent_fd, path.parent.name, label="final-test root"
        ) as final_fd:
            final_identity = directory_identity(final_fd)
            lock_name = f".{path.name}.lock"
            write_bytes_exclusive_at(final_fd, lock_name, b"locked\n")
            try:
                current_bytes = read_bytes_at(
                    final_fd, path.name, label="final-test data claim"
                )
                current_metadata = os.stat(
                    path.name, dir_fd=final_fd, follow_symlinks=False
                )
                payload = _updated_claim_payload(
                    current_bytes,
                    authorization,
                    status=status,
                    error=error,
                    data_manifest=data_manifest,
                )
                temporary_name = f".{path.name}.{uuid4().hex}.tmp"
                write_bytes_exclusive_at(
                    final_fd,
                    temporary_name,
                    (
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
                current_again = os.stat(
                    path.name, dir_fd=final_fd, follow_symlinks=False
                )
                if (current_again.st_dev, current_again.st_ino) != (
                    current_metadata.st_dev,
                    current_metadata.st_ino,
                ):
                    raise ValueError("final-test data claim identity changed")
                os.replace(
                    temporary_name,
                    path.name,
                    src_dir_fd=final_fd,
                    dst_dir_fd=final_fd,
                )
                assert_directory_entry(
                    parent_fd,
                    path.parent.name,
                    expected=final_identity,
                    label="final-test root",
                )
            finally:
                try:
                    os.unlink(lock_name, dir_fd=final_fd)
                except FileNotFoundError:
                    pass


def _updated_claim_payload(
    current_bytes: bytes,
    authorization: FinalTestAuthorization,
    *,
    status: str,
    error: BaseException | None,
    data_manifest: dict[str, object] | None,
) -> dict[str, object]:
    current = json.loads(current_bytes)
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
    return payload


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
