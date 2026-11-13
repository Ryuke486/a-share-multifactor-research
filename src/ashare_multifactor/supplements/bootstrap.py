"""Circular block bootstrap intervals for annualised performance figures.

Daily strategy and benchmark returns are resampled *together*, in contiguous
blocks that wrap around the sample, so every replication keeps the same-day
pairing, the cross-correlation and the short-range dependence (volatility
clustering) of the original path. Statistics use the same definitions as the
published tables: geometric annual return and geometric annual relative return
over ``years``, and a zero-risk-free-rate Sharpe ratio on daily returns.
"""

from __future__ import annotations

import math

import numpy as np

TRADING_DAYS_PER_YEAR = 252.0


def circular_block_indices(size: int, *, block: int, rng: np.random.Generator) -> np.ndarray:
    """``size`` positions made of consecutive blocks with uniformly random starts."""
    if block < 1 or block > size:
        raise ValueError("block length must be between one and the sample length")
    blocks = math.ceil(size / block)
    starts = rng.integers(0, size, size=blocks)
    return ((starts[:, None] + np.arange(block)[None, :]) % size).reshape(-1)[:size]


def block_bootstrap_intervals(
    strategy_returns: np.ndarray,
    benchmark_returns: np.ndarray,
    *,
    years: float,
    block: int,
    replications: int,
    seed: int,
    level: float = 0.95,
) -> dict[str, float]:
    """Point estimates with percentile intervals from paired block resampling."""
    strategy = np.asarray(strategy_returns, dtype=float)
    benchmark = np.asarray(benchmark_returns, dtype=float)
    if strategy.shape != benchmark.shape or strategy.ndim != 1:
        raise ValueError("strategy and benchmark returns must have the same length")
    if not (np.isfinite(strategy).all() and np.isfinite(benchmark).all()):
        raise ValueError("returns must be finite")
    if (strategy <= -1.0).any() or (benchmark <= -1.0).any():
        raise ValueError("a daily return of -100% or worse cannot be compounded")
    if years <= 0 or replications < 1 or not 0.0 < level < 1.0:
        raise ValueError("years, replications and level must be positive and valid")
    if block < 1 or block > strategy.size:
        raise ValueError("block length must be between one and the sample length")

    log_strategy = np.log1p(strategy)
    log_benchmark = np.log1p(benchmark)
    rng = np.random.default_rng(seed)
    draws = np.empty((replications, 3))
    for replication in range(replications):
        indices = circular_block_indices(strategy.size, block=block, rng=rng)
        draws[replication] = _statistics(
            strategy[indices], log_strategy[indices], log_benchmark[indices], years
        )
    point = _statistics(strategy, log_strategy, log_benchmark, years)
    tail = (1.0 - level) / 2.0
    low, high = np.quantile(draws, [tail, 1.0 - tail], axis=0)
    result: dict[str, float] = {}
    for column, name in enumerate(("annual_return", "annual_relative_return", "sharpe")):
        result[name] = float(point[column])
        result[f"{name}_low"] = float(low[column])
        result[f"{name}_high"] = float(high[column])
    result["relative_non_positive_share"] = float(np.mean(draws[:, 1] <= 1e-15))
    return result


def _statistics(
    returns: np.ndarray, log_returns: np.ndarray, log_benchmark: np.ndarray, years: float
) -> tuple[float, float, float]:
    total = float(log_returns.sum())
    relative = total - float(log_benchmark.sum())
    spread = float(returns.std(ddof=1))
    sharpe = (
        float(returns.mean()) / spread * math.sqrt(TRADING_DAYS_PER_YEAR)
        if spread > 0.0
        else math.nan
    )
    return math.expm1(total / years), math.expm1(relative / years), sharpe
