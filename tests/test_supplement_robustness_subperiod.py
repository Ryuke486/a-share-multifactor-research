from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from ashare_multifactor.supplements.robustness_subperiod import (
    judge_against_baseline,
    validation_slice_metrics,
)

BOUNDARY = date(2016, 12, 30)
START = date(2017, 1, 1)
END = date(2021, 12, 31)


def _result(
    research_growth: float, validation_levels: list[float], *, traded: float = 0.0
) -> dict[str, pl.DataFrame]:
    research_days = [date(2016, 12, 28), date(2016, 12, 29), BOUNDARY]
    step = (END - date(2017, 1, 3)) / (len(validation_levels) - 1)
    validation_days = [date(2017, 1, 3) + step * index for index in range(len(validation_levels))]
    validation_days[-1] = END
    start = 100.0 * research_growth
    levels = [100.0, 100.0 * research_growth**0.5, start, *[start * x for x in validation_levels]]
    nav = pl.DataFrame(
        {
            "date": research_days + validation_days,
            "nav": levels,
            "zero_cost_nav": [level * 1.0 for level in levels],
        }
    )
    trades = pl.DataFrame(
        {
            "date": [date(2016, 6, 1), validation_days[1]],
            "amount": [9_999.0, traded],
        }
    )
    return {"nav": nav, "trades": trades}


def test_metrics_are_measured_from_the_boundary_level_inside_the_validation_slice() -> None:
    metrics = validation_slice_metrics(
        _result(3.0, [0.9, 0.6, 1.2, 1.5], traded=600.0), boundary=BOUNDARY, start=START, end=END
    )

    assert metrics["start_date"] == BOUNDARY
    assert metrics["end_date"] == END
    assert metrics["years"] == pytest.approx((END - START).days / 365.25)
    assert metrics["annual_return"] == pytest.approx(1.5 ** (1 / metrics["years"]) - 1.0)
    assert metrics["maximum_drawdown"] == pytest.approx(0.6 - 1.0)
    mean_nav = 300.0 * (1.0 + 0.9 + 0.6 + 1.2 + 1.5) / 5
    assert metrics["turnover"] == pytest.approx(600.0 / (2.0 * mean_nav * metrics["years"]))


def test_the_research_path_does_not_move_a_validation_metric() -> None:
    validation = [1.1, 0.8, 1.3]

    low = validation_slice_metrics(
        _result(2.0, validation), boundary=BOUNDARY, start=START, end=END
    )
    high = validation_slice_metrics(
        _result(7.0, validation), boundary=BOUNDARY, start=START, end=END
    )

    for name in ("annual_return", "zero_cost_annual_return", "maximum_drawdown"):
        assert high[name] == pytest.approx(low[name], abs=1e-12)


def test_a_path_without_the_boundary_observation_is_rejected() -> None:
    result = _result(2.0, [1.1, 1.2])
    result["nav"] = result["nav"].filter(pl.col("date") != BOUNDARY)

    with pytest.raises(ValueError, match="boundary"):
        validation_slice_metrics(result, boundary=BOUNDARY, start=START, end=END)

    late = _result(2.0, [1.1, 1.2])
    late["nav"] = pl.concat(
        [late["nav"], late["nav"].tail(1).with_columns(pl.col("date") + timedelta(days=3))]
    )
    with pytest.raises(ValueError, match="sealed"):
        validation_slice_metrics(late, boundary=BOUNDARY, start=START, end=END)


def test_judgement_applies_the_frozen_tolerances_metric_by_metric() -> None:
    tolerances = {"annual_return": 0.02, "maximum_drawdown": 0.05, "turnover": 0.15}
    baseline = {"annual_return": -0.06, "maximum_drawdown": -0.50, "turnover": 4.0}

    stable = judge_against_baseline(
        {"annual_return": -0.045, "maximum_drawdown": -0.54, "turnover": 4.1},
        baseline,
        tolerances,
    )
    sensitive = judge_against_baseline(
        {"annual_return": -0.085, "maximum_drawdown": -0.56, "turnover": 4.2},
        baseline,
        tolerances,
    )
    shallower = judge_against_baseline(
        {"annual_return": -0.06, "maximum_drawdown": -0.30, "turnover": 4.0},
        baseline,
        tolerances,
    )

    assert stable == {
        "annual_return": "stable",
        "maximum_drawdown": "stable",
        "turnover": "stable",
    }
    assert sensitive["annual_return"] == "sensitive"
    assert sensitive["maximum_drawdown"] == "sensitive"
    assert sensitive["turnover"] == "sensitive"
    assert shallower["maximum_drawdown"] == "stable"
