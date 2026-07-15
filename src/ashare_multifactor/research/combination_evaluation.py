from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
import polars as pl

from ashare_multifactor.research.factor_statistics import newey_west_mean_test


@dataclass(frozen=True)
class CombinationEvaluation:
    ic: pl.DataFrame
    quantiles: pl.DataFrame
    subperiods: pl.DataFrame
    correlations: pl.DataFrame
    correlation_monthly: pl.DataFrame
    summary: pl.DataFrame


def evaluate_combinations(
    scores: pl.DataFrame,
    returns: pl.DataFrame,
    factor_panel: pl.DataFrame | None = None,
    *,
    subperiod_boundaries: tuple[tuple[str, int, int], ...] = (
        ("2005-2008", 2005, 2008),
        ("2009-2012", 2009, 2012),
        ("2013-2016", 2013, 2016),
    ),
) -> CombinationEvaluation:
    joined = scores.join(returns, on=["date", "symbol"], how="left", validate="m:1")
    ic_rows: list[dict[str, object]] = []
    quantile_rows: list[dict[str, object]] = []
    for (signal_date, method), frame in joined.group_by("date", "method", maintain_order=True):
        for horizon in (5, 20, 60):
            label = f"forward_return_{horizon}"
            valid = frame.filter(pl.col("score").is_finite() & pl.col(label).is_finite())
            rank_ic = _spearman(valid["score"], valid[label]) if valid.height >= 3 else None
            ic_rows.append({"date": signal_date, "method": method, "horizon": horizon,
                            "n_obs": valid.height, "rank_ic": rank_ic})
        valid = frame.filter(pl.col("score").is_finite() & pl.col("forward_return_20").is_finite())
        if valid.height:
            ranked = valid.sort("score", "symbol").with_row_index("rank").with_columns(
                ((pl.col("rank") * 5 / valid.height).floor() + 1).clip(1, 5).alias("quantile")
            )
            for quantile in range(1, 6):
                values = ranked.filter(pl.col("quantile") == quantile).get_column(
                    "forward_return_20"
                ).to_numpy()
                if len(values):
                    quantile_rows.append(
                        {
                            "date": signal_date,
                            "method": method,
                            "quantile": quantile,
                            "mean_return": float(np.mean(values)),
                            "n_obs": len(values),
                        }
                    )
    ic = pl.DataFrame(ic_rows).sort("date", "method", "horizon")
    quantiles = pl.DataFrame(quantile_rows).sort("date", "method", "quantile")
    summary_rows: list[dict[str, object]] = []
    for (method, horizon), frame in ic.group_by("method", "horizon", maintain_order=True):
        values = frame.get_column("rank_ic").drop_nulls().to_list()
        nw = newey_west_mean_test(values, horizon=horizon)
        std = float(np.std(values, ddof=1)) if len(values) > 1 else None
        summary_rows.append({"method": method, "horizon": horizon, "mean_ic": nw["mean"],
                             "icir": nw["mean"] / std * math.sqrt(12) if std else None,
                             "nw_t": nw["t"], "valid_months": len(values)})
    summary = pl.DataFrame(summary_rows).sort("method", "horizon")
    q_metrics = []
    for method in sorted(set(scores.get_column("method"))):
        returns_by_quantile = []
        method_quantiles = quantiles.filter(pl.col("method") == method)
        for quantile in method_quantiles.get_column("quantile").unique().sort():
            values = quantiles.filter(
                (pl.col("method") == method) & (pl.col("quantile") == quantile)
            ).sort("date").get_column("mean_return").to_numpy()
            returns_by_quantile.append(float(np.mean(values)))
        q_metrics.append({
            "method": method,
            "q5_q1": returns_by_quantile[-1] - returns_by_quantile[0],
            "monotonicity": _spearman(
                pl.Series(range(1, len(returns_by_quantile) + 1)),
                pl.Series(returns_by_quantile),
            ),
        })
    summary = summary.join(pl.DataFrame(q_metrics), on="method", how="left", validate="m:1")
    subperiods = _subperiods(ic, subperiod_boundaries)
    correlations, correlation_monthly = _correlations(scores, factor_panel)
    return CombinationEvaluation(
        ic, quantiles, subperiods, correlations, correlation_monthly, summary
    )


