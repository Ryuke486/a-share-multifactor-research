from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from ashare_multifactor.final_test.corporate_action_candidates import (
    collect_corporate_action_candidates,
)
from ashare_multifactor.final_test.official_announcement_catalog import (
    build_announcement_catalog,
)
from ashare_multifactor.final_test.official_document_fetcher import (
    fetch_official_documents,
)
from ashare_multifactor.final_test.official_review_submission import (
    publish_review_submission,
)
from test_final_test_official_candidate_review_admission import (
    _AdmissionAnnouncementTransport,
    _AdmissionDocumentTransport,
    _pdf_bytes,
)
from test_final_test_official_evidence_workspace import (
    _complete_query_coverage,
)
from test_final_test_resume import PreparedAttempt


pytest_plugins = ("test_final_test_resume",)

_FIELDS = [
    "code",
    "dividOperateDate",
    "dividPayDate",
    "dividStockMarketDate",
    "dividCashPsBeforeTax",
    "dividStocksPs",
    "dividReserveToStockPs",
]


def _review_inputs(
    attempt: PreparedAttempt,
    *,
    missing_payment_date: bool = False,
):
    inputs = _complete_query_coverage(
        attempt,
        transport=_AdmissionAnnouncementTransport(
            titles={
                "000001": "2024年度权益分派实施公告",
                "600000": "2024年第三季度报告",
            },
            announcement_dates={
                "000001": date(2024, 5, 28),
                "600000": date(2024, 6, 18),
            },
        ),
    )
    catalog = build_announcement_catalog(
        inputs.index_path,
        preparation=inputs.preparation,
        authorization=inputs.authorization,
        contract=inputs.contract,
        destination=inputs.output_root,
    )
    workspace = fetch_official_documents(
        catalog,
        destination=inputs.output_root,
        transport=_AdmissionDocumentTransport(
            {
                "000001": _pdf_bytes("2024-06-01"),
                "600000": _pdf_bytes("2024-07-01"),
            }
        ),
    )

    def query(
        code: str,
        year: int,
        year_type: str,
    ) -> tuple[list[str], list[list[str]]]:
        if code == "sz.000001" and year == 2024:
            return _FIELDS, [
                [
                    code,
                    "2024-06-01",
                    "" if missing_payment_date else "2024-06-08",
                    "",
                    "0.10",
                    "0",
                    "0",
                ]
            ]
        return _FIELDS, []

    candidates = collect_corporate_action_candidates(
        preparation=inputs.preparation,
        authorization=inputs.authorization,
        contract=inputs.contract,
        output_root=inputs.output_root,
        query=query,
    )
    queue = pl.read_parquet(workspace.review_queue_path)
    candidate_frame = pl.read_parquet(candidates.candidates_path)
    catalog_id = queue.item(0, "catalog_id")
    candidate_id = candidate_frame.item(0, "candidate_id")
    announcement_decisions = pl.DataFrame(
        {
            "catalog_id": [catalog_id],
            "status": ["relevant"],
            "explanation": ["official implementation announcement"],
        }
    )
    dispositions = pl.DataFrame(
        {
            "candidate_id": [candidate_id],
            "status": ["accepted"],
            "catalog_id": [catalog_id],
            "explanation": ["official fields match the provider candidate"],
            "original_symbol": ["000001"],
            "corrected_symbol": [None],
            "original_ex_date": [date(2024, 6, 1)],
            "corrected_ex_date": [None],
            "original_effective_date": [
                None if missing_payment_date else date(2024, 6, 8)
            ],
            "corrected_effective_date": [None],
            "original_cash_per_share": [0.1],
            "corrected_cash_per_share": [None],
            "original_share_ratio": [0.0],
            "corrected_share_ratio": [None],
            "correction_reason": [None],
        }
    )
    corporate_facts = pl.DataFrame(
        {
            "catalog_id": [catalog_id],
            "candidate_id": [candidate_id],
            "announcement_date": [date(2022, 8, 1)],
            "symbol": ["000001"],
            "ex_date": [date(2024, 6, 1)],
            "effective_date": [date(2024, 6, 8)],
            "cash_per_share": [0.1],
            "share_ratio": [0.0],
        }
    )
    security_events = pl.DataFrame(
        schema={
            "catalog_id": pl.String,
            "effective_date": pl.Date,
            "source_symbol": pl.String,
            "event_type": pl.String,
            "target_symbol": pl.String,
            "ratio": pl.Float64,
            "cash_per_share": pl.Float64,
            "explanation": pl.String,
        }
    )
    return (
        workspace,
        candidates,
        announcement_decisions,
        dispositions,
        corporate_facts,
        security_events,
    )


def test_review_submission_requires_missing_payment_date_to_be_corrected(
    prepared_attempt: PreparedAttempt,
) -> None:
    (
        workspace,
        candidates,
        announcement_decisions,
        dispositions,
        corporate_facts,
        security_events,
    ) = _review_inputs(prepared_attempt, missing_payment_date=True)

    with pytest.raises(
        ValueError,
        match="missing effective date requires correction or rejection",
    ):
        publish_review_submission(
            workspace=workspace,
            candidate_manifest_path=candidates.manifest_path,
            announcement_decisions=announcement_decisions,
            corporate_action_dispositions=dispositions,
            corporate_action_facts=corporate_facts,
            security_event_facts=security_events,
            reviewer_id="human-reviewer-1",
            reviewed_at="2026-07-27T12:00:00+08:00",
        )


