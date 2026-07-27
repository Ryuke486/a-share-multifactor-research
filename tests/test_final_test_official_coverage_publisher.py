from __future__ import annotations

import json

import polars as pl
import pytest

from ashare_multifactor.final_test.execution_sources import (
    validate_security_event_coverage,
)
from ashare_multifactor.final_test.official_coverage_publisher import (
    publish_official_execution_coverages,
)
from ashare_multifactor.final_test.official_review_submission import (
    publish_review_submission,
)
from ashare_multifactor.final_test.corporate_action_coverage import (
    validate_corporate_action_coverage,
)
from test_final_test_official_review_submission import _review_inputs
from test_final_test_resume import PreparedAttempt


pytest_plugins = ("test_final_test_resume",)


def test_coverage_publisher_atomically_builds_both_ready_coverages_without_pdf_copies(
    prepared_attempt: PreparedAttempt,
) -> None:
    (
        workspace,
        candidates,
        announcement_decisions,
        dispositions,
        corporate_facts,
        security_events,
    ) = _review_inputs(prepared_attempt)
    submission = publish_review_submission(
        workspace=workspace,
        candidate_manifest_path=candidates.manifest_path,
        announcement_decisions=announcement_decisions,
        corporate_action_dispositions=dispositions,
        corporate_action_facts=corporate_facts,
        security_event_facts=security_events,
        reviewer_id="human-reviewer-1",
        reviewed_at="2026-07-27T12:00:00+08:00",
    )
    evidence_inputs = _publisher_bindings(prepared_attempt)

    publication = publish_official_execution_coverages(
        preparation=evidence_inputs["preparation"],
        authorization=evidence_inputs["authorization"],
        contract=evidence_inputs["contract"],
        workspace=workspace,
        candidate_manifest_path=candidates.manifest_path,
        submission_manifest_path=submission.manifest_path,
        official_query_index_path=(
            workspace.root.parent
            / "official_query_coverage/official_query_coverage.json"
        ),
    )

    corporate = validate_corporate_action_coverage(
        publication.corporate_root,
        symbols=["000001", "600000"],
    )
    security = validate_security_event_coverage(
        publication.security_manifest_path,
        symbols=["000001", "600000"],
    )
    assert corporate["actions"].height == 1
    assert security["event_rows"] == 0
    assert (
        corporate["coverage"]
        .filter(pl.col("symbol") == "600000")
        .item(0, "event_count")
        == 0
    )
    assert not list(publication.root.rglob("document.bin"))
    assert not list(publication.root.rglob("response.json"))
    corporate_manifest = json.loads(
        (publication.corporate_root / "coverage.json").read_text(encoding="utf-8")
    )
    assert corporate_manifest["schema_version"] == "3"
    assert corporate_manifest["official_query_coverage"]["root"] == "attempt_evidence"

    recovered = publish_official_execution_coverages(
        preparation=evidence_inputs["preparation"],
        authorization=evidence_inputs["authorization"],
        contract=evidence_inputs["contract"],
        workspace=workspace,
        candidate_manifest_path=candidates.manifest_path,
        submission_manifest_path=submission.manifest_path,
        official_query_index_path=(
            workspace.root.parent
            / "official_query_coverage/official_query_coverage.json"
        ),
    )
    assert recovered == publication


def test_ready_coverage_rechecks_shared_document_bytes(
    prepared_attempt: PreparedAttempt,
) -> None:
    (
        workspace,
        candidates,
        announcement_decisions,
        dispositions,
        corporate_facts,
        security_events,
    ) = _review_inputs(prepared_attempt)
    submission = publish_review_submission(
        workspace=workspace,
        candidate_manifest_path=candidates.manifest_path,
        announcement_decisions=announcement_decisions,
        corporate_action_dispositions=dispositions,
        corporate_action_facts=corporate_facts,
        security_event_facts=security_events,
        reviewer_id="human-reviewer-1",
        reviewed_at="2026-07-27T12:00:00+08:00",
    )
    bindings = _publisher_bindings(prepared_attempt)
    publication = publish_official_execution_coverages(
        **bindings,
        workspace=workspace,
        candidate_manifest_path=candidates.manifest_path,
        submission_manifest_path=submission.manifest_path,
        official_query_index_path=(
            workspace.root.parent
            / "official_query_coverage/official_query_coverage.json"
        ),
    )
    document = next(workspace.root.rglob("document.bin"))
    document.write_bytes(document.read_bytes() + b"changed")

    with pytest.raises(ValueError, match="evidence.*changed|hash"):
        validate_corporate_action_coverage(publication.corporate_root)


def _publisher_bindings(attempt: PreparedAttempt) -> dict[str, object]:
    from ashare_multifactor.final_test.action_source_contract import (
        load_action_source_contract,
    )
    from ashare_multifactor.final_test.preparation import verify_preparation
    from ashare_multifactor.final_test.resume import load_registered_authorization

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
    return {
        "preparation": preparation,
        "authorization": authorization,
        "contract": load_action_source_contract(
            attempt.code_root / "configs/final_execution_sources.yaml"
        ),
    }
