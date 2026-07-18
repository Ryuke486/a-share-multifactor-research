"""Descriptor-anchored temporary snapshots for failed-attempt archival."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
import stat
from threading import RLock
from typing import Iterator
from uuid import uuid4

from ashare_multifactor.final_test.recovery_secure_fs import (
    directory_identity,
    no_follow_flag,
    open_directory_at,
)


_CWD_LOCK = RLock()


@dataclass(frozen=True)
class DirectorySnapshot:
    """A private copy of one directory tree, held by an open descriptor."""

    name: str
    descriptor: int
    identity: tuple[int, int]


@contextmanager
def copied_directory_snapshot(
    source_fd: int,
    destination_parent_fd: int,
    *,
    label: str,
) -> Iterator[DirectorySnapshot]:
    """Copy regular source evidence through directory descriptors, then remove it."""
    name = f".incident-verify-{uuid4().hex}"
    os.mkdir(name, mode=0o700, dir_fd=destination_parent_fd)
    snapshot_fd = open_directory_at(
        destination_parent_fd,
        name,
        label=f"{label} verification snapshot",
    )
    try:
        _copy_tree_at(source_fd, snapshot_fd, label=label)
        os.fsync(snapshot_fd)
        os.fsync(destination_parent_fd)
        yield DirectorySnapshot(
            name=name,
            descriptor=snapshot_fd,
            identity=directory_identity(snapshot_fd),
        )
    finally:
        os.close(snapshot_fd)
        _remove_tree_at(destination_parent_fd, name)
        os.fsync(destination_parent_fd)


@contextmanager
def working_directory_at(directory_fd: int) -> Iterator[None]:
    """Run path-based validators relative to one already-open directory."""
    with _CWD_LOCK:
        previous_fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fchdir(directory_fd)
            yield
        finally:
            os.fchdir(previous_fd)
            os.close(previous_fd)


def _copy_tree_at(source_fd: int, destination_fd: int, *, label: str) -> None:
    for name in sorted(os.listdir(source_fd)):
        metadata = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"{label} contains a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            os.mkdir(name, mode=0o700, dir_fd=destination_fd)
            child_source_fd = open_directory_at(
                source_fd,
                name,
                label=f"{label} child directory",
            )
            child_destination_fd = open_directory_at(
                destination_fd,
                name,
                label=f"{label} snapshot child directory",
            )
            try:
                _copy_tree_at(child_source_fd, child_destination_fd, label=label)
                os.fsync(child_destination_fd)
            finally:
                os.close(child_destination_fd)
                os.close(child_source_fd)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} contains a non-regular file")
        _copy_file_at(source_fd, destination_fd, name)


def _copy_file_at(source_fd: int, destination_fd: int, name: str) -> None:
    source = os.open(name, os.O_RDONLY | no_follow_flag(), dir_fd=source_fd)
    destination = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow_flag(),
        0o600,
        dir_fd=destination_fd,
    )
    try:
        while chunk := os.read(source, 1024 * 1024):
            _write_all(destination, chunk)
        os.fsync(destination)
    finally:
        os.close(destination)
        os.close(source)


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("failed to write incident verification snapshot")
        offset += written


def _remove_tree_at(parent_fd: int, name: str) -> None:
    try:
        directory_fd = open_directory_at(
            parent_fd,
            name,
            label="incident verification snapshot",
        )
    except FileNotFoundError:
        return
    try:
        _remove_tree_contents(directory_fd)
    finally:
        os.close(directory_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _remove_tree_contents(directory_fd: int) -> None:
    for name in sorted(os.listdir(directory_fd)):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = open_directory_at(
                directory_fd,
                name,
                label="incident verification snapshot child",
            )
            try:
                _remove_tree_contents(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)