def test_review_submission_accepts_official_correction_for_missing_payment_date(
    prepared_attempt: PreparedAttempt,
) -> None:
    (
        workspace,
        candidates,
        announcement_decisions,
        dispositions,
        corporate_facts,
        security_events,
    ) = _review_inputs(prepared_attempt, missing_payment_date=True)
    corrected = dispositions.with_columns(
        pl.lit("corrected").alias("status"),
        pl.lit(date(2024, 6, 8)).alias("corrected_effective_date"),
        pl.lit("payment date supplied by the official announcement").alias(
            "correction_reason"
        ),
    )

    submission = publish_review_submission(
        workspace=workspace,
        candidate_manifest_path=candidates.manifest_path,
        announcement_decisions=announcement_decisions,
        corporate_action_dispositions=corrected,
        corporate_action_facts=corporate_facts,
        security_event_facts=security_events,
        reviewer_id="human-reviewer-1",
        reviewed_at="2026-07-27T12:00:00+08:00",
    )

    assert submission.ready is True
    assert submission.corporate_action_dispositions.item(
        0, "corrected_effective_date"
    ) == date(2024, 6, 8)


def test_review_submission_accepts_rejection_for_missing_payment_date(
    prepared_attempt: PreparedAttempt,
) -> None:
    (
        workspace,
        candidates,
        announcement_decisions,
        dispositions,
        corporate_facts,
        security_events,
    ) = _review_inputs(prepared_attempt, missing_payment_date=True)

    submission = publish_review_submission(
        workspace=workspace,
        candidate_manifest_path=candidates.manifest_path,
        announcement_decisions=announcement_decisions,
        corporate_action_dispositions=dispositions.with_columns(
            pl.lit("rejected").alias("status"),
            pl.lit("official announcement does not support an execution fact").alias(
                "explanation"
            ),
        ),
        corporate_action_facts=corporate_facts.clear(),
        security_event_facts=security_events,
        reviewer_id="human-reviewer-1",
        reviewed_at="2026-07-27T12:00:00+08:00",
    )

    assert submission.ready is True
    assert submission.corporate_action_facts.height == 0


def test_review_submission_detects_false_null_original_payment_date(
    prepared_attempt: PreparedAttempt,
) -> None:
    (
        workspace,
        candidates,
        announcement_decisions,
        dispositions,
        corporate_facts,
        security_events,
    ) = _review_inputs(prepared_attempt, missing_payment_date=True)
    false_original = dispositions.with_columns(
        pl.lit("corrected").alias("status"),
        pl.lit(date(2024, 6, 8)).alias("original_effective_date"),
        pl.lit(date(2024, 6, 8)).alias("corrected_effective_date"),
        pl.lit("payment date supplied by the official announcement").alias(
            "correction_reason"
        ),
    )

    with pytest.raises(ValueError, match="original fields are false"):
        publish_review_submission(
            workspace=workspace,
            candidate_manifest_path=candidates.manifest_path,
            announcement_decisions=announcement_decisions,
            corporate_action_dispositions=false_original,
            corporate_action_facts=corporate_facts,
            security_event_facts=security_events,
            reviewer_id="human-reviewer-1",
            reviewed_at="2026-07-27T12:00:00+08:00",
        )


def test_review_submission_exactly_covers_queue_and_provider_candidates(
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

    assert submission.ready is True
    assert submission.announcement_decisions.height == 1
    assert submission.corporate_action_facts.height == 1
    assert submission.security_event_facts.height == 0
    recovered = publish_review_submission(
        workspace=workspace,
        candidate_manifest_path=candidates.manifest_path,
        announcement_decisions=announcement_decisions,
        corporate_action_dispositions=dispositions,
        corporate_action_facts=corporate_facts,
        security_event_facts=security_events,
        reviewer_id="human-reviewer-1",
        reviewed_at="2026-07-27T12:00:00+08:00",
    )
    assert recovered == submission


def test_review_submission_rejects_incomplete_queue_decisions(
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

    with pytest.raises(ValueError, match="review queue coverage is not exact"):
        publish_review_submission(
            workspace=workspace,
            candidate_manifest_path=candidates.manifest_path,
            announcement_decisions=announcement_decisions.clear(),
            corporate_action_dispositions=dispositions,
            corporate_action_facts=corporate_facts,
            security_event_facts=security_events,
            reviewer_id="human-reviewer-1",
            reviewed_at="2026-07-27T12:00:00+08:00",
        )


def test_review_submission_rejects_fact_without_relevant_cached_document(
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

    with pytest.raises(ValueError, match="relevant cached document"):
        publish_review_submission(
            workspace=workspace,
            candidate_manifest_path=candidates.manifest_path,
            announcement_decisions=announcement_decisions.with_columns(
                pl.lit("irrelevant").alias("status")
            ),
            corporate_action_dispositions=dispositions,
            corporate_action_facts=corporate_facts,
            security_event_facts=security_events,
            reviewer_id="human-reviewer-1",
            reviewed_at="2026-07-27T12:00:00+08:00",
        )
