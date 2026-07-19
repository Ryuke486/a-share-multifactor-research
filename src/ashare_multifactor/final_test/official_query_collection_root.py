"""Identity-bound filesystem root for one official-query collection."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import stat

from ashare_multifactor.final_test.action_source_contract import (
    FinalActionSourceContract,
)
from ashare_multifactor.final_test.final_root_binding import FinalRootBinding
from ashare_multifactor.final_test.gate import FinalTestAuthorization
from ashare_multifactor.final_test.official_query_coverage import canonical_json_bytes
from ashare_multifactor.final_test.preparation import FinalTestPreparation
from ashare_multifactor.final_test.recovery_secure_fs import (
    assert_directory_entry,
    directory_identity,
    open_directory_at,
    read_bytes_at,
    write_bytes_exclusive_at,
)


COLLECTION_MANIFEST = "official_query_collection.json"
COVERAGE_DIRECTORY = "official_query_coverage"
_EVIDENCE_PARENT = "final_test_evidence"
_LOCK_NAME = ".official-query-collection.lock"
_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class EvidenceRoot:
    """Held descriptor chain for one attempt-specific evidence directory."""

    output_root: Path
    parent_fd: int
    parent_identity: tuple[int, int]
    root_fd: int
    root_identity: tuple[int, int]
    coverage_fd: int
    coverage_identity: tuple[int, int]


def expected_output_root(final_root: Path, attempt_id: str) -> Path:
    """Return the sole evidence directory permitted for one attempt."""
    return final_root.parent / _EVIDENCE_PARENT / attempt_id


@contextmanager
def open_evidence_root(
    root_binding: FinalRootBinding,
    *,
    attempt_id: str,
) -> Iterator[EvidenceRoot]:
    """Create or open an attempt-specific evidence root through held FDs."""
    parent_fd = _open_or_create_directory(
        root_binding.processed_fd,
        _EVIDENCE_PARENT,
        label="final-test evidence parent",
    )
    root_fd: int | None = None
    coverage_fd: int | None = None
    try:
        root_fd = _open_or_create_directory(
            parent_fd,
            attempt_id,
            label="final-test attempt evidence root",
        )
        coverage_fd = _open_or_create_directory(
            root_fd,
            COVERAGE_DIRECTORY,
            label="official query coverage root",
        )
        yield EvidenceRoot(
            output_root=expected_output_root(root_binding.final_root, attempt_id),
            parent_fd=parent_fd,
            parent_identity=directory_identity(parent_fd),
            root_fd=root_fd,
            root_identity=directory_identity(root_fd),
            coverage_fd=coverage_fd,
            coverage_identity=directory_identity(coverage_fd),
        )
    finally:
        if coverage_fd is not None:
            os.close(coverage_fd)
        if root_fd is not None:
            os.close(root_fd)
        os.close(parent_fd)


@contextmanager
def collection_lock(root_fd: int) -> Iterator[None]:
    """Serialize writers for one attempt without retaining any secret bytes."""
    try:
        descriptor = os.open(
            _LOCK_NAME,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=root_fd,
        )
    except OSError as error:
        raise ValueError("official query collection lock is unsafe") from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def assert_directory_identities(
    root_binding: FinalRootBinding,
    evidence: EvidenceRoot,
    *,
    attempt_id: str,
) -> None:
    """Reject any namespace replacement around held collection descriptors."""
    root_binding.assert_bound()
    assert_directory_entry(
        root_binding.processed_fd,
        _EVIDENCE_PARENT,
        expected=evidence.parent_identity,
        label="final-test evidence parent",
    )
    assert_directory_entry(
        evidence.parent_fd,
        attempt_id,
        expected=evidence.root_identity,
        label="final-test attempt evidence root",
    )
    assert_directory_entry(
        evidence.root_fd,
        COVERAGE_DIRECTORY,
        expected=evidence.coverage_identity,
        label="official query coverage root",
    )


def write_or_verify_collection_manifest(
    root_fd: int,
    *,
    preparation: FinalTestPreparation,
    authorization: FinalTestAuthorization,
    contract: FinalActionSourceContract,
) -> None:
    """Freeze the exact authorization and preparation identity once."""
    expected = canonical_json_bytes(
        collection_manifest_payload(preparation, authorization, contract)
    )
    if entry_exists(root_fd, COLLECTION_MANIFEST):
        existing = read_bytes_at(
            root_fd,
            COLLECTION_MANIFEST,
            label="official query collection manifest",
        )
        if existing != expected:
            raise ValueError("official query collection identity differs")
        return
    write_bytes_exclusive_at(root_fd, COLLECTION_MANIFEST, expected)
    os.fsync(root_fd)


def verify_collection_manifest(
    root_fd: int,
    *,
    preparation: FinalTestPreparation,
    authorization: FinalTestAuthorization,
    contract: FinalActionSourceContract,
) -> None:
    """Verify a stable collection manifest without altering evidence state."""
    expected = canonical_json_bytes(
        collection_manifest_payload(preparation, authorization, contract)
    )
    actual = read_bytes_at(
        root_fd,
        COLLECTION_MANIFEST,
        label="official query collection manifest",
    )
    if actual != expected:
        raise ValueError("official query collection identity differs")


def collection_manifest_payload(
    preparation: FinalTestPreparation,
    authorization: FinalTestAuthorization,
    contract: FinalActionSourceContract,
) -> dict[str, object]:
    """Return canonical public metadata; approval-key bytes are never included."""
    return {
        "schema_version": _SCHEMA_VERSION,
        "role": "official_query_collection",
        "attempt_id": authorization.attempt_id,
        "approval_id": authorization.approval_id,
        "git": {"commit": authorization.git_commit, "tree": authorization.git_tree},
        "stage8": {
            "run_id": authorization.robustness_release,
            "manifest_sha256": authorization.robustness_manifest_sha256,
            "lineage_sha256": authorization.robustness_lineage_sha256,
            "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        },
        "period": [
            authorization.test_period[0].isoformat(),
            authorization.test_period[1].isoformat(),
        ],
        "prepare_manifest_sha256": preparation.manifest_sha256,
        "symbol_count": preparation.symbol_count,
        "symbols_sha256": preparation.symbols_sha256,
        "supported_markets": list(contract.supported_markets),
        "categories": [
            {"category": category, "query_category": query_category}
            for category, query_category in contract.official_query_categories
        ],
        "coverage_relative_path": COVERAGE_DIRECTORY,
    }


@contextmanager
def opened_safe_directory(path: Path, *, label: str) -> Iterator[int]:
    """Open an existing absolute directory only when every ancestor is safe."""
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label} is missing or unsafe")
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise ValueError(f"{label} uses a symlink")
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def entry_exists(parent_fd: int, name: str) -> bool:
    """Check one non-symlink directory entry without following it."""
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("official query collection uses a symlink")
    return True


def _open_or_create_directory(parent_fd: int, name: str, *, label: str) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    return open_directory_at(parent_fd, name, label=label)
