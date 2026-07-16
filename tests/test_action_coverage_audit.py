from datetime import date
from pathlib import Path

import polars as pl
import pytest

from ashare_multifactor.final_test.action_coverage_audit import (
    audit_validation_action_coverage,
    load_official_action_audit_evidence,
    write_action_coverage_audit,
)


def _actions() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "action_id": ["cash-2017", "shares-2018"],
            "ex_date": [date(2017, 6, 1), date(2018, 7, 2)],
            "effective_date": [date(2017, 6, 5), date(2018, 7, 2)],
            "symbol": ["000001", "000001"],
            "cash_per_share": [0.1, 0.0],
            "share_ratio": [0.0, 0.2],
            "source": ["baostock", "baostock"],
        }
    )


def _coverage() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["000001"] * 5,
            "year": list(range(2017, 2022)),
            "status": ["ok"] * 5,
            "row_count": [1, 1, 0, 0, 0],
        }
    )


def _evidence() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "year": [2017, 2018],
            "event_type": ["cash", "shares"],
            "symbol": ["000001", "000001"],
            "ex_date": [date(2017, 6, 1), date(2018, 7, 2)],
            "effective_date": [date(2017, 6, 5), date(2018, 7, 2)],
            "cash_per_share": [0.1, 0.0],
            "share_ratio": [0.0, 0.2],
            "source_url": [
                "https://static.cninfo.com.cn/finalpage/2017/a.PDF",
                "https://static.cninfo.com.cn/finalpage/2018/b.PDF",
            ],
            "sha256": ["a" * 64, "b" * 64],
        }
    )


def test_action_audit_accepts_complete_coverage_zero_diffs_and_samples() -> None:
    result = audit_validation_action_coverage(
        symbols=["000001"],
        query_coverage=_coverage(),
        candidate_actions=_actions(),
        published_actions=_actions(),
        official_evidence=_evidence(),
    )

    assert result.status == "ready"
    assert result.summary["expected_queries"] == 5
    assert result.summary["successful_queries"] == 5
    assert result.summary["event_difference_rows"] == 0
    assert result.summary["official_sample_gaps"] == 0
    assert result.event_differences.is_empty()


def test_action_audit_rejects_missing_symbol_year_query() -> None:
    with pytest.raises(ValueError, match="query coverage"):
        audit_validation_action_coverage(
            symbols=["000001"],
            query_coverage=_coverage().head(4),
            candidate_actions=_actions(),
            published_actions=_actions(),
            official_evidence=_evidence(),
        )


def test_action_audit_rejects_business_field_difference() -> None:
    changed = _actions().with_columns(
        pl.when(pl.col("action_id") == "cash-2017")
        .then(0.2)
        .otherwise(pl.col("cash_per_share"))
        .alias("cash_per_share")
    )

    with pytest.raises(ValueError, match="differences are not zero"):
        audit_validation_action_coverage(
            symbols=["000001"],
            query_coverage=_coverage(),
            candidate_actions=changed,
            published_actions=_actions(),
            official_evidence=_evidence(),
        )


def test_action_audit_rejects_missing_official_category_year_sample() -> None:
    with pytest.raises(ValueError, match="official sample"):
        audit_validation_action_coverage(
            symbols=["000001"],
            query_coverage=_coverage(),
            candidate_actions=_actions(),
            published_actions=_actions(),
            official_evidence=_evidence().head(1),
        )


def test_action_audit_writes_hashed_machine_readable_outputs(tmp_path) -> None:
    audit = audit_validation_action_coverage(
        symbols=["000001"],
        query_coverage=_coverage(),
        candidate_actions=_actions(),
        published_actions=_actions(),
        official_evidence=_evidence(),
    )

    manifest = write_action_coverage_audit(
        tmp_path,
        audit,
        inputs={"source_cache": {"sha256": "f" * 64, "size": 1}},
    )

    assert manifest["status"] == "ready"
    assert manifest["inputs"]["source_cache"]["sha256"] == "f" * 64
    assert {item["path"] for item in manifest["files"]} == {
        "summary.json",
        "query_coverage.parquet",
        "event_differences.parquet",
        "official_evidence_index.parquet",
    }
    assert all((tmp_path / item["path"]).is_file() for item in manifest["files"])


def test_repository_official_action_samples_are_hashed_and_complete() -> None:
    evidence = load_official_action_audit_evidence(
        Path("configs/validation_action_audit_evidence.csv"),
        Path("configs/evidence/validation_action_audit"),
    )

    assert evidence.height == 10
    assert set(evidence.get_column("year")) == set(range(2017, 2022))
    assert set(evidence.get_column("event_type")) == {"cash", "shares"}