def _spearman(left: pl.Series, right: pl.Series) -> float | None:
    left_ranks = left.rank("average").to_numpy()
    right_ranks = right.rank("average").to_numpy()
    value = np.corrcoef(left_ranks, right_ranks)[0, 1]
    return float(value) if value is not None and np.isfinite(value) else None


def _subperiods(
    ic: pl.DataFrame,
    boundaries: tuple[tuple[str, int, int], ...],
) -> pl.DataFrame:
    rows = []
    for label, start, end in boundaries:
        part = ic.filter(pl.col("date").dt.year().is_between(start, end))
        for row in part.group_by("method", "horizon").agg(
            pl.col("rank_ic").mean().alias("mean_ic"),
            pl.col("rank_ic").count().alias("valid_months"),
        ).iter_rows(named=True):
            rows.append({"subperiod": label, **row})
    return pl.DataFrame(
        rows,
        schema={
            "subperiod": pl.String,
            "method": pl.String,
            "horizon": pl.Int64,
            "mean_ic": pl.Float64,
            "valid_months": pl.UInt32,
        },
    ).sort("method", "horizon", "subperiod")


def _correlations(
    scores: pl.DataFrame, factor_panel: pl.DataFrame | None
) -> tuple[pl.DataFrame, pl.DataFrame]:
    wide = scores.pivot(on="method", index=["date", "symbol"], values="score")
    methods = sorted(set(scores.get_column("method")))
    pair_frames: list[pl.DataFrame] = []
    for left_index, left in enumerate(methods):
        for right in methods[left_index:]:
            pair_frames.append(_monthly_pair(wide, left, right, "composite"))
    if factor_panel is not None:
        factors = sorted(set(factor_panel.get_column("factor_name")))
        factor_wide = factor_panel.pivot(
            on="factor_name", index=["date", "symbol"], values="score_size_neutral"
        )
        combined = wide.join(factor_wide, on=["date", "symbol"], how="inner")
        for left in methods:
            for right in factors:
                pair_frames.append(_monthly_pair(combined, left, right, "single_factor"))
    monthly = pl.concat(pair_frames, how="vertical_relaxed").sort(
        "date", "left_name", "right_name"
    )
    rows = []
    for (left, right, right_type), frame in monthly.group_by(
        "left_name", "right_name", "right_type", maintain_order=True
    ):
        valid = frame.filter(pl.col("valid"))
        invalid = frame.filter(~pl.col("valid"))
        reasons = invalid.get_column("reason").drop_nulls()
        primary_reason = reasons.mode().sort().item(0) if len(reasons) else None
        rows.append(
            {
                "left_name": left,
                "right_name": right,
                "right_type": right_type,
                "mean_correlation": valid.get_column("correlation").mean(),
                "common_months": valid.height,
                "invalid_months": invalid.height,
                "primary_reason": primary_reason,
            }
        )
    return pl.DataFrame(rows).sort("left_name", "right_name"), monthly


def _monthly_pair(
    wide: pl.DataFrame, left: str, right: str, right_type: str
) -> pl.DataFrame:
    rows = []
    for (signal_date,), frame in wide.group_by("date", maintain_order=True):
        valid = frame.select(
            pl.col(left).alias("_left"), pl.col(right).alias("_right")
        ).drop_nulls()
        reason = None
        correlation = None
        if valid.height < 3:
            reason = "insufficient_common_securities"
        elif valid.get_column("_left").n_unique() < 2 or valid.get_column("_right").n_unique() < 2:
            reason = "constant_series"
        else:
            correlation = _spearman(valid["_left"], valid["_right"])
            if correlation is None:
                reason = "non_finite_correlation"
        rows.append(
            {
                "date": signal_date,
                "left_name": left,
                "right_name": right,
                "right_type": right_type,
                "common_count": valid.height,
                "correlation": correlation,
                "valid": correlation is not None,
                "reason": reason,
            }
        )
    return pl.DataFrame(rows)
