from datetime import date

import polars as pl
import pytest

from ashare_multifactor.supplements.benchmark import (
    relative_performance,
    universe_benchmark_index,
)

D = [date(2017, 1, day) for day in (2, 3, 4, 5, 6, 9)]


def _prices(rows: list[tuple[date, str, float]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=["date", "symbol", "close_adj"], orient="row")


def _members(rows: list[tuple[date, str, float]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=["date", "symbol", "market_cap"], orient="row")


def _levels(frame: pl.DataFrame, column: str) -> list[float]:
    return [round(value, 12) for value in frame.get_column(column).to_list()]


def test_equal_and_cap_weighted_indices_buy_and_hold_within_a_window() -> None:
    prices = _prices(
        [
            (D[0], "000001", 10.0), (D[0], "600000", 20.0),
            (D[1], "000001", 11.0), (D[1], "600000", 20.0),
            (D[2], "000001", 12.1), (D[2], "600000", 30.0),
        ]
    )
    members = _members([(D[0], "000001", 1.0), (D[0], "600000", 3.0)])

    index = universe_benchmark_index(prices, members, trading_dates=D[:3])

    # buy-and-hold from the signal close: weights drift with prices, no daily rebalance
    assert _levels(index, "equal_weight") == [1.0, 1.05, 1.355]
    assert _levels(index, "cap_weight") == [1.0, 1.025, 1.4275]
    assert index.get_column("member_count").to_list() == [0, 2, 2]


def test_membership_changes_only_after_the_next_signal_close() -> None:
    prices = _prices(
        [
            (D[0], "000001", 10.0), (D[0], "600000", 10.0),
            (D[1], "000001", 10.0), (D[1], "600000", 10.0),
            (D[2], "000001", 20.0), (D[2], "600000", 10.0),
            (D[3], "000001", 20.0), (D[3], "600000", 15.0),
        ]
    )
    members = _members(
        [
            (D[0], "000001", 1.0),
            (D[1], "600000", 1.0),
        ]
    )
    index = universe_benchmark_index(prices, members, trading_dates=D[:4])
    # D[1] is the second signal date: its own return still belongs to the first window
    # (000001 flat), D[2] belongs to the second window (600000 flat), D[3] +50%.
    assert _levels(index, "equal_weight") == [1.0, 1.0, 1.0, 1.5]

    later_member_moves = prices.with_columns(
        pl.when((pl.col("symbol") == "600000") & (pl.col("date") == D[1]))
        .then(pl.lit(999.0))
        .otherwise(pl.col("close_adj"))
        .alias("close_adj")
    )
    changed = universe_benchmark_index(later_member_moves, members, trading_dates=D[:4])
    # a new member's price up to and including its signal close cannot enter the index
    assert _levels(changed, "equal_weight")[:2] == [1.0, 1.0]


def test_suspended_and_delisted_members_are_held_at_their_last_close() -> None:
    prices = _prices(
        [
            (D[0], "000001", 10.0), (D[0], "600000", 10.0),
            (D[1], "000001", 12.0),
            (D[3], "000001", 12.0), (D[3], "600000", 5.0),
        ]
    )
    members = _members([(D[0], "000001", 1.0), (D[0], "600000", 1.0)])

    index = universe_benchmark_index(prices, members, trading_dates=D[:4])

    assert _levels(index, "equal_weight") == [1.0, 1.1, 1.1, 0.85]


def test_index_is_flat_before_the_first_signal_and_rejects_unpriced_members() -> None:
    prices = _prices([(D[1], "000001", 10.0), (D[2], "000001", 11.0)])
    index = universe_benchmark_index(
        prices, _members([(D[1], "000001", 1.0)]), trading_dates=D[:3]
    )
    assert _levels(index, "equal_weight") == [1.0, 1.0, 1.1]

    with pytest.raises(ValueError, match="no close on its signal date"):
        universe_benchmark_index(
            prices, _members([(D[0], "000001", 1.0)]), trading_dates=D[:3]
        )
    with pytest.raises(ValueError, match="not a trading date"):
        universe_benchmark_index(
            prices, _members([(date(2017, 1, 7), "000001", 1.0)]), trading_dates=D[:3]
        )


def test_relative_performance_uses_the_strategy_annualization_convention() -> None:
    strategy = [100.0, 110.0, 99.0, 118.8]
    benchmark = [1.0, 1.05, 1.0, 1.1]

    result = relative_performance(
        strategy_levels=strategy,
        benchmark_levels=benchmark,
        years=2.0,
    )

    assert result["strategy_annual_return"] == pytest.approx(1.188**0.5 - 1)
    assert result["benchmark_annual_return"] == pytest.approx(1.1**0.5 - 1)
    assert result["annual_relative_return"] == pytest.approx((1.188 / 1.1) ** 0.5 - 1)
    strategy_returns = [0.1, -0.1, 0.2]
    benchmark_returns = [0.05, 1.0 / 1.05 - 1, 0.1]
    active = [s - b for s, b in zip(strategy_returns, benchmark_returns, strict=True)]
    mean_active = sum(active) / 3
    tracking = (sum((a - mean_active) ** 2 for a in active) / 2) ** 0.5 * 252**0.5
    assert result["tracking_error"] == pytest.approx(tracking)
    assert result["information_ratio"] == pytest.approx(mean_active * 252 / tracking)


def test_relative_performance_recovers_a_known_beta() -> None:
    benchmark_returns = [0.01, -0.02, 0.015, 0.0, -0.005]
    benchmark = [1.0]
    strategy = [1.0]
    for value in benchmark_returns:
        benchmark.append(benchmark[-1] * (1 + value))
        strategy.append(strategy[-1] * (1 + 2 * value + 0.001))

    result = relative_performance(
        strategy_levels=strategy, benchmark_levels=benchmark, years=1.0
    )

    assert result["beta"] == pytest.approx(2.0)
    assert result["correlation"] == pytest.approx(1.0)


def test_relative_performance_rejects_misaligned_series() -> None:
    with pytest.raises(ValueError, match="same length"):
        relative_performance(strategy_levels=[1.0, 2.0], benchmark_levels=[1.0], years=1.0)
