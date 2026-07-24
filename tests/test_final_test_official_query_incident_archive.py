from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ashare_multifactor.final_test.action_source_contract import (
    load_action_source_contract,
)
from ashare_multifactor.final_test.official_query_client import RetryPolicy
from ashare_multifactor.final_test.preparation import verify_preparation
from ashare_multifactor.final_test.registry import append_attempt_outcome
from ashare_multifactor.final_test.resume import load_registered_authorization
from test_final_test_official_query_collector import ZeroResultTransport
from test_final_test_resume import PreparedAttempt


pytest_plugins = ("test_final_test_resume",)


def test_partial_archive_counts_shared_adaptive_query_package_paths() -> None:
    from ashare_multifactor.final_test.official_query_incident_archive import (
        _is_query_package_manifest,
    )

    assert _is_query_package_manifest(
        "final_test_evidence/attempt-001/official_query_coverage/packages/"
        "announcements/sh/600000/2022-01-01_2023-12-31/"
        "query-package/query_manifest.json"
    )


_REASON = "official query collection paused after a rate-interval defect before coverage index publication"


def test_partial_query_archive_api_is_public() -> None:
    import ashare_multifactor.final_test as final_test

    assert callable(getattr(final_test, "archive_incomplete_query_collection", None))


def _partial_collection(attempt: PreparedAttempt) -> Path:
    from ashare_multifactor.final_test.official_query_collector import (
        collect_official_query_coverage,
    )

    authorization = load_registered_authorization(
        code_root=attempt.code_root,
        data_root=attempt.data_root,
        attempt_id=attempt.attempt_id,
        approval_key=attempt.approval_key,
    )
    preparation = verify_preparation(
        attempt.data_root / "processed/final_test",
        attempt_id=attempt.attempt_id,
        authorization=authorization,
    )
    result = collect_official_query_coverage(
        preparation=preparation,
        authorization=authorization,
        contract=load_action_source_contract(
            attempt.code_root / "configs/final_execution_sources.yaml"
        ),
        output_root=(
            attempt.data_root
            / "processed/final_test_evidence"
            / attempt.attempt_id
        ),
        transport=ZeroResultTransport(),
        policy=RetryPolicy(attempts=1, timeout_seconds=0.1, minimum_interval_seconds=0),
        max_scopes=1,
    )
    assert result.index_path is None
    return result.root.parent


