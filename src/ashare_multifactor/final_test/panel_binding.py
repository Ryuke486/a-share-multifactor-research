"""Held-descriptor binding for one attempt's immutable daily panel."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import stat

from ashare_multifactor.config import Period
from ashare_multifactor.data.manifest import DailyPanelSource, validate_panel_source
from ashare_multifactor.final_test.recovery_secure_fs import (
    directory_identity,
    no_follow_flag,
    open_directory_at,
)


@dataclass(frozen=True)
class _HeldFile:
    relative_path: str
    descriptor: int
    identity: tuple[int, int]
    size_bytes: int
    sha256: str


@dataclass
class FrozenPanelSnapshot:
    root: Path
    datasets_fd: int
    root_fd: int
    root_identity: tuple[int, int]
    source: DailyPanelSource | None
    files: tuple[_HeldFile, ...]
    partition_relatives: tuple[str, ...]
    manifest_sha256: str
    detached: bool = False
    anonymous_frozen: bool = False
    _closed: bool = False

    def assert_bound(self) -> None:
        if self._closed:
            raise ValueError("attempt-bound final daily panel is closed")
        for item in self.files:
            metadata = os.fstat(item.descriptor)
            if (
                (metadata.st_dev, metadata.st_ino) != item.identity
                or metadata.st_size != item.size_bytes
            ):
                raise ValueError("attempt-bound final daily panel file identity changed")
            if self.anonymous_frozen and metadata.st_nlink != 0:
                raise ValueError("attempt-bound final daily panel file is still mutable")

    def read_frozen_files(self) -> dict[str, bytes]:
        self.assert_bound()
        frozen = {
            item.relative_path: _read_held_file(item)
            for item in self.files
        }
        self.assert_bound()
        return frozen

    def close(self) -> None:
        if self._closed:
            return
        for item in reversed(self.files):
            os.close(item.descriptor)
        os.close(self.root_fd)
        self._closed = True

    def detach_from_namespace(self) -> None:
        if self._closed or self.detached:
            raise ValueError("attempt-bound final daily panel detach state is invalid")
        metadata = os.stat(
            self.root.name, dir_fd=self.datasets_fd, follow_symlinks=False
        )
        if (metadata.st_dev, metadata.st_ino) != self.root_identity:
            raise ValueError("attempt-bound final daily panel directory identity changed")
        by_relative = {item.relative_path: item for item in self.files}
        _unlink_bound_tree(self.root_fd, prefix="", held=by_relative)
        os.rmdir(self.root.name, dir_fd=self.datasets_fd)
        self.detached = True
        self.assert_bound()

    def freeze_anonymous(self) -> None:
        if self._closed or not self.detached or self.anonymous_frozen:
            raise ValueError("attempt-bound anonymous panel freeze state is invalid")
        originals = self.files
        frozen: list[_HeldFile] = []
        try:
            for item in originals:
                frozen.append(_anonymous_clone(item))
            by_relative = {item.relative_path: item for item in frozen}
            if self.source is not None:
                self.source = replace(
                    self.source,
                    files=tuple(
                        _descriptor_path(by_relative[path].descriptor)
                        for path in self.partition_relatives
                    ),
                )
            self.files = tuple(frozen)
            self.anonymous_frozen = True
            self.assert_bound()
        except BaseException:
            for item in reversed(frozen):
                os.close(item.descriptor)
            raise
        finally:
            if self.anonymous_frozen:
                for item in reversed(originals):
                    os.close(item.descriptor)


def bind_panel_snapshot(
    root: Path,
    *,
    datasets_fd: int,
    expected_manifest_sha256: str,
    period: Period,
) -> FrozenPanelSnapshot:
    root_fd = open_directory_at(
        datasets_fd, root.name, label="attempt-bound final daily panel"
    )
    held: list[_HeldFile] = []
    try:
        _open_files(root_fd, prefix="", held=held)
        by_relative = {item.relative_path: item for item in held}
        data_manifest = by_relative.get("data_manifest.json")
        manifest_bytes = (
            _read_held_file(data_manifest) if data_manifest is not None else None
        )
        if (
            manifest_bytes is None
            or hashlib.sha256(manifest_bytes).hexdigest()
            != expected_manifest_sha256
        ):
            raise ValueError("attempt-bound final daily panel identity differs")
        source: DailyPanelSource | None = None
        partition_relatives: tuple[str, ...] = ()
        if "manifest.json" in by_relative:
            validated = validate_panel_source(root, period)
            _verify_source_against_held_files(validated, by_relative)
            partition_relatives = tuple(
                path.relative_to(root).as_posix() for path in validated.files
            )
            held_partitions = tuple(
                _descriptor_path(by_relative[path].descriptor)
                for path in partition_relatives
            )
            source = replace(validated, files=held_partitions)
        snapshot = FrozenPanelSnapshot(
            root=root,
            datasets_fd=datasets_fd,
            root_fd=root_fd,
            root_identity=directory_identity(root_fd),
            source=source,
            files=tuple(held),
            partition_relatives=partition_relatives,
            manifest_sha256=expected_manifest_sha256,
        )
        snapshot.assert_bound()
        return snapshot
    except BaseException:
        for item in reversed(held):
            os.close(item.descriptor)
        os.close(root_fd)
        raise


def _open_files(directory_fd: int, *, prefix: str, held: list[_HeldFile]) -> None:
    for name in sorted(os.listdir(directory_fd)):
        relative = f"{prefix}/{name}" if prefix else name
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = open_directory_at(
                directory_fd, name, label="attempt-bound panel directory"
            )
            try:
                _open_files(child_fd, prefix=relative, held=held)
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("attempt-bound final daily panel contains an unsafe entry")
        descriptor = os.open(
            name, os.O_RDONLY | no_follow_flag(), dir_fd=directory_fd
        )
        opened = os.fstat(descriptor)
        held.append(
            _HeldFile(
                relative,
                descriptor,
                (opened.st_dev, opened.st_ino),
                opened.st_size,
                _hash_descriptor(descriptor, opened.st_size),
            )
        )


def _unlink_bound_tree(
    directory_fd: int, *, prefix: str, held: dict[str, _HeldFile]
) -> None:
    for name in sorted(os.listdir(directory_fd)):
        relative = f"{prefix}/{name}" if prefix else name
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = open_directory_at(
                directory_fd, name, label="attempt-bound panel detach directory"
            )
            try:
                _unlink_bound_tree(child_fd, prefix=relative, held=held)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
            continue
        item = held.get(relative)
        if (
            item is None
            or not stat.S_ISREG(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != item.identity
        ):
            raise ValueError("attempt-bound final daily panel detach identity differs")
        os.unlink(name, dir_fd=directory_fd)


def _anonymous_clone(item: _HeldFile) -> _HeldFile:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.tmpfile.argtypes = []
    libc.tmpfile.restype = ctypes.c_void_p
    libc.fileno.argtypes = [ctypes.c_void_p]
    libc.fileno.restype = ctypes.c_int
    libc.fclose.argtypes = [ctypes.c_void_p]
    libc.fclose.restype = ctypes.c_int
    stream = libc.tmpfile()
    if not stream:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    writer = libc.fileno(stream)
    if writer < 0:
        error = ctypes.get_errno()
        libc.fclose(stream)
        raise OSError(error, os.strerror(error))
    reader: int | None = None
    try:
        digest = hashlib.sha256()
        offset = 0
        while offset < item.size_bytes:
            chunk = os.pread(
                item.descriptor,
                min(1024 * 1024, item.size_bytes - offset),
                offset,
            )
            if not chunk:
                raise ValueError("attempt-bound panel changed during anonymous freeze")
            view = memoryview(chunk)
            while view:
                written = os.write(writer, view)
                if written <= 0:
                    raise OSError("anonymous panel freeze made no write progress")
                view = view[written:]
            digest.update(chunk)
            offset += len(chunk)
        if digest.hexdigest() != item.sha256:
            raise ValueError("attempt-bound panel changed during anonymous freeze")
        os.fsync(writer)
        reader = os.open(f"/dev/fd/{writer}", os.O_RDONLY | no_follow_flag())
        opened = os.fstat(reader)
        if opened.st_nlink != 0:
            raise ValueError("anonymous panel freeze unexpectedly has a name")
        if opened.st_size != item.size_bytes:
            raise ValueError("anonymous panel freeze size differs")
        return _HeldFile(
            relative_path=item.relative_path,
            descriptor=reader,
            identity=(opened.st_dev, opened.st_ino),
            size_bytes=opened.st_size,
            sha256=item.sha256,
        )
    except BaseException:
        if reader is not None:
            os.close(reader)
        raise
    finally:
        if libc.fclose(stream) != 0:
            error = ctypes.get_errno()
            if reader is not None:
                os.close(reader)
            raise OSError(error, os.strerror(error))


def _read_held_file(item: _HeldFile) -> bytes:
    metadata = os.fstat(item.descriptor)
    if (metadata.st_dev, metadata.st_ino) != item.identity:
        raise ValueError("attempt-bound final daily panel file identity changed")
    chunks: list[bytes] = []
    offset = 0
    while offset < item.size_bytes:
        chunk = os.pread(item.descriptor, min(1024 * 1024, item.size_bytes - offset), offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    payload = b"".join(chunks)
    if len(payload) != item.size_bytes:
        raise ValueError("attempt-bound final daily panel file size changed")
    if hashlib.sha256(payload).hexdigest() != item.sha256:
        raise ValueError("attempt-bound final daily panel file bytes changed")
    return payload


def _hash_descriptor(descriptor: int, size_bytes: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < size_bytes:
        chunk = os.pread(descriptor, min(1024 * 1024, size_bytes - offset), offset)
        if not chunk:
            break
        digest.update(chunk)
        offset += len(chunk)
    if offset != size_bytes:
        raise ValueError("attempt-bound final daily panel file size changed")
    return digest.hexdigest()


def _verify_source_against_held_files(
    source: DailyPanelSource, held: dict[str, _HeldFile]
) -> None:
    manifest_file = held.get("manifest.json")
    quality_file = held.get("quality_issues.json")
    if manifest_file is None or quality_file is None:
        raise ValueError("attempt-bound final daily panel evidence is incomplete")
    manifest_bytes = _read_held_file(manifest_file)
    if json.loads(manifest_bytes) != source.manifest:
        raise ValueError("attempt-bound final daily panel manifest changed")
    for record in source.manifest["partitions"]:  # validated by validate_panel_source
        relative = str(record["relative_path"])
        item = held.get(relative)
        if (
            item is None
            or item.sha256 != record["sha256"]
            or item.size_bytes != record["size_bytes"]
        ):
            raise ValueError("attempt-bound final daily panel partition changed")
    quality = source.quality_record
    if (
        quality is None
        or quality_file.sha256 != quality["sha256"]
        or quality_file.size_bytes != quality["size_bytes"]
    ):
        raise ValueError("attempt-bound final daily panel quality evidence changed")


def _descriptor_path(descriptor: int) -> Path:
    proc = Path("/proc/self/fd")
    return (proc if proc.exists() else Path("/dev/fd")) / str(descriptor)
