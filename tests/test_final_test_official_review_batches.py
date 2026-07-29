from __future__ import annotations

from datetime import date, datetime, time
import json
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

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
from ashare_multifactor.final_test.official_review_batch_workspace import (
    load_verified_review_batch_workspace,
    prepare_review_batch_workspace,
)
from ashare_multifactor.final_test.official_review_batches import (
    finalize_review_batches,
    publish_review_batch,
)
from test_final_test_official_evidence_workspace import (
    AnnouncementTransport,
    DocumentTransport,
    _complete_query_coverage,
)
from test_final_test_official_candidate_review_admission import _pdf_bytes
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


class _TwoCorporateActionTransport(AnnouncementTransport):
    def fetch(
        self,
        endpoint: str,
        form: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> bytes:
        if endpoint.endswith("/information/topSearch/query"):
            return super().fetch(
                endpoint,
                form,
                timeout_seconds=timeout_seconds,
            )
        del timeout_seconds
        symbol = form["stock"].split(",", maxsplit=1)[0]
        announcement_date = (
            date(2024, 5, 28)
            if symbol == "000001"
            else date(2024, 6, 18)
        )
        timestamp = int(
            datetime.combine(
                announcement_date,
                time.min,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ).timestamp()
            * 1000
        )
        return json.dumps(
            {
                "totalpages": 1,
                "totalAnnouncement": 1,
                "announcements": [
                    {
                        "announcementId": f"{symbol}-announcement",
                        "announcementTitle": "2024年度权益分派实施公告",
                        "announcementTime": timestamp,
                        "adjunctUrl": (
                            f"finalpage/{announcement_date.isoformat()}/{symbol}.PDF"
                        ),
                    }
                ],
            },
            separators=(",", ":"),
        ).encode()


class _ReviewDocumentTransport:
    def fetch(self, url: str, *, timeout_seconds: float) -> bytes:
        del timeout_seconds
        symbol = Path(urlparse(url).path).stem
        ex_date = "2024-06-01" if symbol == "000001" else "2024-07-01"
        return _pdf_bytes(ex_date)


def _batch_inputs(
    attempt: PreparedAttempt,
    *,
    admission_ready: bool = True,
):
    inputs = _complete_query_coverage(
        attempt,
        transport=_TwoCorporateActionTransport(),
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
        transport=(
            _ReviewDocumentTransport()
            if admission_ready
            else DocumentTransport()
        ),
    )

    def query(
        code: str,
        year: int,
        year_type: str,
    ) -> tuple[list[str], list[list[str]]]:
        del year_type
        if year != 2024:
            return _FIELDS, []
        symbol = code.split(".", maxsplit=1)[1]
        operate_date = "2024-06-01" if symbol == "000001" else "2024-07-01"
        pay_date = "2024-06-08" if symbol == "000001" else "2024-07-08"
        return _FIELDS, [
            [code, operate_date, pay_date, "", "0.10", "0", "0"]
        ]

    candidates = collect_corporate_action_candidates(
        preparation=inputs.preparation,
        authorization=inputs.authorization,
        contract=inputs.contract,
        output_root=inputs.output_root,
        query=query,
    )
    return workspace, candidates


def test_review_batch_workspace_blocks_before_writing_plan_when_admission_is_not_ready(
    prepared_attempt: PreparedAttempt,
) -> None:
    workspace, candidates = _batch_inputs(
        prepared_attempt,
        admission_ready=False,
    )
    plans_root = workspace.root.parent / "official_review_batch_plans"
    plans_before = set(plans_root.iterdir()) if plans_root.exists() else set()
    admission_error: ValueError | None = None

    try:
        prepare_review_batch_workspace(
            workspace=workspace,
            candidate_manifest_path=candidates.manifest_path,
            symbols_per_batch=1,
        )
    except ValueError as error:
        admission_error = error

    plans_after = set(plans_root.iterdir()) if plans_root.exists() else set()
    assert plans_after == plans_before
    assert admission_error is not None
    assert "candidate review admission" in str(admission_error).lower()


def _batch_frames(batch):
    queue = pl.read_parquet(batch.queue_path)
    candidates = pl.read_parquet(batch.candidates_path)
    catalog_id = queue.item(0, "catalog_id")
    candidate = candidates.row(0, named=True)
    decisions = pl.DataFrame(
        {
            "catalog_id": [catalog_id],
            "status": ["relevant"],
            "explanation": ["official implementation announcement"],
        }
    )
    dispositions = pl.DataFrame(
        {
            "candidate_id": [candidate["candidate_id"]],
            "status": ["accepted"],
            "catalog_id": [catalog_id],
            "explanation": ["official fields match the provider candidate"],
            "original_symbol": [candidate["symbol"]],
            "corrected_symbol": [None],
            "original_ex_date": [candidate["ex_date"]],
            "corrected_ex_date": [None],
            "original_effective_date": [candidate["effective_date"]],
            "corrected_effective_date": [None],
            "original_cash_per_share": [candidate["cash_per_share"]],
            "corrected_cash_per_share": [None],
            "original_share_ratio": [candidate["share_ratio"]],
            "corrected_share_ratio": [None],
            "correction_reason": [None],
        }
    )
    corporate_facts = pl.DataFrame(
        {
            "catalog_id": [catalog_id],
            "candidate_id": [candidate["candidate_id"]],
            "announcement_date": [date(2022, 8, 1)],
            "symbol": [candidate["symbol"]],
            "ex_date": [candidate["ex_date"]],
            "effective_date": [candidate["effective_date"]],
            "cash_per_share": [candidate["cash_per_share"]],
            "share_ratio": [candidate["share_ratio"]],
        }
    )
    security_facts = pl.DataFrame(
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
    return decisions, dispositions, corporate_facts, security_facts


def _publish_batch(workspace, candidates, batch):
    frames = _batch_frames(batch)
    return publish_review_batch(
        workspace=workspace,
        candidate_manifest_path=candidates.manifest_path,
        batch_manifest_path=batch.manifest_path,
        announcement_decisions=frames[0],
        corporate_action_dispositions=frames[1],
        corporate_action_facts=frames[2],
        security_event_facts=frames[3],
        reviewer_id="human-reviewer-1",
        reviewed_at="2026-07-28T12:00:00+08:00",
    )


def _cross_symbol_frames(batch):
    queue = pl.read_parquet(batch.queue_path).sort("symbol")
    candidates = pl.read_parquet(batch.candidates_path).sort("symbol")
    catalog_ids = queue.get_column("catalog_id").to_list()
    candidate_rows = candidates.rows(named=True)
    decisions = pl.DataFrame(
        {
            "catalog_id": catalog_ids,
            "status": ["relevant"] * len(catalog_ids),
            "explanation": ["official implementation announcement"] * len(catalog_ids),
        }
    )
    reversed_catalog_ids = list(reversed(catalog_ids))
    dispositions = pl.DataFrame(
        {
            "candidate_id": [row["candidate_id"] for row in candidate_rows],
            "status": ["accepted"] * len(candidate_rows),
            "catalog_id": reversed_catalog_ids,
            "explanation": ["cross-symbol evidence"] * len(candidate_rows),
            "original_symbol": [row["symbol"] for row in candidate_rows],
            "corrected_symbol": [None] * len(candidate_rows),
            "original_ex_date": [row["ex_date"] for row in candidate_rows],
            "corrected_ex_date": [None] * len(candidate_rows),
            "original_effective_date": [
                row["effective_date"] for row in candidate_rows
            ],
            "corrected_effective_date": [None] * len(candidate_rows),
            "original_cash_per_share": [
                row["cash_per_share"] for row in candidate_rows
            ],
            "corrected_cash_per_share": [None] * len(candidate_rows),
            "original_share_ratio": [
                row["share_ratio"] for row in candidate_rows
            ],
            "corrected_share_ratio": [None] * len(candidate_rows),
            "correction_reason": [None] * len(candidate_rows),
        }
    )
    corporate_facts = pl.DataFrame(
        {
            "catalog_id": reversed_catalog_ids,
            "candidate_id": [row["candidate_id"] for row in candidate_rows],
            "announcement_date": [date(2022, 8, 1)] * len(candidate_rows),
            "symbol": [row["symbol"] for row in candidate_rows],
            "ex_date": [row["ex_date"] for row in candidate_rows],
            "effective_date": [row["effective_date"] for row in candidate_rows],
            "cash_per_share": [
                row["cash_per_share"] for row in candidate_rows
            ],
            "share_ratio": [row["share_ratio"] for row in candidate_rows],
        }
    )
    security_facts = pl.DataFrame(
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
    return decisions, dispositions, corporate_facts, security_facts


def test_review_batches_resume_then_finalize_exact_submission(
    prepared_attempt: PreparedAttempt,
) -> None:
    workspace, candidates = _batch_inputs(prepared_attempt)
    plan = prepare_review_batch_workspace(
        workspace=workspace,
        candidate_manifest_path=candidates.manifest_path,
        symbols_per_batch=1,
    )
    assert len(plan.batches) == 2
    plan_manifest = json.loads(plan.manifest_path.read_bytes())
    assert plan_manifest["candidate_review_admission"] == {
        "relative_path": plan.admission_manifest_path.relative_to(
            workspace.root.parent
        ).as_posix(),
        "sha256": plan.admission_manifest_sha256,
        "status": "ready",
    }
    assert plan.admission_manifest_path.parent.parent.name == (
        "candidate_review_admissions"
    )

    first = _publish_batch(workspace, candidates, plan.batches[0])
    recovered = load_verified_review_batch_workspace(
        plan.manifest_path,
        workspace=workspace,
        candidate_manifest_path=candidates.manifest_path,
    )
    assert recovered == plan
    with pytest.raises(ValueError, match="review batches are incomplete"):
        finalize_review_batches(
            workspace=workspace,
            candidate_manifest_path=candidates.manifest_path,
            batch_workspace_manifest_path=plan.manifest_path,
            reviewer_id="human-reviewer-1",
            reviewed_at="2026-07-28T18:00:00+08:00",
        )

    assert _publish_batch(workspace, candidates, plan.batches[0]) == first
    _publish_batch(workspace, candidates, plan.batches[1])
    submission = finalize_review_batches(
        workspace=workspace,
        candidate_manifest_path=candidates.manifest_path,
        batch_workspace_manifest_path=plan.manifest_path,
        reviewer_id="human-reviewer-1",
        reviewed_at="2026-07-28T18:00:00+08:00",
    )

    assert submission.ready is True
    assert submission.announcement_decisions.height == 2
    assert submission.corporate_action_dispositions.height == 2
    assert submission.corporate_action_facts.height == 2


def test_review_batch_rejects_conflicting_resubmission(
    prepared_attempt: PreparedAttempt,
) -> None:
    workspace, candidates = _batch_inputs(prepared_attempt)
    plan = prepare_review_batch_workspace(
        workspace=workspace,
        candidate_manifest_path=candidates.manifest_path,
        symbols_per_batch=1,
    )
    batch = plan.batches[0]
    _publish_batch(workspace, candidates, batch)
    frames = _batch_frames(batch)

    with pytest.raises(ValueError, match="review batch decisions differ"):
        publish_review_batch(
            workspace=workspace,
            candidate_manifest_path=candidates.manifest_path,
            batch_manifest_path=batch.manifest_path,
            announcement_decisions=frames[0].with_columns(
                pl.lit("conflicting decision").alias("explanation")
            ),
            corporate_action_dispositions=frames[1],
            corporate_action_facts=frames[2],
            security_event_facts=frames[3],
            reviewer_id="human-reviewer-1",
            reviewed_at="2026-07-28T12:00:00+08:00",
        )


def test_review_batch_workspace_rejects_frozen_input_drift(
    prepared_attempt: PreparedAttempt,
) -> None:
    workspace, candidates = _batch_inputs(prepared_attempt)
    plan = prepare_review_batch_workspace(
        workspace=workspace,
        candidate_manifest_path=candidates.manifest_path,
        symbols_per_batch=1,
    )
    plan.batches[0].queue_path.write_bytes(b"changed")

    with pytest.raises(ValueError, match="review batch workspace"):
        load_verified_review_batch_workspace(
            plan.manifest_path,
            workspace=workspace,
            candidate_manifest_path=candidates.manifest_path,
        )


@pytest.mark.parametrize(
    "drift_target",
    ["admission", "queue", "candidate", "date_rule", "routing"],
)
def test_review_batch_workspace_rejects_bound_admission_identity_drift(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    drift_target: str,
) -> None:
    from ashare_multifactor.final_test import (
        official_candidate_review_admission as admission_module,
    )
    from ashare_multifactor.final_test import (
        official_review_batch_workspace as workspace_module,
    )

    date_rule = tmp_path / "stage9_candidate_review_admission_date_rule.json"
    date_rule.write_bytes(
        admission_module.DEFAULT_ADMISSION_DATE_RULE_PATH.read_bytes()
    )
    monkeypatch.setattr(
        admission_module,
        "DEFAULT_ADMISSION_DATE_RULE_PATH",
        date_rule,
    )
    monkeypatch.setattr(
        workspace_module,
        "DEFAULT_ADMISSION_DATE_RULE_PATH",
        date_rule,
    )
    workspace, candidates = _batch_inputs(prepared_attempt)
    plan = prepare_review_batch_workspace(
        workspace=workspace,
        candidate_manifest_path=candidates.manifest_path,
        symbols_per_batch=1,
    )
    targets = {
        "admission": plan.admission_manifest_path,
        "queue": workspace.review_queue_path,
        "candidate": candidates.manifest_path,
        "date_rule": date_rule,
        "routing": workspace.routing_path,
    }
    targets[drift_target].write_bytes(b"changed")

    with pytest.raises(ValueError):
        load_verified_review_batch_workspace(
            plan.manifest_path,
            workspace=workspace,
            candidate_manifest_path=candidates.manifest_path,
        )


def test_review_batch_rejects_cross_symbol_official_evidence(
    prepared_attempt: PreparedAttempt,
) -> None:
    workspace, candidates = _batch_inputs(prepared_attempt)
    plan = prepare_review_batch_workspace(
        workspace=workspace,
        candidate_manifest_path=candidates.manifest_path,
        symbols_per_batch=2,
    )
    frames = _cross_symbol_frames(plan.batches[0])

    with pytest.raises(ValueError, match="evidence symbol differs"):
        publish_review_batch(
            workspace=workspace,
            candidate_manifest_path=candidates.manifest_path,
            batch_manifest_path=plan.batches[0].manifest_path,
            announcement_decisions=frames[0],
            corporate_action_dispositions=frames[1],
            corporate_action_facts=frames[2],
            security_event_facts=frames[3],
            reviewer_id="human-reviewer-1",
            reviewed_at="2026-07-28T12:00:00+08:00",
        )


def test_review_batch_rejects_unlinked_cross_symbol_corporate_fact(
    prepared_attempt: PreparedAttempt,
) -> None:
    workspace, candidates = _batch_inputs(prepared_attempt)
    plan = prepare_review_batch_workspace(
        workspace=workspace,
        candidate_manifest_path=candidates.manifest_path,
        symbols_per_batch=2,
    )
    batch = plan.batches[0]
    queue = pl.read_parquet(batch.queue_path).sort("symbol")
    candidate_rows = pl.read_parquet(batch.candidates_path).sort("symbol").rows(
        named=True
    )
    catalog_by_symbol = {
        row["symbol"]: row["catalog_id"]
        for row in queue.rows(named=True)
    }
    decisions = pl.DataFrame(
        {
            "catalog_id": queue.get_column("catalog_id"),
            "status": ["relevant"] * queue.height,
            "explanation": ["official implementation announcement"] * queue.height,
        }
    )
    dispositions = pl.DataFrame(
        {
            "candidate_id": [row["candidate_id"] for row in candidate_rows],
            "status": ["accepted"] * len(candidate_rows),
            "catalog_id": [
                catalog_by_symbol[row["symbol"]] for row in candidate_rows
            ],
            "explanation": ["official fields match"] * len(candidate_rows),
            "original_symbol": [row["symbol"] for row in candidate_rows],
            "corrected_symbol": [None] * len(candidate_rows),
            "original_ex_date": [row["ex_date"] for row in candidate_rows],
            "corrected_ex_date": [None] * len(candidate_rows),
            "original_effective_date": [
                row["effective_date"] for row in candidate_rows
            ],
            "corrected_effective_date": [None] * len(candidate_rows),
            "original_cash_per_share": [
                row["cash_per_share"] for row in candidate_rows
            ],
            "corrected_cash_per_share": [None] * len(candidate_rows),
            "original_share_ratio": [
                row["share_ratio"] for row in candidate_rows
            ],
            "corrected_share_ratio": [None] * len(candidate_rows),
            "correction_reason": [None] * len(candidate_rows),
        }
    )
    linked_facts = [
        {
            "catalog_id": catalog_by_symbol[row["symbol"]],
            "candidate_id": row["candidate_id"],
            "announcement_date": date(2022, 8, 1),
            "symbol": row["symbol"],
            "ex_date": row["ex_date"],
            "effective_date": row["effective_date"],
            "cash_per_share": row["cash_per_share"],
            "share_ratio": row["share_ratio"],
        }
        for row in candidate_rows
    ]
    corporate_facts = pl.DataFrame(
        [
            *linked_facts,
            {
                **linked_facts[1],
                "catalog_id": linked_facts[0]["catalog_id"],
                "candidate_id": None,
            },
        ]
    )
    security_facts = pl.DataFrame(
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

    with pytest.raises(
        ValueError,
        match="corporate-action fact evidence symbol differs",
    ):
        publish_review_batch(
            workspace=workspace,
            candidate_manifest_path=candidates.manifest_path,
            batch_manifest_path=batch.manifest_path,
            announcement_decisions=decisions,
            corporate_action_dispositions=dispositions,
            corporate_action_facts=corporate_facts,
            security_event_facts=security_facts,
            reviewer_id="human-reviewer-1",
            reviewed_at="2026-07-28T12:00:00+08:00",
        )
