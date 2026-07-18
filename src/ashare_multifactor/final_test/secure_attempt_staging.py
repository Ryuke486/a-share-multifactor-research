"""Descriptor-anchored creation, writing, and freezing of one final-test attempt."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path

import polars as pl

from ashare_multifactor.audit.secure_tree import (
    atomic_rename_no_replace_at,
    frozen_records,
    read_frozen_tree_at,
)
from ashare_multifactor.final_test.recovery_secure_fs import (
    assert_directory_entry,
    directory_identity,
    open_directory_at,
    open_directory_path,
    opened_directory,
    opened_directory_at,
    read_bytes_at,
    write_bytes_exclusive_at,
)


@dataclass(frozen=True)
class FrozenPreparedPackage:
    release_files: Mapping[str, bytes]
    execution_identity: Mapping[str, object]
    attempt_manifest: Mapping[str, object]
    lineage: Mapping[str, object]
    execution_identity_bytes: bytes
    attempt_manifest_bytes: bytes
    lineage_bytes: bytes


@dataclass(frozen=True)
class AttemptDirectories:
    parent_fd: int
    attempt_fd: int
    datasets_fd: int
    artifacts_fd: int
    attempt_identity: tuple[int, int]

    def close(self) -> None:
        for descriptor in (
            self.artifacts_fd,
            self.datasets_fd,
            self.attempt_fd,
            self.parent_fd,
        ):
            os.close(descriptor)


def ensure_attempt_root(
    final_root: Path,
    *,
    attempt_id: str,
    execution_identity: Mapping[str, object],
) -> Path:
    destination = final_root / "attempt_runs" / attempt_id
    with opened_directory(final_root, label="final-test root") as final_fd:
        try:
            os.mkdir("attempt_runs", mode=0o700, dir_fd=final_fd)
        except FileExistsError:
            pass
        with opened_directory_at(
            final_fd, "attempt_runs", label="attempt-runs root"
        ) as parent_fd:
            if entry_exists_at(parent_fd, attempt_id):
                with opened_directory_at(
                    parent_fd, attempt_id, label="attempt staging root"
                ) as destination_fd:
                    if not _is_resumable_attempt_shell_at(
                        destination_fd, execution_identity
                    ):
                        raise ValueError(
                            "final-test attempt shell is not safely resumable"
                        )
                return destination
            temporary_name = f".{attempt_id}.initializing"
            try:
                os.mkdir(temporary_name, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            with opened_directory_at(
                parent_fd, temporary_name, label="attempt initializer"
            ) as temporary_fd:
                for name in ("datasets", "artifacts"):
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=temporary_fd)
                    except FileExistsError:
                        pass
                if entry_exists_at(temporary_fd, "execution_identity.json"):
                    try:
                        stored = json.loads(
                            read_bytes_at(
                                temporary_fd,
                                "execution_identity.json",
                                label="attempt initializer identity",
                            )
                        )
                    except json.JSONDecodeError as error:
                        raise ValueError(
                            "final-test attempt initializer is invalid"
                        ) from error
                    if stored != dict(execution_identity):
                        raise ValueError(
                            "final-test attempt initializer identity differs"
                        )
                else:
                    write_json_at(
                        temporary_fd,
                        "execution_identity.json",
                        execution_identity,
                    )
                if not _is_resumable_attempt_shell_at(
                    temporary_fd, execution_identity
                ):
                    raise ValueError("final-test attempt initializer is incomplete")
                temporary_identity = directory_identity(temporary_fd)
                atomic_rename_no_replace_at(parent_fd, temporary_name, attempt_id)
                assert_directory_entry(
                    parent_fd,
                    attempt_id,
                    expected=temporary_identity,
                    label="attempt staging root",
                )
    return destination


def open_attempt_directories(attempt_root: Path) -> AttemptDirectories:
    descriptors: list[int] = []
    try:
        parent_fd = open_directory_path(
            attempt_root.parent, label="attempt staging parent"
        )
        descriptors.append(parent_fd)
        attempt_fd = open_directory_at(
            parent_fd, attempt_root.name, label="attempt staging root"
        )
        descriptors.append(attempt_fd)
        datasets_fd = open_directory_at(
            attempt_fd, "datasets", label="attempt datasets staging"
        )
        descriptors.append(datasets_fd)
        artifacts_fd = open_directory_at(
            attempt_fd, "artifacts", label="attempt artifacts staging"
        )
        descriptors.append(artifacts_fd)
        return AttemptDirectories(
            parent_fd=parent_fd,
            attempt_fd=attempt_fd,
            datasets_fd=datasets_fd,
            artifacts_fd=artifacts_fd,
            attempt_identity=directory_identity(attempt_fd),
        )
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def assert_attempt_directory(
    attempt_root: Path, directories: AttemptDirectories
) -> None:
    assert_directory_entry(
        directories.parent_fd,
        attempt_root.name,
        expected=directories.attempt_identity,
        label="attempt staging root",
    )


def write_frame_at(directory_fd: int, name: str, frame: pl.DataFrame) -> None:
    buffer = BytesIO()
    frame.write_parquet(buffer)
    write_bytes_exclusive_at(directory_fd, name, buffer.getvalue())


def write_bytes_at(directory_fd: int, name: str, payload: bytes) -> None:
    write_bytes_exclusive_at(directory_fd, name, payload)


def write_json_at(
    directory_fd: int, name: str, payload: Mapping[str, object]
) -> None:
    write_bytes_exclusive_at(directory_fd, name, json_bytes(payload))


def write_attempt_manifest(
    attempt_fd: int,
    *,
    metadata: Mapping[str, object],
) -> None:
    files = read_frozen_tree_at(attempt_fd, label="attempt staging manifest input")
    files.pop("attempt_manifest.json", None)
    records = []
    for path, payload in sorted(files.items()):
        parent = path.rsplit("/", maxsplit=1)[0] if "/" in path else ""
        records.append(
            {
                "path": path,
                "role": parent.rsplit("/", maxsplit=1)[-1],
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        )
    write_json_at(
        attempt_fd,
        "attempt_manifest.json",
        {**metadata, "files": records},
    )


def freeze_prepared_package(
    attempt_root: Path,
    *,
    lineage: Mapping[str, object] | None,
    bound_input_files: Mapping[str, bytes],
    attempt_fd: int | None = None,
    datasets_fd: int | None = None,
    artifacts_fd: int | None = None,
) -> FrozenPreparedPackage:
    if attempt_fd is not None:
        if datasets_fd is None or artifacts_fd is None:
            raise ValueError("complete prepared staging descriptors are required")
        return _build_frozen_prepared_package(
            datasets=read_frozen_tree_at(
                datasets_fd, label="prepared datasets staging"
            ),
            artifacts=read_frozen_tree_at(
                artifacts_fd, label="prepared artifacts staging"
            ),
            execution_identity_bytes=read_bytes_at(
                attempt_fd,
                "execution_identity.json",
                label="prepared execution identity",
            ),
            attempt_manifest_bytes=read_bytes_at(
                attempt_fd,
                "attempt_manifest.json",
                label="prepared attempt manifest",
            ),
            lineage=lineage,
            bound_input_files=bound_input_files,
        )
    if datasets_fd is not None or artifacts_fd is not None:
        raise ValueError("complete prepared staging descriptors are required")
    with opened_directory(
        attempt_root.parent, label="prepared staging parent"
    ) as parent_fd:
        with opened_directory_at(
            parent_fd, attempt_root.name, label="prepared staging root"
        ) as opened_attempt_fd:
            attempt_identity = directory_identity(opened_attempt_fd)
            with opened_directory_at(
                opened_attempt_fd, "datasets", label="prepared datasets staging"
            ) as opened_datasets_fd:
                datasets = read_frozen_tree_at(
                    opened_datasets_fd, label="prepared datasets staging"
                )
            with opened_directory_at(
                opened_attempt_fd, "artifacts", label="prepared artifacts staging"
            ) as opened_artifacts_fd:
                artifacts = read_frozen_tree_at(
                    opened_artifacts_fd, label="prepared artifacts staging"
                )
            execution_identity_bytes = read_bytes_at(
                opened_attempt_fd,
                "execution_identity.json",
                label="prepared execution identity",
            )
            attempt_manifest_bytes = read_bytes_at(
                opened_attempt_fd,
                "attempt_manifest.json",
                label="prepared attempt manifest",
            )
            assert_directory_entry(
                parent_fd,
                attempt_root.name,
                expected=attempt_identity,
                label="prepared staging root",
            )
    return _build_frozen_prepared_package(
        datasets=datasets,
        artifacts=artifacts,
        execution_identity_bytes=execution_identity_bytes,
        attempt_manifest_bytes=attempt_manifest_bytes,
        lineage=lineage,
        bound_input_files=bound_input_files,
    )


def release_files_identity(files: Mapping[str, bytes]) -> dict[str, object]:
    records = frozen_records(files, prefix="", role="prepared_staging")
    canonical = json.dumps(
        records,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return {
        "staging_files_sha256": hashlib.sha256(canonical).hexdigest(),
        "staging_file_count": len(records),
    }


def verify_attempt_manifest_records(package: FrozenPreparedPackage) -> None:
    records = package.attempt_manifest.get("files")
    expected_files = {
        path: payload
        for path, payload in package.release_files.items()
        if path != "lineage.json"
    }
    expected_files["execution_identity.json"] = package.execution_identity_bytes
    if not isinstance(records, list) or len(records) != len(expected_files):
        raise ValueError("prepared final-test staging identity differs")
    by_path = {
        str(record.get("path")): record
        for record in records
        if isinstance(record, dict)
    }
    if set(by_path) != set(expected_files):
        raise ValueError("prepared final-test staging identity differs")
    for path, payload in expected_files.items():
        record = by_path[path]
        if (
            record.get("sha256") != hashlib.sha256(payload).hexdigest()
            or record.get("size_bytes") != len(payload)
        ):
            raise ValueError("prepared final-test staging identity differs")


def entry_exists_at(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def json_bytes(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n"
    ).encode("utf-8")


def _is_resumable_attempt_shell_at(
    attempt_fd: int, execution_identity: Mapping[str, object]
) -> bool:
    if set(os.listdir(attempt_fd)) != {
        "execution_identity.json",
        "datasets",
        "artifacts",
    }:
        return False
    try:
        with opened_directory_at(
            attempt_fd, "datasets", label="attempt datasets staging"
        ) as datasets_fd:
            if os.listdir(datasets_fd):
                return False
        with opened_directory_at(
            attempt_fd, "artifacts", label="attempt artifacts staging"
        ) as artifacts_fd:
            if os.listdir(artifacts_fd):
                return False
        stored = json.loads(
            read_bytes_at(
                attempt_fd,
                "execution_identity.json",
                label="attempt execution identity",
            )
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return stored == dict(execution_identity)


def _build_frozen_prepared_package(
    *,
    datasets: Mapping[str, bytes],
    artifacts: Mapping[str, bytes],
    execution_identity_bytes: bytes,
    attempt_manifest_bytes: bytes,
    lineage: Mapping[str, object] | None,
    bound_input_files: Mapping[str, bytes],
) -> FrozenPreparedPackage:
    bound_prefix = "execution_inputs/"
    materialized_inputs = {
        path.removeprefix(bound_prefix): payload
        for path, payload in artifacts.items()
        if path.startswith(bound_prefix)
    }
    if materialized_inputs != dict(bound_input_files):
        raise ValueError("prepared frozen execution inputs differ")
    try:
        execution_identity = json.loads(execution_identity_bytes)
        attempt_manifest = json.loads(attempt_manifest_bytes)
    except json.JSONDecodeError as error:
        raise ValueError("prepared final-test staging identity is incomplete") from error
    if not isinstance(execution_identity, dict) or not isinstance(
        attempt_manifest, dict
    ):
        raise ValueError("prepared final-test staging identity differs")
    lineage_bytes = (
        json_bytes(lineage)
        if lineage is not None
        else artifacts.get("lineage_preview.json")
    )
    if lineage_bytes is None:
        raise ValueError("prepared final-test lineage is missing")
    try:
        frozen_lineage = json.loads(lineage_bytes)
    except json.JSONDecodeError as error:
        raise ValueError("prepared final-test lineage is invalid") from error
    if not isinstance(frozen_lineage, dict):
        raise ValueError("prepared final-test lineage is invalid")
    if artifacts.get("lineage_preview.json") != lineage_bytes:
        raise ValueError("prepared final-test lineage preview differs")
    release_files = {
        **{f"datasets/{path}": payload for path, payload in datasets.items()},
        **{f"artifacts/{path}": payload for path, payload in artifacts.items()},
        "lineage.json": lineage_bytes,
    }
    return FrozenPreparedPackage(
        release_files=release_files,
        execution_identity=execution_identity,
        attempt_manifest=attempt_manifest,
        lineage=frozen_lineage,
        execution_identity_bytes=execution_identity_bytes,
        attempt_manifest_bytes=attempt_manifest_bytes,
        lineage_bytes=lineage_bytes,
    )
