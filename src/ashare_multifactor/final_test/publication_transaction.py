"""One held descriptor chain for final-test publication and recovery."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path

from ashare_multifactor.audit.publication import (
    HeldPublishedRelease,
    PublishedRelease,
    publish_release_at,
    resolve_release_package_at,
    restore_current_at,
)
from ashare_multifactor.final_test.recovery_secure_fs import (
    assert_directory_entry,
    directory_identity,
    open_directory_at,
    open_directory_path,
)
from ashare_multifactor.final_test.registry import (
    append_attempt_outcome_at,
    append_prepared_publication_at,
    recover_prepared_publication_at,
    resolve_prepared_publication_at,
)
from ashare_multifactor.final_test.final_root_binding import FinalRootBinding
from ashare_multifactor.final_test.secure_attempt_staging import AttemptDirectories


class PublicationNamespaceChanged(ValueError):
    """The canonical final-test namespace no longer names the held transaction."""


@dataclass
class FinalPublicationTransaction:
    final_root: Path
    attempt_id: str
    data_root_parent_fd: int
    data_root_fd: int
    processed_fd: int
    final_fd: int
    attempts_fd: int
    releases_fd: int
    attempt_runs_fd: int
    attempt_fd: int
    datasets_fd: int
    artifacts_fd: int
    data_root_identity: tuple[int, int]
    processed_identity: tuple[int, int]
    final_identity: tuple[int, int]
    attempts_identity: tuple[int, int]
    releases_identity: tuple[int, int]
    attempt_runs_identity: tuple[int, int]
    attempt_identity: tuple[int, int]
    datasets_identity: tuple[int, int]
    artifacts_identity: tuple[int, int]

    @classmethod
    def open(cls, final_root: Path, *, attempt_id: str) -> FinalPublicationTransaction:
        data_root = final_root.parent.parent
        descriptors: list[int] = []
        try:
            data_root_parent_fd = open_directory_path(
                data_root.parent, label="final-test data-root parent"
            )
            descriptors.append(data_root_parent_fd)
            data_root_fd = open_directory_at(
                data_root_parent_fd, data_root.name, label="final-test data root"
            )
            descriptors.append(data_root_fd)
            processed_fd = open_directory_at(
                data_root_fd, "processed", label="final-test processed root"
            )
            descriptors.append(processed_fd)
            final_fd = open_directory_at(
                processed_fd, "final_test", label="final-test root"
            )
            descriptors.append(final_fd)
            attempts_fd = open_directory_at(
                final_fd, "attempts", label="final-test registry"
            )
            descriptors.append(attempts_fd)
            try:
                os.mkdir("releases", mode=0o700, dir_fd=final_fd)
            except FileExistsError:
                pass
            releases_fd = open_directory_at(
                final_fd, "releases", label="final-test releases"
            )
            descriptors.append(releases_fd)
            attempt_runs_fd = open_directory_at(
                final_fd, "attempt_runs", label="final-test attempt-runs"
            )
            descriptors.append(attempt_runs_fd)
            attempt_fd = open_directory_at(
                attempt_runs_fd, attempt_id, label="final-test attempt"
            )
            descriptors.append(attempt_fd)
            datasets_fd = open_directory_at(
                attempt_fd, "datasets", label="final-test attempt datasets"
            )
            descriptors.append(datasets_fd)
            artifacts_fd = open_directory_at(
                attempt_fd, "artifacts", label="final-test attempt artifacts"
            )
            descriptors.append(artifacts_fd)
            transaction = cls(
                final_root=final_root,
                attempt_id=attempt_id,
                data_root_parent_fd=data_root_parent_fd,
                data_root_fd=data_root_fd,
                processed_fd=processed_fd,
                final_fd=final_fd,
                attempts_fd=attempts_fd,
                releases_fd=releases_fd,
                attempt_runs_fd=attempt_runs_fd,
                attempt_fd=attempt_fd,
                datasets_fd=datasets_fd,
                artifacts_fd=artifacts_fd,
                data_root_identity=directory_identity(data_root_fd),
                processed_identity=directory_identity(processed_fd),
                final_identity=directory_identity(final_fd),
                attempts_identity=directory_identity(attempts_fd),
                releases_identity=directory_identity(releases_fd),
                attempt_runs_identity=directory_identity(attempt_runs_fd),
                attempt_identity=directory_identity(attempt_fd),
                datasets_identity=directory_identity(datasets_fd),
                artifacts_identity=directory_identity(artifacts_fd),
            )
            transaction.assert_bound()
            return transaction
        except BaseException:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            raise

    def __enter__(self) -> FinalPublicationTransaction:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        for descriptor in (
            self.artifacts_fd,
            self.datasets_fd,
            self.attempt_fd,
            self.attempt_runs_fd,
            self.releases_fd,
            self.attempts_fd,
            self.final_fd,
            self.processed_fd,
            self.data_root_fd,
            self.data_root_parent_fd,
        ):
            os.close(descriptor)

    def assert_bound(self) -> None:
        try:
            self._assert_bound()
        except ValueError as error:
            raise PublicationNamespaceChanged(
                "final publication namespace identity changed"
            ) from error

    def _assert_bound(self) -> None:
        assert_directory_entry(
            self.data_root_parent_fd,
            self.final_root.parent.parent.name,
            expected=self.data_root_identity,
            label="final-test data root",
        )
        assert_directory_entry(
            self.data_root_fd,
            "processed",
            expected=self.processed_identity,
            label="final-test processed root",
        )
        assert_directory_entry(
            self.processed_fd,
            "final_test",
            expected=self.final_identity,
            label="final-test root",
        )
        for name, identity, label in (
            ("attempts", self.attempts_identity, "final-test registry"),
            ("releases", self.releases_identity, "final-test releases"),
            ("attempt_runs", self.attempt_runs_identity, "final-test attempt-runs"),
        ):
            assert_directory_entry(
                self.final_fd, name, expected=identity, label=label
            )
        assert_directory_entry(
            self.attempt_runs_fd,
            self.attempt_id,
            expected=self.attempt_identity,
            label="final-test attempt",
        )
        assert_directory_entry(
            self.attempt_fd,
            "datasets",
            expected=self.datasets_identity,
            label="final-test attempt datasets",
        )
        assert_directory_entry(
            self.attempt_fd,
            "artifacts",
            expected=self.artifacts_identity,
            label="final-test attempt artifacts",
        )

    def crosscheck_attempt(self, directories: AttemptDirectories) -> None:
        expected = (
            (self.processed_identity, directory_identity(directories.root_parent_fd)),
            (self.final_identity, directories.root_identity),
            (self.attempt_runs_identity, directories.parent_identity),
            (self.attempt_identity, directories.attempt_identity),
            (self.datasets_identity, directories.datasets_identity),
            (self.artifacts_identity, directories.artifacts_identity),
        )
        if any(left != right for left, right in expected):
            raise PublicationNamespaceChanged(
                "final publication attempt descriptor chain differs"
            )
        self.assert_bound()

    def crosscheck_root(self, binding: FinalRootBinding) -> None:
        expected = (
            (self.data_root_identity, binding.data_identity),
            (self.processed_identity, binding.processed_identity),
            (self.final_identity, binding.final_identity),
            (self.attempts_identity, binding.attempts_identity),
        )
        if any(left != right for left, right in expected):
            raise PublicationNamespaceChanged(
                "final publication root descriptor chain differs"
            )
        binding.assert_bound()
        self.assert_bound()

    def crosscheck_current(self, held: HeldPublishedRelease) -> None:
        expected = (
            (self.processed_identity, directory_identity(held.root_parent_fd)),
            (self.final_identity, held.root_identity),
            (self.releases_identity, held.releases_identity),
        )
        if any(left != right for left, right in expected):
            raise PublicationNamespaceChanged(
                "CURRENT publication descriptor chain differs"
            )
        self.assert_bound()

    def append_prepared(
        self,
        *,
        release_run_id: str,
        sealed_protocol_sha256: str,
        publication_identity: Mapping[str, object],
    ) -> dict[str, object]:
        self.assert_bound()
        result = append_prepared_publication_at(
            self.attempts_fd,
            attempt_id=self.attempt_id,
            release_run_id=release_run_id,
            sealed_protocol_sha256=sealed_protocol_sha256,
            publication_identity=publication_identity,
        )
        self.assert_bound()
        return result

    def resolve_prepared(
        self,
        *,
        release_run_id: str,
        sealed_protocol_sha256: str,
        publication_identity: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        self.assert_bound()
        result = resolve_prepared_publication_at(
            self.attempts_fd,
            attempt_id=self.attempt_id,
            release_run_id=release_run_id,
            sealed_protocol_sha256=sealed_protocol_sha256,
            publication_identity=publication_identity,
        )
        self.assert_bound()
        return result

    def publish(
        self,
        *,
        run_id: str,
        release_files: Mapping[str, bytes],
        manifest_metadata: Mapping[str, object],
        current_must_be_absent: bool,
        fail_before_switch: bool = False,
    ) -> PublishedRelease:
        self.assert_bound()
        release = publish_release_at(
            self.final_root,
            root_parent_fd=self.processed_fd,
            root_fd=self.final_fd,
            releases_fd=self.releases_fd,
            run_id=run_id,
            frozen_release_files=release_files,
            manifest_metadata=manifest_metadata,
            current_must_be_absent=current_must_be_absent,
            fail_before_switch=fail_before_switch,
        )
        self.assert_bound()
        return release

    def append_success(self, release: PublishedRelease, *, reason: str) -> None:
        self.assert_bound()
        append_attempt_outcome_at(
            self.attempts_fd,
            attempt_id=self.attempt_id,
            status="succeeded",
            authoritative=True,
            reason=reason,
            release_run_id=release.run_id,
            release_manifest_sha256=release.manifest_sha256,
        )
        self.assert_bound()

    def recover_prepared(self, release: PublishedRelease) -> None:
        self.assert_bound()
        recover_prepared_publication_at(
            self.attempts_fd,
            attempt_id=self.attempt_id,
            current={
                "run_id": release.run_id,
                "manifest_sha256": release.manifest_sha256,
            },
        )
        self.assert_bound()

    def resolve_release_package(
        self, run_id: str
    ) -> tuple[PublishedRelease, dict[str, bytes], dict[str, object]]:
        self.assert_bound()
        package = resolve_release_package_at(self.final_root, self.releases_fd, run_id)
        self.assert_bound()
        return package

    def restore_current(self, release: PublishedRelease) -> None:
        self.assert_bound()
        restore_current_at(self.final_fd, self.releases_fd, release)
        self.assert_bound()

    def release_exists(self, run_id: str) -> bool:
        try:
            os.stat(run_id, dir_fd=self.releases_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True
