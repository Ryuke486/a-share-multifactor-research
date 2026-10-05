from datetime import date, timedelta
import math

import polars as pl
import pytest

from ashare_multifactor.research.factor_statistics import newey_west_mean_test
from ashare_multifactor.supplements.ic_labels import executable_forward_returns
from ashare_multifactor.supplements.ic_statistics import (
    automatic_lag,
    benjamini_hochberg_with_lag,
    ic_summary,
    monthly_rank_ic,
)

DAYS = [date(2017, 1, 2) + timedelta(days=offset) for offset in range(8)]


def _panel(rows: list[tuple[date, str, float, float]]) -> pl.DataFrame:
    frame = pl.DataFrame(rows, schema=["date", "symbol", "open_adj", "close_adj"], orient="row")
    return frame.with_columns(pl.lit(100).alias("volume"), pl.lit(1000.0).alias("amount"))


def _signals(rows: list[tuple[date, str]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=["date", "symbol"], orient="row")


def test_executable_label_enters_at_the_next_open_and_exits_h_observations_later() -> None:
    panel = _panel([(day, "000001", 10.0 + index, 100.0) for index, day in enumerate(DAYS)])

    labels = executable_forward_returns(
        panel, _signals([(DAYS[0], "000001")]), horizons=(2,), cutoff=DAYS[-1]
    )

    # entry open on DAYS[1] = 11, exit open two valid observations later (DAYS[3]) = 13
    assert labels.item(0, "executable_return_2") == pytest.approx(13.0 / 11.0 - 1.0)
    # the signal-day close never enters the executable label
    changed = panel.with_columns(
        pl.when(pl.col("date") == DAYS[0]).then(999.0).otherwise(pl.col("close_adj"))
        .alias("close_adj")
    )
    assert executable_forward_returns(
        changed, _signals([(DAYS[0], "000001")]), horizons=(2,), cutoff=DAYS[-1]
    ).item(0, "executable_return_2") == pytest.approx(13.0 / 11.0 - 1.0)


def test_executable_label_is_missing_when_the_next_trading_day_is_not_tradable() -> None:
    panel = _panel(
        [(day, "000001", 10.0, 10.0) for day in DAYS]
        + [(day, "600000", 10.0, 10.0) for day in DAYS if day != DAYS[1]]
    )

    labels = executable_forward_returns(
        panel,
        _signals([(DAYS[0], "000001"), (DAYS[0], "600000")]),
        horizons=(2,),
        cutoff=DAYS[-1],
    ).sort("symbol")

    assert labels.get_column("executable_return_2").to_list()[0] == pytest.approx(0.0)
    assert labels.get_column("executable_return_2").to_list()[1] is None


def test_executable_exit_skips_suspended_days_and_respects_the_period_cutoff() -> None:
    rows = [(day, "000001", 10.0 + index, 10.0) for index, day in enumerate(DAYS)]
    panel = _panel([row for row in rows if row[0] != DAYS[2]])

    labels = executable_forward_returns(
        panel, _signals([(DAYS[0], "000001")]), horizons=(2,), cutoff=DAYS[-1]
    )
    # valid observations after entry DAYS[1]: DAYS[3], DAYS[4] -> exit open 14
    assert labels.item(0, "executable_return_2") == pytest.approx(14.0 / 11.0 - 1.0)

    truncated = executable_forward_returns(
        panel, _signals([(DAYS[0], "000001")]), horizons=(2,), cutoff=DAYS[3]
    )
    assert truncated.item(0, "executable_return_2") is None


def test_monthly_rank_ic_requires_a_minimum_cross_section() -> None:
    day = DAYS[0]
    frame = pl.DataFrame(
        {
            "date": [day] * 25,
            "symbol": [f"{index:06d}" for index in range(25)],
            "score": list(range(25)),
            "label": [float(index) for index in range(25)],
        }
    )

    ic = monthly_rank_ic(frame, score="score", label="label", minimum=20)
    assert ic.item(0, "rank_ic") == pytest.approx(1.0)
    assert monthly_rank_ic(frame, score="score", label="label", minimum=30).is_empty()


def test_explicit_lag_summary_matches_the_stage_four_newey_west_statistic() -> None:
    values = [0.05, -0.02, 0.08, 0.01, 0.04, -0.03, 0.07, 0.02, 0.0, 0.06]
    for horizon, lag in ((20, 0), (60, 2)):
        reference = newey_west_mean_test(values, horizon=horizon)
        summary = ic_summary(values, lag=lag)
        assert summary["t"] == pytest.approx(reference["t"])
        assert summary["p"] == pytest.approx(reference["p"])
    summary = ic_summary(values, lag=0)
    mean = sum(values) / len(values)
    std = math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))
    assert summary["icir"] == pytest.approx(mean / std * math.sqrt(12.0))


def test_automatic_lag_follows_the_newey_west_1994_rule() -> None:
    assert automatic_lag(143) == 4
    assert automatic_lag(59) == 3
    assert automatic_lag(100) == 4


def test_bh_with_lag_reproduces_and_extends_the_published_family() -> None:
    series = {
        "a": [0.05, 0.04, 0.06, 0.05, 0.03, 0.07, 0.05, 0.04],
        "b": [0.01, -0.02, 0.03, -0.01, 0.02, 0.0, -0.01, 0.01],
    }
    by_lag = benjamini_hochberg_with_lag(series, lag=0)

    assert set(by_lag) == {"a", "b"}
    assert by_lag["a"]["q"] <= by_lag["b"]["q"]
    assert by_lag["a"]["t"] == pytest.approx(ic_summary(series["a"], lag=0)["t"])


def test_reproduction_guard_rejects_a_changed_or_missing_published_month() -> None:
    from ashare_multifactor.supplements.ic_pipeline import _assert_reproduces, _published_lag

    published = pl.DataFrame(
        {
            "period": ["p", "p"],
            "object_name": ["x", "x"],
            "date": [DAYS[0], DAYS[1]],
            "horizon": [20, 20],
            "rank_ic": [0.5, 0.1],
        }
    )
    exact = pl.DataFrame({"date": [DAYS[0], DAYS[1]], "n_obs": [30, 30], "rank_ic": [0.5, 0.1]})
    _assert_reproduces(exact, published, "p", "x", 20)

    with pytest.raises(ValueError, match="do not reproduce"):
        _assert_reproduces(
            exact.with_columns(pl.col("rank_ic") + 1e-6), published, "p", "x", 20
        )
    with pytest.raises(ValueError, match="do not reproduce"):
        _assert_reproduces(exact.head(1), published, "p", "x", 20)
    assert [_published_lag(horizon) for horizon in (5, 20, 60)] == [0, 0, 2]
