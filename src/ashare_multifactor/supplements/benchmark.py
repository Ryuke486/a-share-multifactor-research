"""Zero-cost universe benchmarks and strategy-relative statistics."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
import math

import numpy as np
import polars as pl


TRADING_DAYS_PER_YEAR = 252


def universe_benchmark_index(
    prices: pl.DataFrame,
    members: pl.DataFrame,
    *,
    trading_dates: Sequence[date],
) -> pl.DataFrame:
    """Chain buy-and-hold universe portfolios formed at each signal-date close.

    ``members`` holds one row per (signal date, symbol) with a positive ``market_cap``.
    A window starts after its signal close and ends at the next signal close, so a
    trading day's return always belongs to the membership decided before that day.
    Members without a later close are carried at their last close (suspension or
    delisting). Days before the first signal are flat.
    """
    dates = sorted(trading_dates)
    if len(set(dates)) != len(dates):
        raise ValueError("trading dates must be unique")
    signal_dates = sorted(set(members.get_column("date").to_list()))
    off_calendar = sorted(set(signal_dates) - set(dates))
    if off_calendar:
        raise ValueError(f"signal date {off_calendar[0]} is not a trading date")
    if members.select("date", "symbol").is_duplicated().any():
        raise ValueError("duplicate benchmark members are not allowed")

    calendar = pl.DataFrame({"date": dates}, schema={"date": pl.Date})
    symbols = members.select("symbol").unique()
    filled = (
        calendar.join(symbols, how="cross")
        .join(
            prices.filter(pl.col("close_adj").is_finite() & (pl.col("close_adj") > 0)).select(
                "date", "symbol", "close_adj"
            ),
            on=["date", "symbol"],
            how="left",
        )
        .sort("symbol", "date")
        .with_columns(pl.col("close_adj").forward_fill().over("symbol").alias("price"))
    )
    # each trading day belongs to the window opened by the last signal close before it
    day_windows = (
        calendar.join_asof(
            pl.DataFrame({"window_start": signal_dates}, schema={"window_start": pl.Date}),
            left_on="date",
            right_on="window_start",
            strategy="backward",
            allow_exact_matches=False,
        )
        .drop_nulls("window_start")
    )
    based = members.join(
        filled.select(
            pl.col("date").alias("window_start"), "symbol", pl.col("close_adj").alias("base")
        ),
        left_on=["date", "symbol"],
        right_on=["window_start", "symbol"],
        how="left",
    )
    unpriced = based.filter(pl.col("base").is_null())
    if unpriced.height:
        row = unpriced.row(0, named=True)
        raise ValueError(f"member {row['symbol']} has no close on its signal date {row['date']}")
    weighted = (
        based.rename({"date": "window_start"})
        .with_columns(
            (1.0 / pl.len().over("window_start")).alias("equal_weight"),
            (pl.col("market_cap") / pl.col("market_cap").sum().over("window_start")).alias(
                "cap_weight"
            ),
        )
    )
    held = (
        filled.select("date", "symbol", "price")
        .join(day_windows, on="date", how="inner")
        .join(weighted, on=["window_start", "symbol"], how="inner")
        .with_columns((pl.col("price") / pl.col("base")).alias("growth"))
        .group_by("window_start", "date")
        .agg(
            (pl.col("equal_weight") * pl.col("growth")).sum().alias("equal_growth"),
            (pl.col("cap_weight") * pl.col("growth")).sum().alias("cap_growth"),
            pl.len().alias("member_count"),
        )
        .sort("date")
    )
    return _chain_windows(calendar, held, signal_dates)


def _chain_windows(
    calendar: pl.DataFrame,
    held: pl.DataFrame,
    signal_dates: list[date],
) -> pl.DataFrame:
    growth = {
        row["date"]: (row["window_start"], row["equal_growth"], row["cap_growth"], row["member_count"])
        for row in held.iter_rows(named=True)
    }
    starts = set(signal_dates)
    equal_level = cap_level = 1.0
    equal_base = cap_base = 1.0
    rows: list[dict[str, object]] = []
    for day in calendar.get_column("date").to_list():
        count = 0
        if day in growth:
            _, equal_growth, cap_growth, count = growth[day]
            equal_level = equal_base * equal_growth
            cap_level = cap_base * cap_growth
        rows.append(
            {"date": day, "equal_weight": equal_level, "cap_weight": cap_level, "member_count": count}
        )
        if day in starts:
            equal_base, cap_base = equal_level, cap_level
    return pl.DataFrame(
        rows,
        schema={
            "date": pl.Date,
            "equal_weight": pl.Float64,
            "cap_weight": pl.Float64,
            "member_count": pl.Int64,
        },
    )


def relative_performance(
    *,
    strategy_levels: Sequence[float],
    benchmark_levels: Sequence[float],
    years: float,
) -> dict[str, float]:
    """Compare two level paths that both start at the same boundary observation."""
    strategy = np.asarray(strategy_levels, dtype=float)
    benchmark = np.asarray(benchmark_levels, dtype=float)
    if strategy.shape != benchmark.shape:
        raise ValueError("strategy and benchmark levels must have the same length")
    if strategy.size < 3 or years <= 0:
        raise ValueError("relative performance needs at least two returns and positive years")
    strategy_returns = strategy[1:] / strategy[:-1] - 1.0
    benchmark_returns = benchmark[1:] / benchmark[:-1] - 1.0
    active = strategy_returns - benchmark_returns
    strategy_growth = float(strategy[-1] / strategy[0])
    benchmark_growth = float(benchmark[-1] / benchmark[0])
    tracking_error = float(np.std(active, ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))
    benchmark_variance = float(np.var(benchmark_returns, ddof=1))
    covariance = float(np.cov(strategy_returns, benchmark_returns, ddof=1)[0, 1])
    return {
        "strategy_annual_return": strategy_growth ** (1.0 / years) - 1.0,
        "benchmark_annual_return": benchmark_growth ** (1.0 / years) - 1.0,
        "annual_relative_return": (strategy_growth / benchmark_growth) ** (1.0 / years) - 1.0,
        "tracking_error": tracking_error,
        "information_ratio": (
            float(np.mean(active)) * TRADING_DAYS_PER_YEAR / tracking_error
            if tracking_error
            else math.nan
        ),
        "beta": covariance / benchmark_variance if benchmark_variance else math.nan,
        "correlation": float(np.corrcoef(strategy_returns, benchmark_returns)[0, 1]),
    }
