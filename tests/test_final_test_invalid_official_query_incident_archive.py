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


_REASON = "CNInfo code-only announcement queries produced invalid complete coverage"


def test_invalid_complete_query_archive_api_is_public() -> None:
    import ashare_multifactor.final_test as final_test

    assert callable(
        getattr(final_test, "archive_invalid_complete_query_collection", None)
    )


def _complete_collection(attempt: PreparedAttempt) -> Path:
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
    )
    assert result.index_path is not None
    return result.root.parent


def _file_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _mark_failed(attempt: PreparedAttempt) -> None:
    append_attempt_outcome(
        attempt.data_root / "processed/final_test/attempts",
        attempt_id=attempt.attempt_id,
        status="failed",
        authoritative=False,
        reason=_REASON,
    )


def test_invalid_complete_query_collection_archives_immutably(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.invalid_official_query_incident_archive import (
        archive_invalid_complete_query_collection,
    )

    source = _complete_collection(prepared_attempt)
    original = _file_hashes(source)
    _mark_failed(prepared_attempt)

    archived = archive_invalid_complete_query_collection(
        prepared_attempt.data_root / "processed",
        attempt_id=prepared_attempt.attempt_id,
    )

    assert archived == (
        prepared_attempt.data_root
        / "processed/final_test_evidence_incidents"
        / prepared_attempt.attempt_id
    )
    assert not source.exists()
    manifest = json.loads(
        (archived / "invalid_complete_query_incident_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["attempt_id"] == prepared_attempt.attempt_id
    assert manifest["status"] == "archived_invalid_complete_official_query_collection"
    assert manifest["reason"] == _REASON
    assert manifest["coverage_index_present"] is True
    assert manifest["reuse_permitted"] is False
    assert manifest["query_package_count"] == 4
    assert {
        record["path"].removeprefix(
            f"final_test_evidence/{prepared_attempt.attempt_id}/"
        ): record["sha256"]
        for record in manifest["files"]
    } == original
    assert archive_invalid_complete_query_collection(
        prepared_attempt.data_root / "processed",
        attempt_id=prepared_attempt.attempt_id,
    ) == archived


def test_invalid_complete_query_archive_refuses_incomplete_coverage(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.invalid_official_query_incident_archive import (
        archive_invalid_complete_query_collection,
    )
    from ashare_multifactor.final_test.official_query_collector import (
        collect_official_query_coverage,
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
    result = collect_official_query_coverage(
        preparation=preparation,
        authorization=authorization,
        contract=load_action_source_contract(
            prepared_attempt.code_root / "configs/final_execution_sources.yaml"
        ),
        output_root=(
            prepared_attempt.data_root
            / "processed/final_test_evidence"
            / prepared_attempt.attempt_id
        ),
        transport=ZeroResultTransport(),
        policy=RetryPolicy(attempts=1, timeout_seconds=0.1, minimum_interval_seconds=0),
        max_scopes=1,
    )
    assert result.index_path is None
    _mark_failed(prepared_attempt)

    with pytest.raises(ValueError, match="complete official-query coverage"):
        archive_invalid_complete_query_collection(
            prepared_attempt.data_root / "processed",
            attempt_id=prepared_attempt.attempt_id,
        )


def test_invalid_complete_query_archive_rechecks_manifest_on_recovery(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.invalid_official_query_incident_archive import (
        archive_invalid_complete_query_collection,
    )

    _complete_collection(prepared_attempt)
    _mark_failed(prepared_attempt)
    archived = archive_invalid_complete_query_collection(
        prepared_attempt.data_root / "processed",
        attempt_id=prepared_attempt.attempt_id,
    )
    manifest_path = archived / "invalid_complete_query_incident_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["coverage_index_sha256"] = "0" * 64
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="manifest identity"):
        archive_invalid_complete_query_collection(
            prepared_attempt.data_root / "processed",
            attempt_id=prepared_attempt.attempt_id,
        )
