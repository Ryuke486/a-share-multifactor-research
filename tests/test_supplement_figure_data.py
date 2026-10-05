from datetime import date

import polars as pl
import pytest

from ashare_multifactor.supplements.figure_data import (
    correlation_matrix,
    cost_components,
    drawdowns,
    quintile_excess,
    rolling_mean_ic,
)


def test_quintile_excess_measures_each_group_against_the_monthly_universe_mean() -> None:
    first, second = date(2017, 1, 26), date(2017, 2, 28)
    quantiles = pl.DataFrame(
        {
            "date": [first] * 2 + [second] * 2,
            "object_name": ["f"] * 4,
            "quantile": [1, 2, 1, 2],
            "n_obs": [10, 30, 10, 10],
            "mean_return": [0.00, 0.04, 0.02, 0.00],
        }
    )

    result = quintile_excess(quantiles).sort("quantile")

    # month one universe 0.03, month two universe 0.01
    assert result.get_column("mean_excess").to_list() == pytest.approx(
        [(-0.03 + 0.01) / 2, (0.01 - 0.01) / 2]
    )
    assert result.get_column("months").to_list() == [2, 2]


def test_rolling_mean_ic_waits_for_a_full_window() -> None:
    days = [date(2017, month, 28) for month in range(1, 5)]
    series = pl.DataFrame({"date": days, "method": ["m"] * 4, "rank_ic": [0.1, 0.2, 0.3, 0.4]})

    result = rolling_mean_ic(series, window=3)

    assert result.get_column("rolling_ic").to_list()[:2] == [None, None]
    assert result.get_column("rolling_ic").to_list()[2:] == pytest.approx([0.2, 0.3])


def test_drawdowns_are_measured_from_the_running_peak() -> None:
    levels = pl.DataFrame({"date": [date(2017, 1, day) for day in (2, 3, 4, 5)], "a": [1.0, 1.2, 0.9, 1.3]})

    result = drawdowns(levels, columns=("a",))

    assert result.get_column("a").to_list() == pytest.approx([0.0, 0.0, -0.25, 0.0])


def test_cost_components_are_expressed_per_traded_amount() -> None:
    breakdown = {"commission": 30.0, "stamp_duty": 50.0, "total_cost": 80.0}

    result = cost_components(breakdown, traded_amount=10_000.0)

    assert result.get_column("component").to_list() == ["commission", "stamp_duty"]
    assert result.get_column("bps").to_list() == pytest.approx([30.0, 50.0])
    assert result.get_column("share").to_list() == pytest.approx([0.375, 0.625])
    with pytest.raises(ValueError, match="do not add up"):
        cost_components({"commission": 30.0, "total_cost": 80.0}, traded_amount=10_000.0)


def test_correlation_matrix_is_symmetric_in_the_requested_order() -> None:
    pairs = pl.DataFrame(
        {
            "factor_a": ["x", "x", "y", "y"],
            "factor_b": ["x", "y", "x", "y"],
            "mean_correlation": [1.0, -0.4, -0.4, 1.0],
        }
    )

    matrix = correlation_matrix(pairs, order=("y", "x"))

    assert matrix.tolist() == [[1.0, -0.4], [-0.4, 1.0]]
    with pytest.raises(ValueError, match="missing"):
        correlation_matrix(pairs, order=("x", "z"))
