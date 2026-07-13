from datetime import date
from pathlib import Path

import polars as pl
import pytest

from ashare_multifactor.config import MvpSettings, Paths, Period, ResearchConfig
from ashare_multifactor.research.evaluation import (
    monthly_signal_panel,
    rank_ic_by_date,
)
from ashare_multifactor.research.portfolio import build_target_weights


def _config(*, universe_size: int = 25, portfolio_size: int = 20) -> ResearchConfig:
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
        mvp=MvpSettings(universe_size, 60, 20, portfolio_size, 10.0, 1_000_000.0),
    )


def test_monthly_signal_panel_keeps_last_observed_trading_date() -> None:
    frame = pl.DataFrame(
        {
            "date": [
                date(2014, 1, 2),
                date(2014, 1, 17),
                date(2014, 1, 29),
                date(2014, 2, 10),
                date(2014, 2, 27),
            ],
            "symbol": ["000001"] * 5,
            "momentum_z": [0.1, 0.2, 0.3, 0.4, 0.5],
            "forward_return_20": [0.01, 0.02, 0.03, 0.04, 0.05],
        }
    )

    monthly = monthly_signal_panel(frame)

    assert monthly.get_column("date").to_list() == [
        date(2014, 1, 29),
        date(2014, 2, 27),
    ]


def test_rank_ic_is_one_for_identical_cross_sectional_ordering() -> None:
    signal_date = date(2014, 1, 29)
    frame = pl.DataFrame(
        {
            "date": [signal_date] * 20,
            "symbol": [f"{index:06d}" for index in range(20)],
            "momentum_z": [float(index) for index in range(20)],
            "forward_return_20": [float(index * 2) for index in range(20)],
        }
    )

    result = rank_ic_by_date(frame)

    assert result.columns == ["date", "n_stocks", "rank_ic", "reason"]
    assert result.get_column("date").item() == signal_date
    assert result.get_column("n_stocks").item() == 20
    assert result.get_column("rank_ic").item() == pytest.approx(1.0)
    assert result.get_column("reason").item() is None


def test_rank_ic_is_null_and_records_insufficient_cross_section() -> None:
    frame = pl.DataFrame(
        {
            "date": [date(2014, 1, 29)] * 19,
            "momentum_z": [float(index) for index in range(19)],
            "forward_return_20": [float(index) for index in range(19)],
        }
    )

    result = rank_ic_by_date(frame)

    assert result.get_column("n_stocks").item() == 19
    assert result.get_column("rank_ic").item() is None
    assert result.get_column("reason").item() == "insufficient_stocks"


def test_rank_ic_is_null_and_records_constant_factor() -> None:
    frame = pl.DataFrame(
        {
            "date": [date(2014, 1, 29)] * 20,
            "momentum_z": [1.0] * 20,
            "forward_return_20": [float(index) for index in range(20)],
        }
    )

    result = rank_ic_by_date(frame)

    assert result.get_column("n_stocks").item() == 20
    assert result.get_column("rank_ic").item() is None
    assert result.get_column("reason").item() == "constant_factor"


def test_target_weights_select_top_twenty_and_use_next_observed_trading_date() -> None:
    signal_date = date(2014, 1, 29)
    execution_date = date(2014, 2, 3)
    frame = pl.DataFrame(
        {
            "date": [signal_date] * 25,
            "symbol": [f"{index:06d}" for index in range(25)],
            "momentum_z": [float(index) for index in range(25)],
            "forward_return_20": [float(25 - index) for index in range(25)],
        }
    )
    trading_dates = pl.DataFrame({"date": [signal_date, execution_date]})

    targets = build_target_weights(frame, trading_dates, _config())

    assert targets.columns == [
        "signal_date",
        "execution_date",
        "symbol",
        "target_weight",
    ]
    assert targets.get_column("symbol").to_list() == [
        f"{index:06d}" for index in range(5, 25)
    ]
    assert targets.get_column("target_weight").to_list() == pytest.approx([0.05] * 20)
    assert targets.get_column("target_weight").sum() == pytest.approx(1.0)
    assert targets.get_column("signal_date").unique().item() == signal_date
    assert targets.get_column("execution_date").unique().item() == execution_date


def test_target_weights_break_equal_factor_scores_by_symbol() -> None:
    signal_date = date(2014, 1, 29)
    symbols = [f"{index:06d}" for index in reversed(range(21))]
    frame = pl.DataFrame(
        {
            "date": [signal_date] * len(symbols),
            "symbol": symbols,
            "momentum_z": [1.0] * len(symbols),
        }
    )
    trading_dates = pl.DataFrame(
        {"date": [signal_date, date(2014, 2, 3)]}
    )

    targets = build_target_weights(frame, trading_dates, _config(universe_size=21))

    assert targets.get_column("symbol").to_list() == [
        f"{index:06d}" for index in range(20)
    ]


def test_target_weights_drop_signal_without_later_trading_date() -> None:
    executable_signal = date(2014, 1, 29)
    next_trading_date = date(2014, 2, 3)
    final_signal = date(2014, 2, 27)
    frame = pl.DataFrame(
        {
            "date": [executable_signal, final_signal],
            "symbol": ["000001", "000002"],
            "momentum_z": [1.0, 2.0],
        }
    )
    trading_dates = pl.DataFrame(
        {"date": [executable_signal, next_trading_date, final_signal]}
    )

    targets = build_target_weights(
        frame,
        trading_dates,
        _config(universe_size=2, portfolio_size=1),
    )

    assert targets.select("signal_date", "execution_date", "symbol").rows() == [
        (executable_signal, next_trading_date, "000001")
    ]
