from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from ashare_multifactor.config import MvpSettings, Paths, Period, ResearchConfig
from ashare_multifactor.research.momentum import add_momentum_and_forward_return
from ashare_multifactor.research.universe import build_universe


def _config(*, universe_size: int = 1) -> ResearchConfig:
    return ResearchConfig(
        paths=Paths(
            raw_unadjusted=Path("raw"),
            raw_backward_adjusted=Path("adj"),
            processed=Path("processed"),
            artifacts=Path("artifacts"),
        ),
        research=Period(date(2005, 1, 1), date(2016, 12, 31)),
        validation=Period(date(2017, 1, 1), date(2021, 12, 31)),
        test=Period(date(2022, 1, 1), date(2025, 12, 31)),
        smoke_data=Period(date(2012, 1, 1), date(2015, 12, 31)),
        smoke_analysis=Period(date(2014, 1, 1), date(2015, 12, 31)),
        mvp=MvpSettings(universe_size, 60, 20, 20, 10.0, 1_000_000.0),
    )


def _raw_row(day: date, symbol: str, amount: float) -> dict[str, object]:
    return {
        "date": day,
        "symbol": symbol,
        "amount": amount,
        "is_st": False,
        "open_raw": 10.0,
        "high_raw": 11.0,
        "low_raw": 9.0,
        "close_raw": 10.5,
    }


def _universe_panel() -> tuple[pl.DataFrame, list[date]]:
    days = [date(2013, 5, 1) + timedelta(days=offset) for offset in range(253)]
    rows = [_raw_row(day, "000001", 1_000.0) for day in days]
    rows.extend(_raw_row(day, "000002", 100.0) for day in days[-21:])
    return pl.DataFrame(rows), days


def test_universe_is_point_in_time_and_requires_252_observations() -> None:
    panel, days = _universe_panel()
    target_date = days[251]

    baseline = build_universe(panel, _config())
    changed_future = build_universe(
        panel.with_columns(
            pl.when((pl.col("date") == days[252]) & (pl.col("symbol") == "000002"))
            .then(1_000_000_000.0)
            .otherwise(pl.col("amount"))
            .alias("amount")
        ),
        _config(),
    )

    target = baseline.filter(pl.col("date") == target_date).sort("symbol")
    assert target.select(
        "symbol",
        "history_observations",
        "amount_mean_20",
        "liquidity_rank",
        "is_eligible",
    ).rows() == [
        ("000001", 252, 1_000.0, 1, True),
        ("000002", 20, 100.0, 2, False),
    ]
    assert (
        changed_future.filter(pl.col("date") == target_date)
        .select(
            "symbol",
            "history_observations",
            "amount_mean_20",
            "liquidity_rank",
            "is_eligible",
        )
        .sort("symbol")
        .rows()
        == target.select(
            "symbol",
            "history_observations",
            "amount_mean_20",
            "liquidity_rank",
            "is_eligible",
        ).rows()
    )


@pytest.mark.parametrize(
    ("column", "invalid_value"),
    [
        ("is_st", True),
        ("open_raw", None),
        ("high_raw", None),
        ("low_raw", None),
        ("close_raw", None),
    ],
)
def test_universe_rejects_st_and_invalid_prices(column: str, invalid_value: object) -> None:
    panel, days = _universe_panel()
    target_date = days[251]
    changed = panel.with_columns(
        pl.when((pl.col("date") == target_date) & (pl.col("symbol") == "000001"))
        .then(pl.lit(invalid_value))
        .otherwise(pl.col(column))
        .alias(column)
    )

    row = build_universe(changed, _config()).filter(
        (pl.col("date") == target_date) & (pl.col("symbol") == "000001")
    )

    assert row.get_column("is_eligible").item() is False


def test_universe_rejects_non_positive_20_observation_average_amount() -> None:
    panel, days = _universe_panel()
    target_date = days[251]
    changed = panel.with_columns(
        pl.when((pl.col("symbol") == "000001") & pl.col("date").is_between(days[232], target_date))
        .then(0.0)
        .otherwise(pl.col("amount"))
        .alias("amount")
    )

    row = build_universe(changed, _config()).filter(
        (pl.col("date") == target_date) & (pl.col("symbol") == "000001")
    )

    assert row.get_column("amount_mean_20").item() == 0.0
    assert row.get_column("is_eligible").item() is False


@pytest.mark.parametrize("invalid_amount", [float("nan"), float("inf")])
def test_universe_excludes_non_finite_amount_before_liquidity_ranking(
    invalid_amount: float,
) -> None:
    days = [date(2013, 5, 1) + timedelta(days=offset) for offset in range(253)]
    rows = [
        _raw_row(day, symbol, amount)
        for symbol, amount in (("000001", 1_000.0), ("000002", 500.0))
        for day in days
    ]
    panel = pl.DataFrame(rows).with_columns(
        pl.when((pl.col("symbol") == "000001") & (pl.col("date") == days[251]))
        .then(invalid_amount)
        .otherwise(pl.col("amount"))
        .alias("amount")
    )

    rows = (
        build_universe(panel, _config(universe_size=2))
        .filter(pl.col("date") == days[251])
        .sort("symbol")
    )

    assert rows.select("amount_mean_20", "liquidity_rank", "is_eligible").rows() == [
        (None, None, False),
        (500.0, 1, True),
    ]


def _momentum_panel(
    *,
    symbols: tuple[str, ...] = ("000001",),
    eligible: tuple[bool, ...] = (True,),
    slopes: tuple[float, ...] = (1.0,),
) -> tuple[pl.DataFrame, list[date]]:
    days = [date(2013, 11, 2) + timedelta(days=offset) for offset in range(92)]
    rows = [
        {
            "date": day,
            "symbol": symbol,
            "close_adj": 100.0 + slopes[symbol_index] * day_index,
            "is_eligible": eligible[symbol_index],
        }
        for symbol_index, symbol in enumerate(symbols)
        for day_index, day in enumerate(days)
    ]
    return pl.DataFrame(rows), days


