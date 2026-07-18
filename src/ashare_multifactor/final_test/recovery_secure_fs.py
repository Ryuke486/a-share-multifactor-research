"""No-follow, directory-fd filesystem primitives for final-test recovery."""

from __future__ import annotations

import ctypes
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Iterator, Mapping


def atomic_rename_no_replace(source: Path, destination: Path) -> None:
    with opened_directory(source.parent, label="rename source parent") as source_fd:
        with opened_directory(
            destination.parent,
            label="rename destination safe directory",
        ) as destination_fd:
            atomic_rename_no_replace_at(
                source_fd,
                source.name,
                destination_fd,
                destination.name,
            )


def atomic_rename_no_replace_at(
    source_fd: int,
    source_name: str,
    destination_fd: int,
    destination_name: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source_name)
    destination_bytes = os.fsencode(destination_name)
    if sys.platform == "darwin":
        function = getattr(libc, "renameatx_np", None)
        if function is None:
            raise RuntimeError("atomic no-replace renameat is unavailable")
        result = function(
            ctypes.c_int(source_fd),
            source_bytes,
            ctypes.c_int(destination_fd),
            destination_bytes,
            ctypes.c_uint(0x00000004),
        )
    elif sys.platform.startswith("linux"):
        function = getattr(libc, "renameat2", None)
        if function is None:
            raise RuntimeError("atomic no-replace renameat is unavailable")
        result = function(
            ctypes.c_int(source_fd),
            source_bytes,
            ctypes.c_int(destination_fd),
            destination_bytes,
            ctypes.c_uint(1),
        )
    else:
        raise RuntimeError("atomic no-replace directory publication is unsupported")
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError("recovery claim target already exists")
    raise OSError(error_number, os.strerror(error_number), destination_name)


def directory_records_at(
    directory_fd: int,
    *,
    prefix: str,
    role: str,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for name in sorted(os.listdir(directory_fd)):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        relative = f"{prefix}/{name}"
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("interrupted archive contains a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            with opened_directory_at(
                directory_fd,
                name,
                label="archived child directory",
            ) as child_fd:
                records.extend(
                    directory_records_at(child_fd, prefix=relative, role=role)
                )
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("interrupted archive contains a non-regular file")
        descriptor = os.open(
            name,
            os.O_RDONLY | no_follow_flag(),
            dir_fd=directory_fd,
        )
        try:
            digest = hashlib.sha256()
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            current = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        records.append(
            {
                "path": relative,
                "role": role,
                "sha256": digest.hexdigest(),
                "size_bytes": current.st_size,
            }
        )
    return records


@contextmanager
def opened_directory(path: Path, *, label: str) -> Iterator[int]:
    descriptor = open_directory_path(path, label=label)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def opened_directory_at(
    parent_fd: int, name: str, *, label: str
) -> Iterator[int]:
    descriptor = open_directory_at(parent_fd, name, label=label)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def open_directory_path(path: Path, *, label: str) -> int:
    try:
        return os.open(path, directory_flags())
    except FileNotFoundError:
        raise
    except OSError as error:
        raise ValueError(f"{label} is not a safe directory") from error


def open_directory_at(parent_fd: int, name: str, *, label: str) -> int:
    if os.open not in os.supports_dir_fd:
        raise RuntimeError("secure directory-relative open is unavailable")
    try:
        return os.open(name, directory_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise ValueError(f"{label} is not a safe directory") from error


def directory_flags() -> int:
    directory = getattr(os, "O_DIRECTORY", None)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if directory is None or no_follow is None:
        raise RuntimeError("secure no-follow directory open is unavailable")
    return os.O_RDONLY | directory | no_follow


def no_follow_flag() -> int:
    value = getattr(os, "O_NOFOLLOW", None)
    if value is None:
        raise RuntimeError("secure no-follow file open is unavailable")
    return value


def descriptor_path(descriptor: int) -> Path:
    """Return a path alias for one already-open descriptor.

    This is only for APIs which accept a path spelling but never traverse a
    child of that spelling.  Namespace-sensitive filesystem work must still
    use the ``*_at`` primitives above.
    """
    proc = Path("/proc/self/fd")
    return (proc if proc.exists() else Path("/dev/fd")) / str(descriptor)


def directory_identity(descriptor: int) -> tuple[int, int]:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("opened archive is not a directory")
    return metadata.st_dev, metadata.st_ino


def assert_directory_entry(
    parent_fd: int,
    name: str,
    *,
    expected: tuple[int, int],
    label: str,
) -> None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as error:
        raise ValueError(f"{label} identity changed") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} uses a symlink or is not a safe directory")
    if (metadata.st_dev, metadata.st_ino) != expected:
        raise ValueError(f"{label} identity changed")


def read_json_at(parent_fd: int, name: str, *, label: str) -> dict[str, object]:
    try:
        payload = read_bytes_at(parent_fd, name, label=label)
        value = json.loads(payload)
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is missing or invalid") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} is missing or invalid")
    return value


def read_bytes_at(parent_fd: int, name: str, *, label: str) -> bytes:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | no_follow_flag(),
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        raise
    except OSError as error:
        raise ValueError(f"{label} uses a symlink or is unsafe") from error
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def write_bytes_exclusive_at(parent_fd: int, name: str, payload: bytes) -> None:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow_flag(),
        0o600,
        dir_fd=parent_fd,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def json_bytes(payload: Mapping[str, object]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
