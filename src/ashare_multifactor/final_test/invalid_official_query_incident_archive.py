"""Archive complete official-query evidence that is explicitly not reusable."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Iterator
from uuid import uuid4

from ashare_multifactor.final_test.final_root_binding import FinalRootBinding
from ashare_multifactor.final_test.gate import (
    FINAL_TEST_END,
    FINAL_TEST_START,
    FinalTestAuthorization,
)
from ashare_multifactor.final_test.official_query_collection_root import (
    COLLECTION_MANIFEST,
    COVERAGE_DIRECTORY,
    IDENTITIES_DIRECTORY,
    entry_exists,
)
from ashare_multifactor.final_test.official_query_coverage import canonical_json_bytes
from ashare_multifactor.final_test.official_query_packages import INDEX_NAME
from ashare_multifactor.final_test.preparation import (
    FinalTestPreparation,
    verify_preparation,
)
from ashare_multifactor.final_test.recovery_secure_fs import (
    assert_directory_entry,
    atomic_rename_no_replace_at,
    directory_identity,
    directory_records_at,
    open_directory_at,
    opened_directory_at,
    read_bytes_at,
    read_json_at,
    write_bytes_exclusive_at,
)
from ashare_multifactor.final_test.registry import (
    resolve_attempt_state_at,
    validate_publication_id,
)


_ARCHIVE_PARENT = "final_test_evidence_incidents"
_EVIDENCE_PARENT = "final_test_evidence"
_LOCK = ".official-query-collection.lock"
_MANIFEST = "invalid_complete_query_incident_manifest.json"
_MANIFEST_TEMP = re.compile(r"\.invalid-complete-query-incident\.[0-9a-f]{32}\.tmp")
_SYMBOL = re.compile(r"[0-9]{6}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_STATUS = "archived_invalid_complete_official_query_collection"
_ROLE = "archived_invalid_complete_official_query_evidence"
_ALLOWED_ROOTS = frozenset(
    {
        _LOCK,
        COLLECTION_MANIFEST,
        COVERAGE_DIRECTORY,
        IDENTITIES_DIRECTORY,
        "official_announcement_catalog",
        "official_document_workspace",
        _MANIFEST,
    }
)


def archive_invalid_complete_query_collection(
    processed_root: Path,
    *,
    attempt_id: str,
) -> Path:
    """Move terminal failed, complete but invalid query evidence without reuse."""
    validate_publication_id(attempt_id)
    _assert_safe_directory(processed_root, label="processed root")
    destination = processed_root / _ARCHIVE_PARENT / attempt_id
    source = processed_root / _EVIDENCE_PARENT / attempt_id
    if not source.exists():
        if destination.is_dir() and not destination.is_symlink():
            _verify_archive(destination, attempt_id=attempt_id, processed_root=processed_root)
            return destination
        raise FileNotFoundError(source)

    final_root = processed_root / "final_test"
    with FinalRootBinding.open(final_root) as binding:
        if binding.final_root.parent.absolute() != processed_root.absolute():
            raise ValueError("final-test processed root differs from evidence archive root")
        archive_fd = _open_archive_parent(binding.processed_fd)
        try:
            archive_identity = directory_identity(archive_fd)
            with opened_directory_at(
                binding.processed_fd,
                _EVIDENCE_PARENT,
                label="official-query evidence parent",
            ) as evidence_parent_fd:
                evidence_parent_identity = directory_identity(evidence_parent_fd)
                source_fd = open_directory_at(
                    evidence_parent_fd,
                    attempt_id,
                    label="invalid official-query incident source",
                )
                try:
                    source_identity = directory_identity(source_fd)
                    if entry_exists(archive_fd, attempt_id):
                        raise FileExistsError(
                            "invalid official-query incident destination already exists"
                        )
                    with _locked_collection(source_fd):
                        _assert_bound(
                            binding,
                            archive_fd=archive_fd,
                            archive_identity=archive_identity,
                            evidence_parent_fd=evidence_parent_fd,
                            evidence_parent_identity=evidence_parent_identity,
                            source_fd=source_fd,
                            source_identity=source_identity,
                            attempt_id=attempt_id,
                        )
                        _recover_orphan_manifest_temps(source_fd)
                        authorization, reason = _failed_authorization(
                            binding,
                            attempt_id=attempt_id,
                        )
                        preparation = verify_preparation(
                            final_root,
                            attempt_id=attempt_id,
                            authorization=authorization,
                            expected_state="failed",
                            root_binding=binding,
                        )
                        collection_bytes = read_bytes_at(
                            source_fd,
                            COLLECTION_MANIFEST,
                            label="invalid official-query collection manifest",
                        )
                        _assert_collection_binding(
                            collection_bytes,
                            authorization=authorization,
                            preparation=preparation,
                        )
                        coverage_index = _complete_coverage_index(source_fd)
                        _assert_source_entries(source_fd)
                        files = _records(source_fd, attempt_id=attempt_id)
                        package_count = _query_package_count(files)
                        manifest = _manifest_payload(
                            attempt_id=attempt_id,
                            reason=reason,
                            collection_bytes=collection_bytes,
                            coverage_index=coverage_index,
                            package_count=package_count,
                            evidence_parent_identity=evidence_parent_identity,
                            files=files,
                        )
                        _write_or_verify_manifest(
                            source_fd,
                            manifest=manifest,
                            files=files,
                        )
                        if _records(source_fd, attempt_id=attempt_id) != files:
                            raise ValueError(
                                "invalid official-query evidence changed during archival"
                            )
                        _assert_bound(
                            binding,
                            archive_fd=archive_fd,
                            archive_identity=archive_identity,
                            evidence_parent_fd=evidence_parent_fd,
                            evidence_parent_identity=evidence_parent_identity,
                            source_fd=source_fd,
                            source_identity=source_identity,
                            attempt_id=attempt_id,
                        )
                        atomic_rename_no_replace_at(
                            evidence_parent_fd,
                            attempt_id,
                            archive_fd,
                            attempt_id,
                        )
                        assert_directory_entry(
                            archive_fd,
                            attempt_id,
                            expected=source_identity,
                            label="invalid official-query incident destination",
                        )
                        assert_directory_entry(
                            binding.processed_fd,
                            _EVIDENCE_PARENT,
                            expected=evidence_parent_identity,
                            label="official-query evidence parent",
                        )
                        assert_directory_entry(
                            binding.processed_fd,
                            _ARCHIVE_PARENT,
                            expected=archive_identity,
                            label="invalid official-query incident archive",
                        )
                        os.fsync(evidence_parent_fd)
                        os.fsync(archive_fd)
                finally:
                    os.close(source_fd)
        finally:
            os.close(archive_fd)
    _verify_archive(destination, attempt_id=attempt_id, processed_root=processed_root)
    return destination


def _open_archive_parent(processed_fd: int) -> int:
    try:
        os.mkdir(_ARCHIVE_PARENT, mode=0o700, dir_fd=processed_fd)
    except FileExistsError:
        pass
    return open_directory_at(
        processed_fd,
        _ARCHIVE_PARENT,
        label="invalid official-query incident archive",
    )


@contextmanager
def _locked_collection(source_fd: int) -> Iterator[None]:
    try:
        descriptor = os.open(_LOCK, os.O_RDWR | os.O_NOFOLLOW, dir_fd=source_fd)
    except OSError as error:
        raise ValueError("invalid official-query incident lock is missing or unsafe") from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _failed_authorization(
    binding: FinalRootBinding,
    *,
    attempt_id: str,
) -> tuple[FinalTestAuthorization, str]:
    state = resolve_attempt_state_at(binding.attempts_fd, attempt_id)
    if state.get("state") != "failed" or state.get("authoritative") is not False:
        raise ValueError("invalid official-query incident attempt is not terminal failed")
    outcome = read_json_at(
        binding.attempts_fd,
        f"{attempt_id}.outcome.json",
        label="invalid official-query incident outcome",
    )
    reason = outcome.get("reason")
    fields = (
        "approval_id",
        "registered_at",
        "git_commit",
        "git_tree",
        "sealed_protocol_sha256",
        "robustness_release",
        "robustness_manifest_sha256",
        "robustness_lineage_sha256",
    )
    if (
        outcome.get("attempt_id") != attempt_id
        or outcome.get("status") != "failed"
        or outcome.get("authoritative") is not False
        or not isinstance(reason, str)
        or not reason
        or any(not isinstance(state.get(field), str) or not state[field] for field in fields)
    ):
        raise ValueError("invalid official-query incident outcome is invalid")
    return (
        FinalTestAuthorization(
            attempt_id=attempt_id,
            approval_id=str(state["approval_id"]),
            registered_at=str(state["registered_at"]),
            git_commit=str(state["git_commit"]),
            git_tree=str(state["git_tree"]),
            sealed_protocol_sha256=str(state["sealed_protocol_sha256"]),
            robustness_release=str(state["robustness_release"]),
            robustness_manifest_sha256=str(state["robustness_manifest_sha256"]),
            robustness_lineage_sha256=str(state["robustness_lineage_sha256"]),
            test_period=(FINAL_TEST_START, FINAL_TEST_END),
        ),
        reason,
    )


def _assert_collection_binding(
    payload: bytes,
    *,
    authorization: FinalTestAuthorization,
    preparation: FinalTestPreparation,
) -> None:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid official-query collection manifest is invalid") from error
    expected = {
        "schema_version": "1",
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
        "period": [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()],
        "prepare_manifest_sha256": preparation.manifest_sha256,
        "symbol_count": preparation.symbol_count,
        "symbols_sha256": preparation.symbols_sha256,
        "supported_markets": ["sh", "sz"],
        "categories": [
            {"category": "corporate_actions", "query_category": ""},
            {"category": "security_events", "query_category": ""},
        ],
        "coverage_relative_path": COVERAGE_DIRECTORY,
    }
    if not isinstance(value, dict) or payload != canonical_json_bytes(value) or value != expected:
        raise ValueError("invalid official-query collection identity differs from failed attempt")


def _complete_coverage_index(source_fd: int) -> bytes:
    coverage_fd = open_directory_at(
        source_fd,
        COVERAGE_DIRECTORY,
        label="invalid official-query coverage root",
    )
    try:
        if not entry_exists(coverage_fd, INDEX_NAME):
            raise ValueError("complete official-query coverage index is missing")
        return read_bytes_at(
            coverage_fd,
            INDEX_NAME,
            label="invalid official-query coverage index",
        )
    finally:
        os.close(coverage_fd)


def _assert_source_entries(source_fd: int) -> None:
    unexpected = sorted(set(os.listdir(source_fd)) - _ALLOWED_ROOTS)
    if unexpected:
        raise ValueError(f"invalid official-query incident contains unexpected evidence: {unexpected}")


def _records(source_fd: int, *, attempt_id: str) -> list[dict[str, object]]:
    return [
        record
        for record in directory_records_at(
            source_fd,
            prefix=f"{_EVIDENCE_PARENT}/{attempt_id}",
            role=_ROLE,
        )
        if record["path"] != f"{_EVIDENCE_PARENT}/{attempt_id}/{_MANIFEST}"
    ]


def _query_package_count(files: list[dict[str, object]]) -> int:
    return sum(
        1
        for record in files
        if isinstance(record.get("path"), str)
        and _is_query_package_manifest(record["path"])
    )


def _is_query_package_manifest(path: str) -> bool:
    parts = path.split("/")
    return (
        len(parts) == 9
        and parts[2:4] == [COVERAGE_DIRECTORY, "packages"]
        and parts[4] in {"corporate_actions", "security_events"}
        and parts[5] in {"sh", "sz"}
        and _SYMBOL.fullmatch(parts[6]) is not None
        and parts[7:] == ["query-package", "query_manifest.json"]
    )


def _manifest_payload(
    *,
    attempt_id: str,
    reason: str,
    collection_bytes: bytes,
    coverage_index: bytes,
    package_count: int,
    evidence_parent_identity: tuple[int, int],
    files: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "schema_version": "1",
        "attempt_id": attempt_id,
        "status": _STATUS,
        "reason": reason,
        "source_relative_path": f"{_EVIDENCE_PARENT}/{attempt_id}",
        "evidence_parent_identity": list(evidence_parent_identity),
        "coverage_index_present": True,
        "coverage_index_sha256": hashlib.sha256(coverage_index).hexdigest(),
        "collection_manifest_sha256": hashlib.sha256(collection_bytes).hexdigest(),
        "query_package_count": package_count,
        "reuse_permitted": False,
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }


def _write_or_verify_manifest(
    source_fd: int,
    *,
    manifest: dict[str, object],
    files: list[dict[str, object]],
) -> None:
    payload = canonical_json_bytes(manifest)
    try:
        _publish_manifest(source_fd, payload)
    except FileExistsError:
        existing = read_json_at(
            source_fd,
            _MANIFEST,
            label="invalid official-query incident manifest",
        )
        _assert_manifest(existing, files=files, expected=manifest)


def _publish_manifest(source_fd: int, payload: bytes) -> None:
    temporary = f".invalid-complete-query-incident.{uuid4().hex}.tmp"
    try:
        write_bytes_exclusive_at(source_fd, temporary, payload)
        descriptor = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_fd)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        atomic_rename_no_replace_at(source_fd, temporary, source_fd, _MANIFEST)
        os.fsync(source_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=source_fd)
        except FileNotFoundError:
            pass


def _recover_orphan_manifest_temps(source_fd: int) -> None:
    removed = False
    for name in sorted(os.listdir(source_fd)):
        if _MANIFEST_TEMP.fullmatch(name) is None:
            continue
        metadata = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("invalid official-query incident manifest temp is unsafe")
        os.unlink(name, dir_fd=source_fd)
        removed = True
    if removed:
        os.fsync(source_fd)


def _assert_bound(
    binding: FinalRootBinding,
    *,
    archive_fd: int,
    archive_identity: tuple[int, int],
    evidence_parent_fd: int,
    evidence_parent_identity: tuple[int, int],
    source_fd: int,
    source_identity: tuple[int, int],
    attempt_id: str,
) -> None:
    binding.assert_bound()
    assert_directory_entry(
        binding.processed_fd,
        _ARCHIVE_PARENT,
        expected=archive_identity,
        label="invalid official-query incident archive",
    )
    assert_directory_entry(
        binding.processed_fd,
        _EVIDENCE_PARENT,
        expected=evidence_parent_identity,
        label="official-query evidence parent",
    )
    assert_directory_entry(
        evidence_parent_fd,
        attempt_id,
        expected=source_identity,
        label="invalid official-query incident source",
    )
    if directory_identity(archive_fd) != archive_identity or directory_identity(
        source_fd
    ) != source_identity:
        raise ValueError("invalid official-query incident descriptor identity differs")


def _verify_archive(
    destination: Path,
    *,
    attempt_id: str,
    processed_root: Path,
) -> None:
    _assert_safe_directory(destination, label="invalid official-query incident destination")
    parent_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        archive_fd = open_directory_at(
            parent_fd,
            destination.name,
            label="invalid official-query incident destination",
        )
        try:
            manifest = read_json_at(
                archive_fd,
                _MANIFEST,
                label="invalid official-query incident manifest",
            )
            files = _records(archive_fd, attempt_id=attempt_id)
            collection_bytes = read_bytes_at(
                archive_fd,
                COLLECTION_MANIFEST,
                label="invalid official-query collection manifest",
            )
            coverage_index = _complete_coverage_index(archive_fd)
            expected = _manifest_payload(
                attempt_id=attempt_id,
                reason=str(manifest.get("reason", "")),
                collection_bytes=collection_bytes,
                coverage_index=coverage_index,
                package_count=_query_package_count(files),
                evidence_parent_identity=_identity_from_manifest(
                    manifest.get("evidence_parent_identity")
                ),
                files=files,
            )
            _assert_manifest(manifest, files=files, expected=expected)
            _assert_evidence_parent_identity(
                processed_root,
                manifest["evidence_parent_identity"],
            )
        finally:
            os.close(archive_fd)
    finally:
        os.close(parent_fd)


def _assert_manifest(
    manifest: dict[str, object],
    *,
    files: list[dict[str, object]],
    expected: dict[str, object],
) -> None:
    required = set(expected)
    if (
        set(manifest) != required
        or not isinstance(manifest.get("reason"), str)
        or not manifest["reason"]
        or not isinstance(manifest.get("archived_at"), str)
        or not manifest["archived_at"]
        or manifest.get("files") != files
        or any(
            manifest.get(field) != expected[field]
            for field in expected
            if field not in {"archived_at", "reason"}
        )
    ):
        raise ValueError("invalid official-query incident manifest identity mismatch")
    for field in ("coverage_index_sha256", "collection_manifest_sha256"):
        if not isinstance(manifest.get(field), str) or _SHA256.fullmatch(manifest[field]) is None:
            raise ValueError("invalid official-query incident manifest identity mismatch")


def _identity_from_manifest(value: object) -> tuple[int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(not isinstance(item, int) or isinstance(item, bool) for item in value)
    ):
        raise ValueError("invalid official-query incident manifest identity mismatch")
    return value[0], value[1]


def _assert_evidence_parent_identity(processed_root: Path, value: object) -> None:
    expected = _identity_from_manifest(value)
    parent = processed_root / _EVIDENCE_PARENT
    _assert_safe_directory(parent, label="official-query evidence parent")
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if directory_identity(descriptor) != expected:
            raise ValueError("official-query evidence parent identity changed")
    finally:
        os.close(descriptor)


def _assert_safe_directory(path: Path, *, label: str) -> None:
    if path.is_symlink() or not path.is_dir() or path.resolve() != path.absolute():
        raise ValueError(f"{label} is not a safe canonical directory")