def test_momentum_and_forward_return_use_exact_trading_observation_offsets() -> None:
    panel, days = _momentum_panel()
    target_index = 70
    target_date = days[target_index]

    baseline = add_momentum_and_forward_return(panel, _config())
    changed_after_horizon = add_momentum_and_forward_return(
        panel.with_columns(
            pl.when(pl.col("date") >= days[target_index + 21])
            .then(pl.col("close_adj") * 100.0)
            .otherwise(pl.col("close_adj"))
            .alias("close_adj")
        ),
        _config(),
    )

    row = baseline.filter(pl.col("date") == target_date).row(0, named=True)
    changed_row = changed_after_horizon.filter(pl.col("date") == target_date).row(0, named=True)
    assert row["momentum_60"] == pytest.approx(
        (100.0 + target_index) / (100.0 + target_index - 60) - 1.0
    )
    assert row["forward_return_20"] == pytest.approx(
        (100.0 + target_index + 20) / (100.0 + target_index) - 1.0
    )
    assert changed_row["momentum_60"] == pytest.approx(row["momentum_60"])
    assert changed_row["forward_return_20"] == pytest.approx(row["forward_return_20"])
    assert baseline.get_column("date").min() >= date(2014, 1, 1)
    assert baseline.get_column("date").max() <= date(2015, 12, 31)


def test_momentum_z_uses_only_eligible_stocks() -> None:
    panel, days = _momentum_panel(
        symbols=("000001", "000002", "000003"),
        eligible=(True, True, False),
        slopes=(1.0, 2.0, 100.0),
    )
    target_date = days[70]

    rows = (
        add_momentum_and_forward_return(panel, _config(universe_size=3))
        .filter(pl.col("date") == target_date)
        .sort("symbol")
    )

    first_z, second_z, excluded_z = rows.get_column("momentum_z").to_list()
    assert first_z < 0.0
    assert second_z > 0.0
    assert first_z + second_z == pytest.approx(0.0)
    assert excluded_z is None


def test_momentum_z_is_null_when_eligible_cross_section_has_zero_standard_deviation() -> None:
    panel, days = _momentum_panel(
        symbols=("000001", "000002"),
        eligible=(True, True),
        slopes=(1.0, 1.0),
    )

    rows = add_momentum_and_forward_return(panel, _config(universe_size=2)).filter(
        pl.col("date") == days[70]
    )

    assert rows.get_column("momentum_z").to_list() == [None, None]


@pytest.mark.parametrize("invalid_close", [float("nan"), float("inf"), 0.0, -1.0])
def test_non_positive_or_non_finite_close_is_excluded_without_polluting_z_scores(
    invalid_close: float,
) -> None:
    panel, days = _momentum_panel(
        symbols=("000001", "000002", "000003", "000004"),
        eligible=(True, True, True, True),
        slopes=(1.0, 2.0, 3.0, 4.0),
    )
    target_date = days[70]
    invalid = panel.with_columns(
        pl.when((pl.col("symbol") == "000004") & (pl.col("date") == target_date))
        .then(invalid_close)
        .otherwise(pl.col("close_adj"))
        .alias("close_adj")
    )
    control = panel.with_columns(
        pl.when((pl.col("symbol") == "000004") & (pl.col("date") == target_date))
        .then(False)
        .otherwise(pl.col("is_eligible"))
        .alias("is_eligible")
    )

    actual = (
        add_momentum_and_forward_return(invalid, _config(universe_size=4))
        .filter(pl.col("date") == target_date)
        .sort("symbol")
    )
    expected = (
        add_momentum_and_forward_return(control, _config(universe_size=4))
        .filter(pl.col("date") == target_date)
        .sort("symbol")
    )

    assert actual.filter(pl.col("symbol") == "000004").select(
        "momentum_60", "forward_return_20", "momentum_z"
    ).row(0) == (None, None, None)
    assert actual.filter(pl.col("symbol") != "000004").get_column(
        "momentum_z"
    ).to_list() == pytest.approx(
        expected.filter(pl.col("symbol") != "000004").get_column("momentum_z").to_list()
    )


@pytest.mark.parametrize("invalid_future_close", [float("nan"), float("inf"), 0.0])
def test_invalid_future_close_only_nulls_label_not_current_factor(
    invalid_future_close: float,
) -> None:
    panel, days = _momentum_panel(
        symbols=("000001", "000002", "000003"),
        eligible=(True, True, True),
        slopes=(1.0, 2.0, 3.0),
    )
    target_index = 70
    target_date = days[target_index]
    changed = panel.with_columns(
        pl.when((pl.col("symbol") == "000003") & (pl.col("date") == days[target_index + 20]))
        .then(invalid_future_close)
        .otherwise(pl.col("close_adj"))
        .alias("close_adj")
    )

    baseline = add_momentum_and_forward_return(panel, _config(universe_size=3))
    actual = add_momentum_and_forward_return(changed, _config(universe_size=3))
    expected_row = baseline.filter(
        (pl.col("date") == target_date) & (pl.col("symbol") == "000003")
    ).row(0, named=True)
    actual_row = actual.filter(
        (pl.col("date") == target_date) & (pl.col("symbol") == "000003")
    ).row(0, named=True)

    assert actual_row["forward_return_20"] is None
    assert actual_row["momentum_60"] == pytest.approx(expected_row["momentum_60"])
    assert actual_row["momentum_z"] == pytest.approx(expected_row["momentum_z"])
