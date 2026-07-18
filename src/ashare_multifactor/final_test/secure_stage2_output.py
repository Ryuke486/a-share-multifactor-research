"""Descriptor-bound Stage-2 output transaction for the final-test panel."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Iterator
from uuid import uuid4

import polars as pl

from ashare_multifactor.data.build import PartitionManifest
from ashare_multifactor.final_test.recovery_secure_fs import (
    assert_directory_entry,
    atomic_rename_no_replace_at,
    directory_identity,
    open_directory_at,
    open_directory_path,
    write_bytes_exclusive_at,
)

if TYPE_CHECKING:
    from ashare_multifactor.final_test.final_root_binding import FinalRootBinding


@dataclass
class Stage2Directories:
    data_root_parent_fd: int
    data_root_fd: int
    processed_fd: int
    final_fd: int
    data_staging_fd: int
    attempt_fd: int
    validation_fd: int
    data_root_identity: tuple[int, int]
    processed_identity: tuple[int, int]
    final_identity: tuple[int, int]
    data_staging_identity: tuple[int, int]
    attempt_identity: tuple[int, int]
    validation_identity: tuple[int, int]
    final_name: str
    processed_name: str
    attempt_name: str
    data_root_name: str
    root_binding: FinalRootBinding | None = None
    ancestors_removed: bool = False

    def assert_bound(self) -> None:
        if self.root_binding is not None:
            self.root_binding.crosscheck(
                processed_fd=self.processed_fd,
                final_identity=self.final_identity,
            )
        assert_directory_entry(
            self.data_root_parent_fd,
            self.data_root_name,
            expected=self.data_root_identity,
            label="final-test data root",
        )
        assert_directory_entry(
            self.data_root_fd,
            self.processed_name,
            expected=self.processed_identity,
            label="final-test processed root",
        )
        assert_directory_entry(
            self.processed_fd,
            self.final_name,
            expected=self.final_identity,
            label="final-test root",
        )
        assert_directory_entry(
            self.final_fd,
            "data-staging",
            expected=self.data_staging_identity,
            label="final-test data staging root",
        )
        if not self.ancestors_removed:
            assert_directory_entry(
                self.data_staging_fd,
                self.attempt_name,
                expected=self.attempt_identity,
                label="final-test data staging attempt",
            )
            assert_directory_entry(
                self.attempt_fd,
                "validation_evaluation",
                expected=self.validation_identity,
                label="secure Stage-2 output parent",
            )

    def assert_published(self, panel_identity: tuple[int, int]) -> None:
        assert_directory_entry(
            self.final_fd,
            "daily_panel",
            expected=panel_identity,
            label="final-test daily panel",
        )

    def remove_empty_ancestors(self) -> None:
        self.assert_bound()
        os.rmdir("validation_evaluation", dir_fd=self.attempt_fd)
        os.rmdir(self.attempt_name, dir_fd=self.data_staging_fd)
        self.ancestors_removed = True


@contextmanager
def opened_stage2_directories(
    final_root: Path,
    attempt_id: str,
    *,
    root_binding: FinalRootBinding | None = None,
) -> Iterator[Stage2Directories]:
    with _opened_stage2_directories(
        final_root,
        attempt_id,
        require_empty=True,
        root_binding=root_binding,
    ) as directories:
        yield directories


@contextmanager
def opened_existing_stage2_directories(
    final_root: Path,
    attempt_id: str,
    *,
    root_binding: FinalRootBinding | None = None,
) -> Iterator[Stage2Directories]:
    with _opened_stage2_directories(
        final_root,
        attempt_id,
        require_empty=False,
        root_binding=root_binding,
    ) as directories:
        yield directories


@contextmanager
def _opened_stage2_directories(
    final_root: Path,
    attempt_id: str,
    *,
    require_empty: bool,
    root_binding: FinalRootBinding | None,
) -> Iterator[Stage2Directories]:
    data_root = final_root.parent.parent
    if root_binding is not None:
        if final_root.absolute() != root_binding.final_root.absolute():
            raise ValueError("held final-test root differs from Stage-2 root")
        root_binding.assert_bound()
        data_root_parent_fd = os.dup(root_binding.data_parent_fd)
    else:
        data_root_parent_fd = open_directory_path(
            data_root.parent, label="final-test data-root parent"
        )
    data_root_fd: int | None = None
    processed_fd: int | None = None
    final_fd: int | None = None
    descriptors: list[int] = []
    try:
        if root_binding is None:
            data_root_fd = open_directory_at(
                data_root_parent_fd, data_root.name, label="final-test data root"
            )
            processed_fd = open_directory_at(
                data_root_fd, final_root.parent.name, label="final-test processed root"
            )
            final_fd = open_directory_at(
                processed_fd, final_root.name, label="final-test root"
            )
        else:
            data_root_fd = os.dup(root_binding.data_fd)
            processed_fd = os.dup(root_binding.processed_fd)
            final_fd = os.dup(root_binding.final_fd)
        current_fd = final_fd
        for part in ("data-staging", attempt_id, "validation_evaluation"):
            if require_empty:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
            current_fd = open_directory_at(
                current_fd, part, label="secure Stage-2 output parent"
            )
            descriptors.append(current_fd)
        data_staging_fd, attempt_fd, validation_fd = descriptors
        if require_empty:
            if set(os.listdir(attempt_fd)) != {"validation_evaluation"}:
                raise ValueError("secure Stage-2 attempt staging contains extra entries")
            if os.listdir(validation_fd):
                raise ValueError("secure Stage-2 output parent is not empty")
        directories_value = Stage2Directories(
            data_root_parent_fd=data_root_parent_fd,
            data_root_fd=data_root_fd,
            processed_fd=processed_fd,
            final_fd=final_fd,
            data_staging_fd=data_staging_fd,
            attempt_fd=attempt_fd,
            validation_fd=validation_fd,
            data_root_identity=directory_identity(data_root_fd),
            processed_identity=directory_identity(processed_fd),
            final_identity=directory_identity(final_fd),
            data_staging_identity=directory_identity(data_staging_fd),
            attempt_identity=directory_identity(attempt_fd),
            validation_identity=directory_identity(validation_fd),
            final_name=final_root.name,
            processed_name=final_root.parent.name,
            attempt_name=attempt_id,
            data_root_name=data_root.name,
            root_binding=root_binding,
        )
        directories_value.assert_bound()
        yield directories_value
        directories_value.assert_bound()
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        for descriptor in (final_fd, processed_fd, data_root_fd, data_root_parent_fd):
            if descriptor is not None:
                os.close(descriptor)


class SecureStage2Output:
    """Write and publish one Stage-2 tree through a held parent descriptor."""

    def __init__(self, parent_fd: int, target_name: str) -> None:
        self._parent_fd = parent_fd
        self._target_name = target_name
        self._staging_name = f".{target_name}-{uuid4().hex}.tmp"
        os.mkdir(self._staging_name, mode=0o700, dir_fd=parent_fd)
        self._staging_fd = open_directory_at(
            parent_fd, self._staging_name, label="secure Stage-2 staging"
        )
        self._staging_identity = directory_identity(self._staging_fd)
        self._published = False
        self._closed = False

    def write_year(
        self, year: int, frames: list[pl.DataFrame]
    ) -> PartitionManifest:
        self._assert_open()
        frame = pl.concat(frames).sort(["date", "symbol"])
        directory_name = f"year={year}"
        os.mkdir(directory_name, mode=0o700, dir_fd=self._staging_fd)
        year_fd = open_directory_at(
            self._staging_fd, directory_name, label="secure Stage-2 partition"
        )
        try:
            output_fd = os.open(
                "part-000.parquet",
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=year_fd,
            )
            try:
                with os.fdopen(os.dup(output_fd), "wb") as stream:
                    frame.write_parquet(stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                metadata = os.fstat(output_fd)
                digest = _descriptor_sha256(output_fd, metadata.st_size)
            finally:
                os.close(output_fd)
        finally:
            os.close(year_fd)
        return PartitionManifest(
            relative_path=f"{directory_name}/part-000.parquet",
            year=year,
            rows=frame.height,
            min_date=frame.get_column("date").min(),
            max_date=frame.get_column("date").max(),
            size_bytes=metadata.st_size,
            sha256=digest,
        )

    def write_json(self, name: str, payload: object) -> bytes:
        self._assert_open()
        content = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        write_bytes_exclusive_at(self._staging_fd, name, content)
        return content

    def publish(self) -> None:
        self._assert_open()
        assert_directory_entry(
            self._parent_fd,
            self._staging_name,
            expected=self._staging_identity,
            label="secure Stage-2 staging",
        )
        atomic_rename_no_replace_at(
            self._parent_fd,
            self._staging_name,
            self._parent_fd,
            self._target_name,
        )
        assert_directory_entry(
            self._parent_fd,
            self._target_name,
            expected=self._staging_identity,
            label="secure Stage-2 output",
        )
        os.fsync(self._parent_fd)
        self._published = True

    def close(self) -> None:
        if not self._closed:
            if not self._published and not os.listdir(self._staging_fd):
                assert_directory_entry(
                    self._parent_fd,
                    self._staging_name,
                    expected=self._staging_identity,
                    label="empty secure Stage-2 staging",
                )
                os.rmdir(self._staging_name, dir_fd=self._parent_fd)
            os.close(self._staging_fd)
            self._closed = True

    def __enter__(self) -> SecureStage2Output:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _assert_open(self) -> None:
        if self._closed:
            raise ValueError("secure Stage-2 output transaction is closed")


def _descriptor_sha256(descriptor: int, size_bytes: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < size_bytes:
        chunk = os.pread(descriptor, min(1024 * 1024, size_bytes - offset), offset)
        if not chunk:
            raise ValueError("secure Stage-2 output changed during hashing")
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()
