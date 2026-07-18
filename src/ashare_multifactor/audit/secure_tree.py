"""Small directory-fd primitives for writing immutable evidence trees."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
import errno
import os
from pathlib import PurePosixPath
import stat
import sys
from typing import Iterator, Mapping


def write_frozen_tree_at(
    parent_fd: int,
    name: str,
    files: Mapping[str, bytes],
    *,
    resumable: bool,
    label: str,
) -> None:
    """Create one exact no-symlink tree below an already trusted directory FD."""
    expected = _validated_files(files)
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError:
        if not resumable:
            raise FileExistsError(f"{label} already exists") from None
    with _opened_directory_at(parent_fd, name, label=label) as root_fd:
        _write_level(root_fd, expected, resumable=resumable, label=label)
        if _read_level(root_fd, prefix="", label=label) != expected:
            raise ValueError(f"{label} bytes changed during materialization")


def verify_frozen_tree_at(
    parent_fd: int,
    name: str,
    files: Mapping[str, bytes],
    *,
    label: str,
) -> None:
    expected = _validated_files(files)
    with _opened_directory_at(parent_fd, name, label=label) as root_fd:
        if _read_level(root_fd, prefix="", label=label) != expected:
            raise ValueError(f"{label} bytes differ from frozen snapshot")


def read_frozen_tree_at(directory_fd: int, *, label: str) -> dict[str, bytes]:
    """Read one exact tree through a caller-owned directory descriptor."""
    return _read_level(directory_fd, prefix="", label=label)


def atomic_rename_no_replace_at(
    parent_fd: int,
    source_name: str,
    destination_name: str,
) -> None:
    """Rename one child without ever replacing a competing destination."""
    libc = ctypes.CDLL(None, use_errno=True)
    source = os.fsencode(source_name)
    destination = os.fsencode(destination_name)
    if sys.platform == "darwin":
        function = getattr(libc, "renameatx_np", None)
        if function is None:
            raise RuntimeError("atomic no-replace renameat is unavailable")
        result = function(
            ctypes.c_int(parent_fd),
            source,
            ctypes.c_int(parent_fd),
            destination,
            ctypes.c_uint(0x00000004),
        )
    elif sys.platform.startswith("linux"):
        function = getattr(libc, "renameat2", None)
        if function is None:
            raise RuntimeError("atomic no-replace renameat is unavailable")
        result = function(
            ctypes.c_int(parent_fd),
            source,
            ctypes.c_int(parent_fd),
            destination,
            ctypes.c_uint(1),
        )
    else:
        raise RuntimeError("atomic no-replace renameat is unsupported")
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError("release destination already exists")
    raise OSError(error_number, os.strerror(error_number), destination_name)


def frozen_records(
    files: Mapping[str, bytes],
    *,
    prefix: str,
    role: str,
) -> list[dict[str, object]]:
    import hashlib

    expected = _validated_files(files)
    return [
        {
            "path": f"{prefix}/{path}" if prefix else path,
            "role": role,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }
        for path, payload in sorted(expected.items())
    ]


def _validated_files(files: Mapping[str, bytes]) -> dict[str, bytes]:
    if not files:
        raise ValueError("frozen tree is empty")
    validated: dict[str, bytes] = {}
    for path, payload in files.items():
        relative = PurePosixPath(path)
        if (
            not isinstance(path, str)
            or not isinstance(payload, bytes)
            or relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or path in validated
        ):
            raise ValueError("frozen tree path or payload is invalid")
        validated[path] = payload
    return validated


def _write_level(
    directory_fd: int,
    files: Mapping[str, bytes],
    *,
    resumable: bool,
    label: str,
) -> None:
    direct_files: dict[str, bytes] = {}
    children: dict[str, dict[str, bytes]] = {}
    for path, payload in files.items():
        head, separator, tail = path.partition("/")
        if separator:
            children.setdefault(head, {})[tail] = payload
        else:
            direct_files[head] = payload
    expected_names = set(direct_files) | set(children)
    existing_names = set(os.listdir(directory_fd))
    if not existing_names.issubset(expected_names):
        raise ValueError(f"{label} contains an unexpected entry")
    for name, payload in sorted(direct_files.items()):
        _write_file_at(
            directory_fd,
            name,
            payload,
            resumable=resumable,
            label=label,
        )
    for name, child_files in sorted(children.items()):
        try:
            os.mkdir(name, mode=0o700, dir_fd=directory_fd)
        except FileExistsError:
            pass
        with _opened_directory_at(directory_fd, name, label=label) as child_fd:
            _write_level(
                child_fd,
                child_files,
                resumable=resumable,
                label=label,
            )
    if set(os.listdir(directory_fd)) != expected_names:
        raise ValueError(f"{label} inventory changed during materialization")


def _write_file_at(
    directory_fd: int,
    name: str,
    payload: bytes,
    *,
    resumable: bool,
    label: str,
) -> None:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW")
    flags |= 0 if resumable else os.O_EXCL
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        current = os.fstat(descriptor)
        if not stat.S_ISREG(current.st_mode):
            raise ValueError(f"{label} contains a non-regular file")
        if current.st_size:
            if not resumable:
                raise FileExistsError(f"{label} file already exists")
            os.lseek(descriptor, 0, os.SEEK_SET)
            existing = os.read(descriptor, current.st_size)
            if not payload.startswith(existing):
                raise ValueError(f"{label} partial bytes differ")
        else:
            existing = b""
        if len(existing) < len(payload):
            os.lseek(descriptor, 0, os.SEEK_END)
            remainder = memoryview(payload)[len(existing) :]
            while remainder:
                written = os.write(descriptor, remainder)
                if written <= 0:
                    raise OSError(f"{label} write made no progress")
                remainder = remainder[written:]
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_level(directory_fd: int, *, prefix: str, label: str) -> dict[str, bytes]:
    before = os.fstat(directory_fd)
    names = tuple(sorted(os.listdir(directory_fd)))
    files: dict[str, bytes] = {}
    for name in names:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        relative = f"{prefix}/{name}" if prefix else name
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"{label} uses a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            with _opened_directory_at(directory_fd, name, label=label) as child_fd:
                files.update(_read_level(child_fd, prefix=relative, label=label))
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} contains a non-regular file")
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW"),
            dir_fd=directory_fd,
        )
        try:
            opened = os.fstat(descriptor)
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            closed = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        identities = {
            (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
            for item in (metadata, opened, closed, current)
        }
        payload = b"".join(chunks)
        if len(identities) != 1 or len(payload) != opened.st_size:
            raise ValueError(f"{label} file changed during verification")
        files[relative] = payload
    after = os.fstat(directory_fd)
    if (
        tuple(sorted(os.listdir(directory_fd))) != names
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
    ):
        raise ValueError(f"{label} changed during verification")
    return files


@contextmanager
def _opened_directory_at(parent_fd: int, name: str, *, label: str) -> Iterator[int]:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY")
            | getattr(os, "O_NOFOLLOW"),
            dir_fd=parent_fd,
        )
    except OSError as error:
        raise ValueError(f"{label} is not a safe directory") from error
    try:
        yield descriptor
    finally:
        os.close(descriptor)
