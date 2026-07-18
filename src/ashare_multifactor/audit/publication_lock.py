"""Descriptor-relative advisory locks for publication readers and writers."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from typing import Iterator


@contextmanager
def publication_read_lock_at(parent_fd: int, name: str) -> Iterator[None]:
    with _publication_lock_at(parent_fd, name, fcntl.LOCK_SH):
        yield


@contextmanager
def publication_write_lock_at(parent_fd: int, name: str) -> Iterator[None]:
    with _publication_lock_at(parent_fd, name, fcntl.LOCK_EX):
        yield


@contextmanager
def _publication_lock_at(parent_fd: int, name: str, operation: int) -> Iterator[None]:
    descriptor = _open_or_create_lock_at(parent_fd, name)
    try:
        fcntl.flock(descriptor, operation)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _open_or_create_lock_at(parent_fd: int, name: str) -> int:
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW")
    try:
        return os.open(
            name,
            flags | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_fd,
        )
    except FileExistsError:
        return os.open(name, flags, dir_fd=parent_fd)
