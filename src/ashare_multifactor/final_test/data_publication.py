from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
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
                temporary_metadata = os.stat(
                    temporary_name, dir_fd=final_fd, follow_symlinks=False
                )
                try:
                    atomic_rename_no_replace_at(
                        final_fd,
                        temporary_name,
                        final_fd,
                        claim_path.name,
                    )
                except FileExistsError as error:
                    raise ValueError("final-test data build is already claimed") from error
                installed_metadata = os.stat(
                    claim_path.name, dir_fd=final_fd, follow_symlinks=False
                )
                installed_bytes = read_bytes_at(
                    final_fd, claim_path.name, label="final-test data claim"
                )
                if (
                    (installed_metadata.st_dev, installed_metadata.st_ino)
                    != (temporary_metadata.st_dev, temporary_metadata.st_ino)
                    or installed_bytes != claim_bytes
                ):
                    _unlink_if_identity(final_fd, claim_path.name, installed_metadata)
                    raise ValueError("final-test data claim identity or bytes changed")
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
            current_bytes = read_bytes_at(
                final_fd, path.name, label="final-test data claim"
            )
            current_metadata = os.stat(
                path.name, dir_fd=final_fd, follow_symlinks=False
            )
            try:
                payload = _updated_claim_payload(
                    current_bytes,
                    authorization,
                    status=status,
                    error=error,
                    data_manifest=data_manifest,
                )
            except ValueError:
                if _claim_transaction_exists(final_fd, lock_name):
                    _, completed = _recover_claim_transaction(
                        final_fd,
                        path.name,
                        lock_name,
                        authorization,
                        current_bytes=current_bytes,
                        expected_bytes=None,
                    )
                    if completed:
                        return
                raise
            expected_bytes = _claim_bytes(payload)
            temporary_name = f".{path.name}.{uuid4().hex}.tmp"
            transaction = {
                "schema_version": "1",
                "attempt_id": authorization.attempt_id,
                "approval_id": authorization.approval_id,
                "old_sha256": hashlib.sha256(current_bytes).hexdigest(),
                "new_sha256": hashlib.sha256(expected_bytes).hexdigest(),
                "new_bytes_base64": base64.b64encode(expected_bytes).decode("ascii"),
                "temporary_name": temporary_name,
            }
            transaction_bytes = _claim_bytes(transaction)
            recovered = False
            try:
                write_bytes_exclusive_at(final_fd, lock_name, transaction_bytes)
                os.fsync(final_fd)
            except FileExistsError:
                temporary_name, recovered = _recover_claim_transaction(
                    final_fd,
                    path.name,
                    lock_name,
                    authorization,
                    current_bytes=current_bytes,
                    expected_bytes=expected_bytes,
                )
                if recovered:
                    assert_directory_entry(
                        parent_fd,
                        path.parent.name,
                        expected=final_identity,
                        label="final-test root",
                    )
                    return
            try:
                try:
                    os.unlink(temporary_name, dir_fd=final_fd)
                except FileNotFoundError:
                    pass
                write_bytes_exclusive_at(final_fd, temporary_name, expected_bytes)
                temporary_metadata = os.stat(
                    temporary_name, dir_fd=final_fd, follow_symlinks=False
                )
                current_again = os.stat(
                    path.name, dir_fd=final_fd, follow_symlinks=False
                )
                if (current_again.st_dev, current_again.st_ino) != (
                    current_metadata.st_dev,
                    current_metadata.st_ino,
                ) or read_bytes_at(
                    final_fd, path.name, label="final-test data claim"
                ) != current_bytes:
                    raise ValueError("final-test data claim identity changed")
                os.replace(
                    temporary_name,
                    path.name,
                    src_dir_fd=final_fd,
                    dst_dir_fd=final_fd,
                )
                installed_metadata = os.stat(
                    path.name, dir_fd=final_fd, follow_symlinks=False
                )
                installed_bytes = read_bytes_at(
                    final_fd, path.name, label="final-test data claim"
                )
                if (
                    (installed_metadata.st_dev, installed_metadata.st_ino)
                    != (temporary_metadata.st_dev, temporary_metadata.st_ino)
                    or installed_bytes != expected_bytes
                ):
                    _unlink_if_identity(final_fd, path.name, installed_metadata)
                    write_bytes_exclusive_at(final_fd, path.name, current_bytes)
                    raise ValueError("final-test data claim identity or bytes changed")
                assert_directory_entry(
                    parent_fd,
                    path.parent.name,
                    expected=final_identity,
                    label="final-test root",
                )
                os.fsync(final_fd)
            except Exception:
                try:
                    os.unlink(temporary_name, dir_fd=final_fd)
                except FileNotFoundError:
                    pass
                try:
                    os.unlink(lock_name, dir_fd=final_fd)
                except FileNotFoundError:
                    pass
                raise
            else:
                try:
                    os.unlink(temporary_name, dir_fd=final_fd)
                except FileNotFoundError:
                    pass
                os.unlink(lock_name, dir_fd=final_fd)
                os.fsync(final_fd)


