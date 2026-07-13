import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest
import yaml

from ashare_multifactor.data.field_audit import audit_factor_fields, write_data_readiness
from ashare_multifactor.factors.definitions import FACTOR_DEFINITIONS, FactorDefinition


def _frame_with_audited_fields() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [date(2005, 1, 4), date(2005, 1, 5), date(2005, 1, 6), date(2005, 1, 7)],
            "pe_ttm": [10.0, None, float("inf"), -5.0],
            "pb": [2.0, 2.1, 2.2, 2.3],
            "ps_ttm": [3.0, 3.1, 3.2, 3.3],
            "close_adj": [8.0, 8.2, 8.1, 8.4],
            "amount": [100.0, 110.0, 120.0, 130.0],
            "turnover": [0.01, 0.02, 0.03, 0.04],
            "total_market_cap": [1000.0, 1100.0, 1200.0, 1300.0],
            "industry": ["A", None, "B", "B"],
            "is_st": [False, False, None, True],
        }
    )


def test_unverified_valuation_caps_factor_at_watch() -> None:
    readiness = audit_factor_fields(_frame_with_audited_fields(), valuation_verified=False)

    assert readiness.factor_status["ep_ttm"] == "unverified"
    assert readiness.factor_status["bp"] == "unverified"
    assert readiness.factor_status["sp_ttm"] == "unverified"


def test_verified_valuation_is_ready_when_source_columns_exist() -> None:
    readiness = audit_factor_fields(_frame_with_audited_fields(), valuation_verified=True)

    assert readiness.factor_status["ep_ttm"] == "ready"
    assert readiness.factor_status["bp"] == "ready"
    assert readiness.factor_status["sp_ttm"] == "ready"


def test_missing_required_field_is_reported_per_factor() -> None:
    readiness = audit_factor_fields(_frame_with_audited_fields().drop("turnover"))

    assert readiness.factor_status["turnover_20"] == "missing"


def test_industry_neutralization_is_disabled_without_historical_evidence() -> None:
    readiness = audit_factor_fields(_frame_with_audited_fields(), industry_verified=False)

    assert readiness.industry_neutralization_enabled is False
    assert readiness.field_metrics["industry"]["point_in_time_status"] == "unverified"


def test_industry_neutralization_requires_field_and_verified_evidence() -> None:
    verified = audit_factor_fields(_frame_with_audited_fields(), industry_verified=True)
    missing = audit_factor_fields(
        _frame_with_audited_fields().drop("industry"), industry_verified=True
    )

    assert verified.industry_neutralization_enabled is True
    assert missing.industry_neutralization_enabled is False


def test_field_metrics_report_coverage_dates_evidence_and_impact() -> None:
    readiness = audit_factor_fields(
        _frame_with_audited_fields(),
        valuation_verified=False,
        historical_st_verified=False,
    )

    valuation = readiness.field_metrics["pe_ttm"]
    assert valuation == {
        "non_null_rate": pytest.approx(0.75),
        "finite_rate": pytest.approx(0.50),
        "positive_rate": pytest.approx(0.25),
        "earliest_date": "2005-01-04",
        "latest_date": "2005-01-07",
        "point_in_time_status": "unverified",
        "evidence_note": "Historical point-in-time valuation provenance has not been verified.",
        "affected_factors": ["ep_ttm"],
    }

    historical_st = readiness.field_metrics["is_st"]
    assert historical_st["point_in_time_status"] == "unverified"
    assert historical_st["evidence_note"] == (
        "Historical ST status provenance has not been verified; this affects the universe filter."
    )
    assert historical_st["affected_factors"] == [
        definition.name for definition in FACTOR_DEFINITIONS
    ]


def test_pit_fields_and_impacts_are_derived_from_injected_definitions() -> None:
    definitions = (
        FactorDefinition(
            name="custom_pit_factor",
            family="value",
            source_columns=("custom_pit",),
            lookback=0,
            direction=1,
            requires_verified_pit=True,
            size_neutralize=True,
        ),
        FactorDefinition(
            name="plain_factor",
            family="other",
            source_columns=("pe_ttm",),
            lookback=0,
            direction=1,
            requires_verified_pit=False,
            size_neutralize=True,
        ),
    )
    frame = pl.DataFrame(
        {
            "date": [date(2005, 1, 4)],
            "custom_pit": [2.0],
            "pe_ttm": [10.0],
            "industry": ["A"],
            "is_st": [False],
        }
    )

    readiness = audit_factor_fields(frame, definitions, valuation_verified=False)

    assert readiness.factor_status == {
        "custom_pit_factor": "unverified",
        "plain_factor": "ready",
    }
    assert readiness.field_metrics["custom_pit"]["point_in_time_status"] == "unverified"
    assert readiness.field_metrics["custom_pit"]["affected_factors"] == [
        "custom_pit_factor"
    ]
    assert readiness.field_metrics["pe_ttm"]["point_in_time_status"] == (
        "protocol_accepted"
    )
    assert readiness.field_metrics["pe_ttm"]["affected_factors"] == ["plain_factor"]


def test_data_readiness_json_is_thin_deterministic_serialization(tmp_path: Path) -> None:
    readiness = audit_factor_fields(_frame_with_audited_fields())
    output = tmp_path / "data_readiness.json"

    write_data_readiness(output, readiness)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload == readiness.to_dict()
    assert set(payload) == {
        "factor_status",
        "industry_neutralization_enabled",
        "field_metrics",
    }
    assert output.read_text(encoding="utf-8").endswith("\n")


def test_evidence_config_starts_unverified_without_fabricated_cross_checks() -> None:
    payload = yaml.safe_load(Path("configs/data_field_evidence.yaml").read_text(encoding="utf-8"))

    for name in ("valuation", "industry", "historical_st"):
        entry = payload["field_groups"][name]
        assert entry["point_in_time_status"] == "unverified"
        assert set(entry["cross_check"]) == {
            "source",
            "retrieval_date",
            "sample_rule",
            "result_summary",
        }
        assert entry["cross_check"]["source"] is None
        assert entry["cross_check"]["retrieval_date"] is None
        assert entry["cross_check"]["sample_rule"] is None
        assert entry["cross_check"]["result_summary"] == "No external cross-check completed."

    valuation = payload["field_groups"]["valuation"]
    assert "fields" not in valuation
    assert "affected_factors" not in valuation
