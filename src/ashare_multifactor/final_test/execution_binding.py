"""Resolve execution inputs into one immutable, descriptor-anchored snapshot."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
from types import MappingProxyType

from ashare_multifactor.final_test.execution_identity import (
    assert_execution_identity_authorized,
)
from ashare_multifactor.final_test.gate import (
    FINAL_TEST_END,
    FINAL_TEST_START,
    FinalTestAuthorization,
)
from ashare_multifactor.final_test.recovery_secure_fs import (
    opened_directory,
    opened_directory_at,
    read_bytes_at,
)
from ashare_multifactor.final_test.registry import (
    resolve_execution_binding,
    resolve_execution_input_manifest_hash,
    resolve_execution_output_intent,
)


@dataclass(frozen=True)
class BoundExecutionInputs:
    """Validated input bytes; later source-path replacement cannot change them."""

    manifest: Mapping[str, object]
    manifest_sha256: str
    files: Mapping[str, bytes]

    def __getitem__(self, name: str) -> bytes:
        return self.files[name]


def resolve_bound_execution_inputs(
    final_root: Path,
    authorization: FinalTestAuthorization,
) -> BoundExecutionInputs:
    """Read the registry-bound input tree once through no-follow descriptors."""
    identity = assert_execution_identity_authorized(
        resolve_execution_binding(
            final_root / "attempts", attempt_id=authorization.attempt_id
        ),
        authorization,
    )
    expected_hash = resolve_execution_input_manifest_hash(
        final_root / "attempts", identity=identity
    )
    expected_outputs = resolve_execution_output_intent(
        final_root / "attempts", identity=identity
    )
    return validate_execution_input_candidate(
        final_root,
        authorization,
        execution_identity=identity,
        expected_manifest_sha256=expected_hash,
        expected_primary_outputs=expected_outputs,
    )


def validate_execution_input_candidate(
    final_root: Path,
    authorization: FinalTestAuthorization,
    *,
    execution_identity: Mapping[str, object],
    expected_primary_outputs: Mapping[str, Mapping[str, object]],
    expected_manifest_sha256: str | None = None,
) -> BoundExecutionInputs:
    """Validate complete canonical inputs, including before the hash append."""
    identity = assert_execution_identity_authorized(
        execution_identity, authorization
    )
    if final_root.is_symlink():
        raise ValueError("final-test root uses a symlink")
    root = final_root.resolve()
    with opened_directory(root, label="final-test root") as final_fd:
        stored_identity = _read_nested_json(
            final_fd,
            ("attempt_runs", authorization.attempt_id),
            "execution_identity.json",
            label="execution identity",
        )
        if stored_identity != identity:
            raise ValueError("execution identity differs from registry binding")
        with opened_directory_at(
            final_fd, "attempt_inputs", label="execution-input parent"
        ) as parent_fd:
            with opened_directory_at(
                parent_fd,
                authorization.attempt_id,
                label="execution-input root",
            ) as input_fd:
                tree = _read_stable_tree(input_fd)

    manifest_bytes = tree.get("manifest.json")
    if manifest_bytes is None:
        raise ValueError("execution-input manifest is missing")
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if (
        expected_manifest_sha256 is not None
        and manifest_sha256 != expected_manifest_sha256
    ):
        raise ValueError("execution-input manifest differs from registry binding")
    try:
        payload = json.loads(manifest_bytes)
    except json.JSONDecodeError as error:
        raise ValueError("invalid execution-input manifest") from error
    if not isinstance(payload, dict):
        raise ValueError("invalid execution-input manifest")
    _verify_manifest_payload(
        payload,
        authorization=authorization,
        identity=identity,
        tree=tree,
        expected_primary_outputs=expected_primary_outputs,
    )
    return BoundExecutionInputs(
        manifest=MappingProxyType(payload),
        manifest_sha256=manifest_sha256,
        files=MappingProxyType(tree),
    )


def _read_nested_json(
    parent_fd: int,
    directories: tuple[str, ...],
    filename: str,
    *,
    label: str,
) -> dict[str, object]:
    descriptors: list[int] = []
    current = parent_fd
    try:
        for name in directories:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY")
                | getattr(os, "O_NOFOLLOW"),
                dir_fd=current,
            )
            descriptors.append(descriptor)
            current = descriptor
        value = json.loads(read_bytes_at(current, filename, label=label))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is missing or invalid") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    if not isinstance(value, dict):
        raise ValueError(f"{label} is missing or invalid")
    return value


def _read_stable_tree(root_fd: int) -> dict[str, bytes]:
    before = os.fstat(root_fd)
    first_names = tuple(sorted(os.listdir(root_fd)))
    result = _read_tree_at(root_fd, prefix="")
    after_names = tuple(sorted(os.listdir(root_fd)))
    after = os.fstat(root_fd)
    if (
        first_names != after_names
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
    ):
        raise ValueError("execution-input tree changed during binding")
    return result


def _read_tree_at(directory_fd: int, *, prefix: str) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for name in sorted(os.listdir(directory_fd)):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        relative = f"{prefix}/{name}" if prefix else name
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("execution-input tree uses a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            with opened_directory_at(
                directory_fd, name, label="execution-input child directory"
            ) as child_fd:
                before = os.fstat(child_fd)
                child = _read_tree_at(child_fd, prefix=relative)
                after = os.fstat(child_fd)
                if (
                    before.st_mtime_ns != after.st_mtime_ns
                    or before.st_ctime_ns != after.st_ctime_ns
                ):
                    raise ValueError("execution-input tree changed during binding")
                result.update(child)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("execution-input tree contains a non-regular file")
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW"),
            dir_fd=directory_fd,
        )
        try:
            opened = os.fstat(descriptor)
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                payload = stream.read()
            closed = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        identities = {
            (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
            for item in (metadata, opened, closed, current)
        }
        if len(identities) != 1 or len(payload) != opened.st_size:
            raise ValueError("execution-input file changed during binding")
        result[relative] = payload
    return result


def _verify_manifest_payload(
    payload: dict[str, object],
    *,
    authorization: FinalTestAuthorization,
    identity: Mapping[str, object],
    tree: Mapping[str, bytes],
    expected_primary_outputs: Mapping[str, Mapping[str, object]],
) -> None:
    expected = {
        "attempt_id": authorization.attempt_id,
        "approval_id": authorization.approval_id,
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "robustness_release": authorization.robustness_release,
        "period": [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()],
        **identity,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError("execution-input manifest differs from authorization")
    primary = _records_by_path(payload.get("files"), label="file")
    if set(primary) != {"corporate_actions.parquet", "security_events.parquet"}:
        raise ValueError("execution-input manifest file set is incomplete")
    if set(expected_primary_outputs) != set(primary):
        raise ValueError("deterministic execution output intent is incomplete")
    for path, expected_output in expected_primary_outputs.items():
        data = tree[path]
        if (
            expected_output.get("size_bytes") != len(data)
            or expected_output.get("sha256")
            != hashlib.sha256(data).hexdigest()
        ):
            raise ValueError("deterministic execution output differs from bound intent")
    coverage = _records_by_path(
        payload.get("coverage_snapshot_files"),
        label="coverage snapshot inventory",
    )
    if not coverage:
        raise ValueError("execution-input coverage snapshot inventory is missing")
    if any(
        not PurePosixPath(path).parts
        or PurePosixPath(path).parts[0] != "coverage_snapshot"
        for path in coverage
    ):
        raise ValueError("execution-input coverage snapshot path is unsafe")
    expected_paths = {"manifest.json", *primary, *coverage}
    if set(tree) != expected_paths:
        raise ValueError("execution-input full file inventory differs")
    for path, record in {**primary, **coverage}.items():
        data = tree[path]
        if (
            record.get("size_bytes") != len(data)
            or record.get("sha256") != hashlib.sha256(data).hexdigest()
        ):
            raise ValueError(f"execution-input digest mismatch: {path}")
    snapshot_bytes = tree.get("coverage_snapshot/snapshot_manifest.json")
    if snapshot_bytes is None or hashlib.sha256(snapshot_bytes).hexdigest() != identity.get(
        "coverage_snapshot_manifest_sha256"
    ):
        raise ValueError("coverage snapshot manifest differs from registry binding")
    try:
        snapshot = json.loads(snapshot_bytes)
    except json.JSONDecodeError as error:
        raise ValueError("invalid coverage snapshot manifest") from error
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
    if not isinstance(snapshot, dict) or any(
        snapshot.get(key) != value for key, value in snapshot_expected.items()
    ):
        raise ValueError("coverage snapshot manifest differs from registry binding")
    nested = _records_by_path(snapshot.get("files"), label="snapshot file inventory")
    if {f"coverage_snapshot/{path}" for path in nested} != (
        set(coverage) - {"coverage_snapshot/snapshot_manifest.json"}
    ):
        raise ValueError("coverage snapshot nested inventory differs")


def _records_by_path(value: object, *, label: str) -> dict[str, dict[str, object]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"execution-input {label} is missing")
    records: dict[str, dict[str, object]] = {}
    for record in value:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ValueError(f"execution-input {label} is invalid")
        path = str(record["path"])
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts or path in records:
            raise ValueError(f"execution-input {label} is invalid")
        records[path] = record
    if len(records) != len(value):
        raise ValueError(f"execution-input {label} is invalid")
    return records