def _file_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_partial_query_collection_archives_immutably_after_attempt_failure(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_query_incident_archive import (
        archive_incomplete_query_collection,
    )

    source = _partial_collection(prepared_attempt)
    original = _file_hashes(source)
    append_attempt_outcome(
        prepared_attempt.data_root / "processed/final_test/attempts",
        attempt_id=prepared_attempt.attempt_id,
        status="failed",
        authoritative=False,
        reason=_REASON,
    )

    archived = archive_incomplete_query_collection(
        prepared_attempt.data_root / "processed",
        attempt_id=prepared_attempt.attempt_id,
    )

    assert archived == (
        prepared_attempt.data_root
        / "processed/final_test_evidence_incidents"
        / prepared_attempt.attempt_id
    )
    assert not source.exists()
    manifest_path = archived / "incomplete_query_incident_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["attempt_id"] == prepared_attempt.attempt_id
    assert manifest["status"] == "archived_incomplete_official_query_collection"
    assert manifest["reason"] == _REASON
    assert manifest["coverage_index_present"] is False
    assert manifest["query_package_count"] == 1
    assert {
        record["path"].removeprefix(
            f"final_test_evidence/{prepared_attempt.attempt_id}/"
        ): record["sha256"]
        for record in manifest["files"]
    } == original
    assert archive_incomplete_query_collection(
        prepared_attempt.data_root / "processed",
        attempt_id=prepared_attempt.attempt_id,
    ) == archived


def test_partial_query_archive_rejects_noncanonical_collection_manifest(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_query_incident_archive import (
        archive_incomplete_query_collection,
    )

    source = _partial_collection(prepared_attempt)
    collection_manifest = source / "official_query_collection.json"
    collection_manifest.write_text(
        json.dumps(json.loads(collection_manifest.read_text(encoding="utf-8")), indent=2)
        + "\n",
        encoding="utf-8",
    )
    append_attempt_outcome(
        prepared_attempt.data_root / "processed/final_test/attempts",
        attempt_id=prepared_attempt.attempt_id,
        status="failed",
        authoritative=False,
        reason=_REASON,
    )

    with pytest.raises(ValueError, match="collection manifest"):
        archive_incomplete_query_collection(
            prepared_attempt.data_root / "processed",
            attempt_id=prepared_attempt.attempt_id,
        )

    assert source.is_dir()


def test_partial_query_archive_refuses_a_completed_coverage_index(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_query_collector import (
        collect_official_query_coverage,
    )
    from ashare_multifactor.final_test.official_query_incident_archive import (
        archive_incomplete_query_collection,
    )

    authorization = load_registered_authorization(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        attempt_id=prepared_attempt.attempt_id,
        approval_key=prepared_attempt.approval_key,
    )
    preparation = verify_preparation(
        prepared_attempt.data_root / "processed/final_test",
        attempt_id=prepared_attempt.attempt_id,
        authorization=authorization,
    )
    source = (
        prepared_attempt.data_root
        / "processed/final_test_evidence"
        / prepared_attempt.attempt_id
    )
    complete = collect_official_query_coverage(
        preparation=preparation,
        authorization=authorization,
        contract=load_action_source_contract(
            prepared_attempt.code_root / "configs/final_execution_sources.yaml"
        ),
        output_root=source,
        transport=ZeroResultTransport(),
        policy=RetryPolicy(attempts=1, timeout_seconds=0.1, minimum_interval_seconds=0),
    )
    assert complete.index_path is not None
    append_attempt_outcome(
        prepared_attempt.data_root / "processed/final_test/attempts",
        attempt_id=prepared_attempt.attempt_id,
        status="failed",
        authoritative=False,
        reason=_REASON,
    )

    with pytest.raises(ValueError, match="complete official-query coverage"):
        archive_incomplete_query_collection(
            prepared_attempt.data_root / "processed",
            attempt_id=prepared_attempt.attempt_id,
        )

    assert source.is_dir()


def test_partial_query_archive_rechecks_package_count_on_recovery(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_query_incident_archive import (
        archive_incomplete_query_collection,
    )

    _partial_collection(prepared_attempt)
    append_attempt_outcome(
        prepared_attempt.data_root / "processed/final_test/attempts",
        attempt_id=prepared_attempt.attempt_id,
        status="failed",
        authoritative=False,
        reason=_REASON,
    )
    archived = archive_incomplete_query_collection(
        prepared_attempt.data_root / "processed",
        attempt_id=prepared_attempt.attempt_id,
    )
    manifest_path = archived / "incomplete_query_incident_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["query_package_count"] = 99
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="manifest identity"):
        archive_incomplete_query_collection(
            prepared_attempt.data_root / "processed",
            attempt_id=prepared_attempt.attempt_id,
        )


def test_partial_query_archive_refuses_tampered_interrupted_manifest_before_move(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ashare_multifactor.final_test import official_query_incident_archive as archive

    source = _partial_collection(prepared_attempt)
    append_attempt_outcome(
        prepared_attempt.data_root / "processed/final_test/attempts",
        attempt_id=prepared_attempt.attempt_id,
        status="failed",
        authoritative=False,
        reason=_REASON,
    )
    original_rename = archive.atomic_rename_no_replace_at

    def interrupt_source_move(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
    ) -> None:
        if source_name == destination_name == prepared_attempt.attempt_id:
            raise KeyboardInterrupt("injected before evidence archive move")
        original_rename(source_fd, source_name, destination_fd, destination_name)

    monkeypatch.setattr(archive, "atomic_rename_no_replace_at", interrupt_source_move)
    with pytest.raises(KeyboardInterrupt, match="before evidence archive move"):
        archive.archive_incomplete_query_collection(
            prepared_attempt.data_root / "processed",
            attempt_id=prepared_attempt.attempt_id,
        )
    monkeypatch.setattr(archive, "atomic_rename_no_replace_at", original_rename)
    manifest_path = source / "incomplete_query_incident_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["query_package_count"] = 99
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="manifest identity"):
        archive.archive_incomplete_query_collection(
            prepared_attempt.data_root / "processed",
            attempt_id=prepared_attempt.attempt_id,
        )

    assert source.is_dir()
    assert not (
        prepared_attempt.data_root
        / "processed/final_test_evidence_incidents"
        / prepared_attempt.attempt_id
    ).exists()


def test_partial_query_archive_rejects_evidence_parent_swap_after_move(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ashare_multifactor.final_test import official_query_incident_archive as archive

    _partial_collection(prepared_attempt)
    append_attempt_outcome(
        prepared_attempt.data_root / "processed/final_test/attempts",
        attempt_id=prepared_attempt.attempt_id,
        status="failed",
        authoritative=False,
        reason=_REASON,
    )
    original_rename = archive.atomic_rename_no_replace_at
    processed = prepared_attempt.data_root / "processed"
    evidence_parent = processed / "final_test_evidence"
    displaced = processed / "displaced-final-test-evidence"

    def replace_parent_after_move(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
    ) -> None:
        original_rename(source_fd, source_name, destination_fd, destination_name)
        if source_name == destination_name == prepared_attempt.attempt_id:
            evidence_parent.rename(displaced)
            evidence_parent.mkdir()

    monkeypatch.setattr(archive, "atomic_rename_no_replace_at", replace_parent_after_move)

    with pytest.raises(ValueError, match="evidence parent.*identity"):
        archive.archive_incomplete_query_collection(
            processed,
            attempt_id=prepared_attempt.attempt_id,
        )

    assert displaced.is_dir()
    assert (
        processed
        / "final_test_evidence_incidents"
        / prepared_attempt.attempt_id
    ).is_dir()
    with pytest.raises(ValueError, match="evidence parent.*identity"):
        archive.archive_incomplete_query_collection(
            processed,
            attempt_id=prepared_attempt.attempt_id,
        )
