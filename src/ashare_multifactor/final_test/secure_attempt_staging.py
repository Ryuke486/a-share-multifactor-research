"""Descriptor-anchored creation, writing, and freezing of one final-test attempt."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
from uuid import uuid4

import polars as pl

from ashare_multifactor.audit.secure_tree import (
    atomic_rename_no_replace_at,
    copy_frozen_tree_at,
    frozen_records,
    read_frozen_tree_at,
)
from ashare_multifactor.config import Period
from ashare_multifactor.final_test.panel_binding import (
    FrozenPanelSnapshot,
    bind_panel_snapshot,
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
    root_parent_fd: int
    root_fd: int
    parent_fd: int
    attempt_fd: int
    datasets_fd: int
    artifacts_fd: int
    root_identity: tuple[int, int]
    attempt_identity: tuple[int, int]
    parent_identity: tuple[int, int]
    datasets_identity: tuple[int, int]
    artifacts_identity: tuple[int, int]

    def close(self) -> None:
        for descriptor in (
            self.artifacts_fd,
            self.datasets_fd,
            self.attempt_fd,
            self.parent_fd,
            self.root_fd,
            self.root_parent_fd,
        ):
            os.close(descriptor)


def ensure_attempt_root(
    final_root: Path,
    *,
    attempt_id: str,
    execution_identity: Mapping[str, object],
) -> tuple[Path, AttemptDirectories]:
    destination = final_root / "attempt_runs" / attempt_id
    root_parent_fd = open_directory_path(
        final_root.parent, label="final-test processed root"
    )
    final_fd = open_directory_at(
        root_parent_fd, final_root.name, label="final-test root"
    )
    parent_fd: int | None = None
    attempt_fd: int | None = None
    try:
        try:
            os.mkdir("attempt_runs", mode=0o700, dir_fd=final_fd)
        except FileExistsError:
            pass
        parent_fd = open_directory_at(
            final_fd, "attempt_runs", label="attempt-runs root"
        )
        if entry_exists_at(parent_fd, attempt_id):
            attempt_fd = open_directory_at(
                parent_fd, attempt_id, label="attempt staging root"
            )
            if not _is_resumable_attempt_shell_at(attempt_fd, execution_identity):
                raise ValueError("final-test attempt shell is not safely resumable")
            return destination, _directories_from_open_fds(
                root_parent_fd, final_fd, parent_fd, attempt_fd
            )
        temporary_name = f".{attempt_id}.initializing"
        try:
            os.mkdir(temporary_name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        attempt_fd = open_directory_at(
            parent_fd, temporary_name, label="attempt initializer"
        )
        for name in ("datasets", "artifacts"):
            try:
                os.mkdir(name, mode=0o700, dir_fd=attempt_fd)
            except FileExistsError:
                pass
        if entry_exists_at(attempt_fd, "execution_identity.json"):
            try:
                stored = json.loads(
                    read_bytes_at(
                        attempt_fd,
                        "execution_identity.json",
                        label="attempt initializer identity",
                    )
                )
            except json.JSONDecodeError as error:
                raise ValueError("final-test attempt initializer is invalid") from error
            if stored != dict(execution_identity):
                raise ValueError("final-test attempt initializer identity differs")
        else:
            write_json_at(attempt_fd, "execution_identity.json", execution_identity)
        if not _is_resumable_attempt_shell_at(attempt_fd, execution_identity):
            raise ValueError("final-test attempt initializer is incomplete")
        temporary_identity = directory_identity(attempt_fd)
        atomic_rename_no_replace_at(parent_fd, temporary_name, attempt_id)
        assert_directory_entry(
            parent_fd,
            attempt_id,
            expected=temporary_identity,
            label="attempt staging root",
        )
        return destination, _directories_from_open_fds(
            root_parent_fd, final_fd, parent_fd, attempt_fd
        )
    except BaseException:
        for descriptor in (attempt_fd, parent_fd, final_fd, root_parent_fd):
            if descriptor is not None:
                os.close(descriptor)
        raise


def _directories_from_open_fds(
    root_parent_fd: int,
    root_fd: int,
    parent_fd: int,
    attempt_fd: int,
) -> AttemptDirectories:
    datasets_fd: int | None = None
    artifacts_fd: int | None = None
    try:
        datasets_fd = open_directory_at(
            attempt_fd, "datasets", label="attempt datasets staging"
        )
        artifacts_fd = open_directory_at(
            attempt_fd, "artifacts", label="attempt artifacts staging"
        )
        return AttemptDirectories(
            root_parent_fd=root_parent_fd,
            root_fd=root_fd,
            parent_fd=parent_fd,
            attempt_fd=attempt_fd,
            datasets_fd=datasets_fd,
            artifacts_fd=artifacts_fd,
            root_identity=directory_identity(root_fd),
            attempt_identity=directory_identity(attempt_fd),
            parent_identity=directory_identity(parent_fd),
            datasets_identity=directory_identity(datasets_fd),
            artifacts_identity=directory_identity(artifacts_fd),
        )
    except BaseException:
        for descriptor in (artifacts_fd, datasets_fd):
            if descriptor is not None:
                os.close(descriptor)
        raise


def assert_attempt_directory(
    attempt_root: Path, directories: AttemptDirectories
) -> None:
    assert_directory_entry(
        directories.root_parent_fd,
        attempt_root.parent.parent.name,
        expected=directories.root_identity,
        label="final-test root",
    )
    assert_directory_entry(
        directories.root_fd,
        "attempt_runs",
        expected=directories.parent_identity,
        label="attempt-runs root",
    )
    assert_directory_entry(
        directories.parent_fd,
        attempt_root.name,
        expected=directories.attempt_identity,
        label="attempt staging root",
    )
    assert_directory_entry(
        directories.attempt_fd,
        "datasets",
        expected=directories.datasets_identity,
        label="attempt datasets staging",
    )
    assert_directory_entry(
        directories.attempt_fd,
        "artifacts",
        expected=directories.artifacts_identity,
        label="attempt artifacts staging",
    )


def open_existing_attempt_directories(
    final_root: Path, *, attempt_id: str
) -> tuple[Path, AttemptDirectories]:
    root_parent_fd = open_directory_path(
        final_root.parent, label="final-test processed root"
    )
    root_fd: int | None = None
    parent_fd: int | None = None
    attempt_fd: int | None = None
    try:
        root_fd = open_directory_at(root_parent_fd, final_root.name, label="final-test root")
        parent_fd = open_directory_at(root_fd, "attempt_runs", label="attempt-runs root")
        attempt_fd = open_directory_at(parent_fd, attempt_id, label="attempt staging root")
        directories = _directories_from_open_fds(
            root_parent_fd, root_fd, parent_fd, attempt_fd
        )
        attempt_root = final_root / "attempt_runs" / attempt_id
        assert_attempt_directory(attempt_root, directories)
        return attempt_root, directories
    except BaseException:
        for descriptor in (attempt_fd, parent_fd, root_fd, root_parent_fd):
            if descriptor is not None:
                os.close(descriptor)
        raise
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
    bound_panel_files: Mapping[str, bytes] | None = None,
) -> None:
    files = read_frozen_tree_at(attempt_fd, label="attempt staging manifest input")
    if bound_panel_files is not None:
        _assert_bound_panel_files(files, bound_panel_files)
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


def replace_attempt_manifest(
    attempt_fd: int,
    *,
    expected_bytes: bytes,
    replacement_bytes: bytes,
) -> None:
    current = read_bytes_at(
        attempt_fd, "attempt_manifest.json", label="attempt manifest transition"
    )
    current_metadata = os.stat(
        "attempt_manifest.json", dir_fd=attempt_fd, follow_symlinks=False
    )
    if current != expected_bytes:
        raise ValueError("attempt manifest transition identity differs")
    temporary_name = f".attempt_manifest.{uuid4().hex}.tmp"
    write_bytes_exclusive_at(attempt_fd, temporary_name, replacement_bytes)
    temporary_metadata = os.stat(
        temporary_name, dir_fd=attempt_fd, follow_symlinks=False
    )
    try:
        current_again = os.stat(
            "attempt_manifest.json", dir_fd=attempt_fd, follow_symlinks=False
        )
        if (
            (current_again.st_dev, current_again.st_ino)
            != (current_metadata.st_dev, current_metadata.st_ino)
            or read_bytes_at(
                attempt_fd,
                "attempt_manifest.json",
                label="attempt manifest transition",
            )
            != expected_bytes
        ):
            raise ValueError("attempt manifest transition identity differs")
        os.replace(
            temporary_name,
            "attempt_manifest.json",
            src_dir_fd=attempt_fd,
            dst_dir_fd=attempt_fd,
        )
        installed = os.stat(
            "attempt_manifest.json", dir_fd=attempt_fd, follow_symlinks=False
        )
        installed_bytes = read_bytes_at(
            attempt_fd,
            "attempt_manifest.json",
            label="attempt manifest transition",
        )
        if (
            (installed.st_dev, installed.st_ino)
            != (temporary_metadata.st_dev, temporary_metadata.st_ino)
            or installed_bytes != replacement_bytes
        ):
            try:
                os.unlink("attempt_manifest.json", dir_fd=attempt_fd)
            except FileNotFoundError:
                pass
            write_bytes_exclusive_at(
                attempt_fd, "attempt_manifest.json", expected_bytes
            )
            raise ValueError("attempt manifest transition identity differs")
        os.fsync(attempt_fd)
    finally:
        try:
            os.unlink(temporary_name, dir_fd=attempt_fd)
        except FileNotFoundError:
            pass


def snapshot_attempt_panel(
    panel_root: Path,
    *,
    expected_manifest_sha256: str,
    datasets_fd: int,
    staged_panel_root: Path,
    period: Period,
) -> FrozenPanelSnapshot:
    with opened_directory(
        panel_root.parent, label="final daily panel parent"
    ) as panel_parent_fd:
        with opened_directory_at(
            panel_parent_fd, panel_root.name, label="final daily panel"
        ) as panel_fd:
            panel_identity = directory_identity(panel_fd)
            manifest = read_bytes_at(
                panel_fd,
                "data_manifest.json",
                label="final daily panel manifest",
            )
            if hashlib.sha256(manifest).hexdigest() != expected_manifest_sha256:
                raise ValueError("final daily panel manifest identity differs")
            copy_frozen_tree_at(
                panel_fd,
                datasets_fd,
                staged_panel_root.name,
                label="attempt-bound final daily panel",
            )
            assert_directory_entry(
                panel_parent_fd,
                panel_root.name,
                expected=panel_identity,
                label="final daily panel",
            )
    snapshot = bind_panel_snapshot(
        staged_panel_root,
        datasets_fd=datasets_fd,
        expected_manifest_sha256=expected_manifest_sha256,
        period=period,
    )
    try:
        snapshot.detach_from_namespace()
        return snapshot
    except BaseException:
        snapshot.close()
        raise


def freeze_prepared_package(
    attempt_root: Path,
    *,
    lineage: Mapping[str, object] | None,
    bound_input_files: Mapping[str, bytes],
    bound_panel_files: Mapping[str, bytes],
    attempt_fd: int | None = None,
    datasets_fd: int | None = None,
    artifacts_fd: int | None = None,
    attempt_manifest_metadata: Mapping[str, object] | None = None,
) -> FrozenPreparedPackage:
    if attempt_fd is not None:
        if datasets_fd is None or artifacts_fd is None:
            raise ValueError("complete prepared staging descriptors are required")
        datasets = read_frozen_tree_at(
            datasets_fd, label="prepared datasets staging"
        )
        artifacts = read_frozen_tree_at(
            artifacts_fd, label="prepared artifacts staging"
        )
        execution_identity_bytes = read_bytes_at(
            attempt_fd,
            "execution_identity.json",
            label="prepared execution identity",
        )
        if attempt_manifest_metadata is None:
            attempt_manifest_bytes = read_bytes_at(
                attempt_fd,
                "attempt_manifest.json",
                label="prepared attempt manifest",
            )
        else:
            manifest_files = {
                **{f"datasets/{path}": payload for path, payload in datasets.items()},
                **{f"artifacts/{path}": payload for path, payload in artifacts.items()},
                "execution_identity.json": execution_identity_bytes,
            }
            records = []
            for path, payload in sorted(manifest_files.items()):
                parent = path.rsplit("/", maxsplit=1)[0] if "/" in path else ""
                records.append(
                    {
                        "path": path,
                        "role": parent.rsplit("/", maxsplit=1)[-1],
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "size_bytes": len(payload),
                    }
                )
            attempt_manifest_bytes = json_bytes(
                {**attempt_manifest_metadata, "files": records}
            )
        return _build_frozen_prepared_package(
            datasets=datasets,
            artifacts=artifacts,
            execution_identity_bytes=execution_identity_bytes,
            attempt_manifest_bytes=attempt_manifest_bytes,
            lineage=lineage,
            bound_input_files=bound_input_files,
            bound_panel_files=bound_panel_files,
        )
    raise ValueError("prepared staging descriptors are required")


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
    bound_panel_files: Mapping[str, bytes],
) -> FrozenPreparedPackage:
    _assert_bound_panel_files(datasets, bound_panel_files)
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


def _assert_bound_panel_files(
    datasets: Mapping[str, bytes], bound_panel_files: Mapping[str, bytes]
) -> None:
    prefixes = ("final_daily_panel/", "datasets/final_daily_panel/")
    materialized = {
        next(
            path.removeprefix(prefix)
            for prefix in prefixes
            if path.startswith(prefix)
        ): payload
        for path, payload in datasets.items()
        if path.startswith(prefixes)
    }
    if materialized != dict(bound_panel_files):
        raise ValueError("prepared frozen final daily panel differs")
