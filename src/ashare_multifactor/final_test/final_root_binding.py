"""Long-lived canonical final-test root binding from claim through publication."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from ashare_multifactor.final_test.recovery_secure_fs import (
    assert_directory_entry,
    directory_identity,
    open_directory_at,
    open_directory_path,
)


class FinalRootNamespaceChanged(ValueError):
    """The canonical path no longer names the root claimed by this operation."""


@dataclass
class FinalRootBinding:
    final_root: Path
    data_parent_fd: int
    data_fd: int
    processed_fd: int
    final_fd: int
    attempts_fd: int
    data_identity: tuple[int, int]
    processed_identity: tuple[int, int]
    final_identity: tuple[int, int]
    attempts_identity: tuple[int, int]

    @classmethod
    def open(cls, final_root: Path) -> FinalRootBinding:
        data_root = final_root.parent.parent
        descriptors: list[int] = []
        try:
            data_parent_fd = open_directory_path(
                data_root.parent, label="final-test data-root parent"
            )
            descriptors.append(data_parent_fd)
            data_fd = open_directory_at(
                data_parent_fd, data_root.name, label="final-test data root"
            )
            descriptors.append(data_fd)
            processed_fd = open_directory_at(
                data_fd, final_root.parent.name, label="final-test processed root"
            )
            descriptors.append(processed_fd)
            final_fd = open_directory_at(
                processed_fd, final_root.name, label="final-test root"
            )
            descriptors.append(final_fd)
            attempts_fd = open_directory_at(
                final_fd, "attempts", label="final-test registry"
            )
            descriptors.append(attempts_fd)
            binding = cls(
                final_root=final_root,
                data_parent_fd=data_parent_fd,
                data_fd=data_fd,
                processed_fd=processed_fd,
                final_fd=final_fd,
                attempts_fd=attempts_fd,
                data_identity=directory_identity(data_fd),
                processed_identity=directory_identity(processed_fd),
                final_identity=directory_identity(final_fd),
                attempts_identity=directory_identity(attempts_fd),
            )
            binding.assert_bound()
            return binding
        except BaseException:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            raise

    def __enter__(self) -> FinalRootBinding:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        for descriptor in (
            self.attempts_fd,
            self.final_fd,
            self.processed_fd,
            self.data_fd,
            self.data_parent_fd,
        ):
            os.close(descriptor)

    def assert_bound(self) -> None:
        try:
            data_root = self.final_root.parent.parent
            assert_directory_entry(
                self.data_parent_fd,
                data_root.name,
                expected=self.data_identity,
                label="final-test data root",
            )
            assert_directory_entry(
                self.data_fd,
                self.final_root.parent.name,
                expected=self.processed_identity,
                label="final-test processed root",
            )
            assert_directory_entry(
                self.processed_fd,
                self.final_root.name,
                expected=self.final_identity,
                label="final-test root",
            )
            assert_directory_entry(
                self.final_fd,
                "attempts",
                expected=self.attempts_identity,
                label="final-test registry",
            )
        except ValueError as error:
            raise FinalRootNamespaceChanged(
                "claimed final-test root namespace identity changed"
            ) from error

    def crosscheck(
        self,
        *,
        processed_fd: int,
        final_identity: tuple[int, int],
        attempts_identity: tuple[int, int] | None = None,
    ) -> None:
        if (
            directory_identity(processed_fd) != self.processed_identity
            or final_identity != self.final_identity
            or (
                attempts_identity is not None
                and attempts_identity != self.attempts_identity
            )
        ):
            raise FinalRootNamespaceChanged(
                "claimed final-test root descriptor chain differs"
            )
        self.assert_bound()
