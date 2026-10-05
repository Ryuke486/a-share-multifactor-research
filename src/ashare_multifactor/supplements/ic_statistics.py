"""Rank IC series and Newey-West tests with an explicitly chosen lag."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math

import numpy as np
import polars as pl
from scipy import stats

from ashare_multifactor.research.factor_statistics import benjamini_hochberg


def monthly_rank_ic(
    frame: pl.DataFrame, *, score: str, label: str, minimum: int = 20
) -> pl.DataFrame:
    """Spearman correlation of score and label per date over finite pairs."""
    rows: list[dict[str, object]] = []
    valid = frame.filter(pl.col(score).is_finite() & pl.col(label).is_finite())
    for (day,), group in valid.sort("date", "symbol").group_by("date", maintain_order=True):
        if group.height < minimum:
            continue
        scores = group.get_column(score).to_numpy()
        labels = group.get_column(label).to_numpy()
        if np.ptp(scores) == 0.0 or np.ptp(labels) == 0.0:
            continue
        correlation = float(stats.spearmanr(scores, labels).statistic)
        if np.isfinite(correlation):
            rows.append({"date": day, "n_obs": group.height, "rank_ic": correlation})
    return pl.DataFrame(rows, schema={"date": pl.Date, "n_obs": pl.Int64, "rank_ic": pl.Float64})


def automatic_lag(observations: int) -> int:
    """Newey and West (1994) plug-in lag: floor(4 * (T / 100) ** (2 / 9))."""
    return math.floor(4.0 * (observations / 100.0) ** (2.0 / 9.0))


def ic_summary(values: Sequence[float], *, lag: int) -> dict[str, float | int | None]:
    """Mean, annualized ICIR and a Bartlett-kernel Newey-West t test at ``lag``."""
    sample = np.asarray([value for value in values if np.isfinite(value)], dtype=float)
    n = int(sample.size)
    if n < 2:
        return {"n": n, "mean": None, "icir": None, "lag": lag, "t": None, "p": None}
    mean = float(sample.mean())
    std = float(sample.std(ddof=1))
    residuals = sample - mean
    long_run = float(residuals @ residuals / n)
    for offset in range(1, min(lag, n - 1) + 1):
        weight = 1.0 - offset / (lag + 1.0)
        long_run += 2.0 * weight * float(residuals[offset:] @ residuals[:-offset] / n)
    t = mean / math.sqrt(long_run / n) if long_run > 0.0 else None
    return {
        "n": n,
        "mean": mean,
        "icir": mean / std * math.sqrt(12.0) if std > 0.0 else None,
        "lag": lag,
        "t": t,
        "p": float(2.0 * stats.norm.sf(abs(t))) if t is not None else None,
    }


def benjamini_hochberg_with_lag(
    series: Mapping[str, Sequence[float]], *, lag: int
) -> dict[str, dict[str, float | int | None]]:
    """Test every series at one lag and BH-adjust the p-values as one family."""
    names = sorted(series)
    summaries = {name: ic_summary(series[name], lag=lag) for name in names}
    q_values = benjamini_hochberg([summaries[name]["p"] for name in names])
    return {name: {**summaries[name], "q": q} for name, q in zip(names, q_values, strict=True)}
