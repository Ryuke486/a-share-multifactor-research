from datetime import date
from pathlib import Path

import polars as pl

from ashare_multifactor.config import load_config
from ashare_multifactor.validation.selection import select_validation_candidate
from ashare_multifactor.validation.metrics import validation_portfolio_metrics


def _metrics(
    rolling_return: float,
    *,
    rolling_drawdown: float = -0.24,
    rolling_turnover: float = 0.74,
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "candidate": [
                "family_equal_size_stratified_buffered",
                "rolling_ic_family_size_stratified_buffered",
                "family_equal_top100_equal",
            ],
            "net_annual_return": [0.10, rolling_return, 0.105],
            "maximum_drawdown": [-0.20, rolling_drawdown, -0.18],
            "turnover": [0.70, rolling_turnover, 0.85],
        }
    )


def test_selection_keeps_simple_default_without_preregistered_improvement() -> None:
    settings = load_config(
        Path("configs/research_protocol.yaml")
    ).validation_evaluation

    decision = select_validation_candidate(_metrics(0.109), settings)

    assert decision["selected_candidate"] == settings.default_candidate
    failures = {
        row["candidate"]: row["failed_rules"] for row in decision["candidates"]
    }
    assert "minimum_return_improvement" in failures[
        "rolling_ic_family_size_stratified_buffered"
    ]
    assert "maximum_turnover_increase" in failures["family_equal_top100_equal"]


def test_selection_accepts_dynamic_only_when_every_gate_passes() -> None:
    settings = load_config(
        Path("configs/research_protocol.yaml")
    ).validation_evaluation

    decision = select_validation_candidate(
        _metrics(0.12, rolling_drawdown=-0.24, rolling_turnover=0.78), settings
    )

    assert decision["selected_candidate"] == (
        "rolling_ic_family_size_stratified_buffered"
    )
    selected = next(
        row for row in decision["candidates"] if row["selected"]
    )
    assert selected["failed_rules"] == []


def test_selection_reports_negative_results_without_rewriting_rules() -> None:
    settings = load_config(
        Path("configs/research_protocol.yaml")
    ).validation_evaluation
    negative = _metrics(-0.20).with_columns(pl.lit(-0.10).alias("net_annual_return"))

    decision = select_validation_candidate(negative, settings)

    assert decision["selected_candidate"] == settings.default_candidate
    assert decision["status"] == "selected_by_frozen_rules"


def test_validation_metrics_compute_cash_weight_from_nav() -> None:
    result = {
        "nav": pl.DataFrame(
            {
                "date": [date(2016, 12, 30), date(2017, 1, 3), date(2021, 12, 31)],
                "nav": [100.0, 100.0, 110.0],
                "zero_cost_nav": [100.0, 101.0, 115.0],
                "cash": [100.0, 20.0, 22.0],
            }
        ),
        "target_diagnostics": pl.DataFrame(
            {
                "date": [date(2017, 1, 3)],
                "target_deviation_l1": [0.1],
            }
        ),
        "scenario_reconciliation": pl.DataFrame(
            {"date": [date(2017, 1, 3)], "difference": [0.0]}
        ),
    }
    diagnostics = pl.DataFrame(
        {"date": [date(2017, 1, 31)], "one_way_turnover": [0.7]}
    )

    metrics = validation_portfolio_metrics("candidate", result, diagnostics)

    assert metrics["average_cash_weight"] == 0.2
