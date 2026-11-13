"""Fama-MacBeth cross-sectional regressions of forward returns on factor scores.

Each signal date gets one OLS regression of the forward return on an intercept and
the factor scores of every stock with a complete row. Scores are standardised
inside the regression sample, so a coefficient is the return premium per one
cross-sectional standard deviation of that factor *holding the other regressors
fixed* - the incremental information a univariate Rank IC cannot show. The time
series of monthly coefficients is then tested like the published Rank IC series.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import polars as pl

from ashare_multifactor.supplements.ic_statistics import automatic_lag, ic_summary

_MONTHLY_SCHEMA = {
    "date": pl.Date,
    "factor_name": pl.String,
    "coefficient": pl.Float64,
    "n_obs": pl.Int64,
    "r_squared": pl.Float64,
}


def monthly_cross_sectional_regressions(
    frame: pl.DataFrame,
    *,
    factors: Sequence[str],
    label: str,
    minimum: int,
) -> pl.DataFrame:
    """One row per (date, factor) with the OLS slope on the standardised score.

    A date is skipped when fewer than ``minimum`` complete rows remain or when a
    regressor is constant in the sample. The label is only ever the dependent
    variable.
    """
    names = list(factors)
    if label in names:
        raise ValueError("the label must not be used as a regressor")
    if len(set(names)) != len(names) or not names:
        raise ValueError("regressors must be a non-empty list of distinct factors")
    if frame.select("date", "symbol").is_duplicated().any():
        raise ValueError("duplicate (date, symbol) rows are not allowed")
    complete = frame.select("date", "symbol", *names, label).filter(
        pl.all_horizontal(pl.col(column).is_finite() for column in (*names, label))
    )
    rows: list[dict[str, object]] = []
    for (day,), group in complete.sort("date", "symbol").group_by("date", maintain_order=True):
        if group.height < max(minimum, len(names) + 2):
            continue
        scores = group.select(names).to_numpy()
        spread = scores.std(axis=0, ddof=1)
        if not np.all(spread > 0.0):
            continue
        design = np.column_stack([np.ones(group.height), (scores - scores.mean(axis=0)) / spread])
        returns = group.get_column(label).to_numpy()
        solution, *_ = np.linalg.lstsq(design, returns, rcond=None)
        residual = returns - design @ solution
        total = float(((returns - returns.mean()) ** 2).sum())
        r_squared = 1.0 - float(residual @ residual) / total if total > 0.0 else float("nan")
        for name, coefficient in zip(names, solution[1:], strict=True):
            rows.append(
                {
                    "date": day,
                    "factor_name": name,
                    "coefficient": float(coefficient),
                    "n_obs": group.height,
                    "r_squared": r_squared,
                }
            )
    return pl.DataFrame(rows, schema=_MONTHLY_SCHEMA)


def fama_macbeth_summary(monthly: pl.DataFrame, *, published_lag: int) -> pl.DataFrame:
    """Mean premium per factor with Newey-West t at the published and automatic lags."""
    rows: list[dict[str, object]] = []
    ordered = monthly.sort("factor_name", "date")
    for (name,), series in ordered.group_by("factor_name", maintain_order=True):
        values = series.get_column("coefficient").to_list()
        auto = automatic_lag(len(values))
        base = ic_summary(values, lag=published_lag)
        alternative = ic_summary(values, lag=auto)
        rows.append(
            {
                "factor_name": name,
                "months": base["n"],
                "mean_coefficient": base["mean"],
                "published_lag": published_lag,
                "t_published_lag": base["t"],
                "automatic_lag": auto,
                "t_automatic_lag": alternative["t"],
                "mean_n_obs": float(series.get_column("n_obs").mean()),
                "mean_r_squared": float(series.get_column("r_squared").mean()),
            }
        )
    return pl.DataFrame(
        rows,
        schema={
            "factor_name": pl.String,
            "months": pl.Int64,
            "mean_coefficient": pl.Float64,
            "published_lag": pl.Int64,
            "t_published_lag": pl.Float64,
            "automatic_lag": pl.Int64,
            "t_automatic_lag": pl.Float64,
            "mean_n_obs": pl.Float64,
            "mean_r_squared": pl.Float64,
        },
    )
