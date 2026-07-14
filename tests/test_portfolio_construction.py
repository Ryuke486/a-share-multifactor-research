from datetime import date

import polars as pl
import pytest

from ashare_multifactor.portfolio.diagnostics import (
    compare_portfolios,
    portfolio_diagnostics,
    quality_warnings,
)
from ashare_multifactor.portfolio.ranking import top_n_equal
from ashare_multifactor.portfolio.size_stratified import size_stratified_buffered


def _cross_section(count: int = 12) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [date(2005, 1, 31)] * count,
            "symbol": [f"{index:06d}" for index in range(count, 0, -1)],
            "score": [float(index // 2) for index in range(count)],
            "log_market_cap": [float(index) for index in range(count)],
        }
    )


def test_top_n_equal_is_stable_and_normalized() -> None:
    result = top_n_equal(_cross_section(), portfolio_size=5, portfolio_name="top5")

    assert result.height == 5
    assert result.get_column("target_weight").sum() == pytest.approx(1.0)
    assert result.sort("selection_rank").get_column("symbol").to_list() == [
        "000001",
        "000002",
        "000003",
        "000004",
        "000005",
    ]


def test_size_stratified_buffer_retains_prior_target_inside_buffer() -> None:
    current = _cross_section(20)
    previous = pl.DataFrame(
        {
            "symbol": ["000003"],
            "target_weight": [1.0],
        }
    )

    result = size_stratified_buffered(
        current,
        previous_targets=previous,
        size_groups=2,
        per_group=2,
        buffer_rank=3,
        portfolio_name="buffered",
    )

    assert result.height == 4
    assert "000003" in result.get_column("symbol").to_list()
    assert result.group_by("size_group").len().get_column("len").to_list() == [2, 2]
    assert result.get_column("target_weight").sum() == pytest.approx(1.0)


def test_portfolio_diagnostics_compute_one_way_turnover_and_concentration() -> None:
    current = pl.DataFrame({"symbol": ["A", "B"], "target_weight": [0.5, 0.5]})
    previous = pl.DataFrame({"symbol": ["A", "C"], "target_weight": [0.5, 0.5]})

    result = portfolio_diagnostics(current, previous)

    assert result["one_way_turnover"] == pytest.approx(0.5)
    assert result["hhi"] == pytest.approx(0.5)
    assert result["effective_holdings"] == pytest.approx(2.0)


def test_portfolio_diagnostics_accept_empty_initial_portfolio() -> None:
    current = pl.DataFrame({"symbol": ["A", "B"], "target_weight": [0.5, 0.5]})

    result = portfolio_diagnostics(current, pl.DataFrame())

    assert result["one_way_turnover"] == pytest.approx(0.5)


def test_compare_portfolios_computes_overlap_and_turnover_difference() -> None:
    baseline = pl.DataFrame(
        {"symbol": ["A", "B"], "target_weight": [0.5, 0.5]}
    )
    primary = pl.DataFrame(
        {"symbol": ["A", "C"], "target_weight": [0.5, 0.5]}
    )

    result = compare_portfolios(
        baseline,
        primary,
        baseline_turnover=0.2,
        primary_turnover=0.5,
    )

    assert result["holding_overlap"] == pytest.approx(0.5)
    assert result["turnover_difference"] == pytest.approx(0.3)


def test_quality_warnings_records_coverage_and_turnover_anomalies() -> None:
    warnings = quality_warnings(
        portfolio_name="main",
        holdings=80,
        expected_holdings=100,
        coverage=0.7,
        minimum_coverage=0.8,
        turnover=0.9,
        turnover_warning=0.8,
    )

    assert {item["rule"] for item in warnings} == {
        "insufficient_holdings",
        "coverage_decline",
        "high_target_turnover",
    }