def _recover_claim_transaction(
    final_fd: int,
    claim_name: str,
    lock_name: str,
    authorization: FinalTestAuthorization,
    *,
    current_bytes: bytes,
    expected_bytes: bytes | None,
) -> tuple[str, bool]:
    lock_bytes = read_bytes_at(final_fd, lock_name, label="claim transaction")
    try:
        transaction = json.loads(lock_bytes)
        encoded = transaction["new_bytes_base64"]
        recorded_new = base64.b64decode(encoded, validate=True)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid final-test data claim transaction") from error
    temporary_name = transaction.get("temporary_name")
    if (
        not isinstance(transaction, dict)
        or transaction.get("schema_version") != "1"
        or transaction.get("attempt_id") != authorization.attempt_id
        or transaction.get("approval_id") != authorization.approval_id
        or not isinstance(temporary_name, str)
        or not temporary_name.startswith(f".{claim_name}.")
        or not temporary_name.endswith(".tmp")
        or "/" in temporary_name
        or transaction.get("new_sha256")
        != hashlib.sha256(recorded_new).hexdigest()
    ):
        raise ValueError("final-test data claim transaction identity differs")
    try:
        _assert_claim_authorization_identity(json.loads(recorded_new), authorization)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError("final-test data claim transaction identity differs") from error
    current_hash = hashlib.sha256(current_bytes).hexdigest()
    if current_hash == transaction.get("new_sha256") and current_bytes == recorded_new:
        try:
            os.unlink(temporary_name, dir_fd=final_fd)
        except FileNotFoundError:
            pass
        os.unlink(lock_name, dir_fd=final_fd)
        os.fsync(final_fd)
        return temporary_name, True
    if expected_bytes is None:
        if current_hash != transaction.get("old_sha256"):
            raise ValueError("final-test data claim transaction cannot be taken over")
        return temporary_name, False
    expected_hash = hashlib.sha256(expected_bytes).hexdigest()
    if (
        current_hash != transaction.get("old_sha256")
        or expected_hash != transaction.get("new_sha256")
        or expected_bytes != recorded_new
    ):
        raise ValueError("final-test data claim transaction cannot be taken over")
    return temporary_name, False


def _claim_transaction_exists(final_fd: int, lock_name: str) -> bool:
    try:
        os.stat(lock_name, dir_fd=final_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _claim_bytes(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


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
    _assert_claim_authorization_identity(current, authorization)
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


def _assert_claim_authorization_identity(
    current: object, authorization: FinalTestAuthorization
) -> None:
    if not isinstance(current, dict):
        raise ValueError("final-test data claim authorization identity differs")
    expected = _claim_payload(authorization, status=str(current.get("status")))
    if any(
        current.get(key) != value for key, value in expected.items()
    ):
        raise ValueError("final-test data claim authorization identity differs")
    allowed = {
        *expected,
        "data_manifest",
        "input_inventory",
        "staging_relative_path",
        "error_type",
        "error",
    }
    if not set(current).issubset(allowed):
        raise ValueError("final-test data claim canonical identity differs")


def _unlink_if_identity(parent_fd: int, name: str, metadata: os.stat_result) -> None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == (metadata.st_dev, metadata.st_ino):
        os.unlink(name, dir_fd=parent_fd)


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
