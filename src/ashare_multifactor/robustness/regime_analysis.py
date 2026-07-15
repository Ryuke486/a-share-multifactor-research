from __future__ import annotations

from datetime import date
import math

import numpy as np
import polars as pl


def classify_market_regimes(
    market: pl.DataFrame,
    *,
    trend_window: int = 60,
    volatility_window: int = 20,
) -> pl.DataFrame:
    """Label regimes from trailing market returns, never from strategy results."""
    required = {"date", "market_return"}
    if required - set(market.columns):
        raise ValueError("market regime input is missing date or market_return")
    frame = market.sort("date")
    values = frame.get_column("market_return").to_numpy()
    vol_history: list[float] = []
    rows: list[dict[str, object]] = []
    for index, (trade_date, value) in enumerate(
        zip(frame.get_column("date"), values, strict=True)
    ):
        trend_slice = values[max(0, index - trend_window + 1) : index + 1]
        if len(trend_slice) < trend_window or not np.isfinite(trend_slice).all():
            trend = "insufficient_history"
            trailing_return = None
        else:
            trailing_return = float(np.prod(1.0 + trend_slice) - 1.0)
            trend = "up" if trailing_return >= 0.0 else "down"
        vol_slice = values[max(0, index - volatility_window + 1) : index + 1]
        if len(vol_slice) < volatility_window or not np.isfinite(vol_slice).all():
            current_volatility = None
            volatility_regime = "insufficient_history"
        else:
            current_volatility = float(np.std(vol_slice, ddof=1) * math.sqrt(252.0))
            threshold = float(np.median(vol_history)) if vol_history else None
            volatility_regime = (
                "insufficient_history"
                if threshold is None
                else ("high" if current_volatility > threshold else "low")
            )
            vol_history.append(current_volatility)
        rows.append(
            {
                "date": trade_date,
                "market_return": float(value),
                "trailing_market_return": trailing_return,
                "market_volatility": current_volatility,
                "market_trend": trend,
                "volatility_regime": volatility_regime,
            }
        )
    return pl.DataFrame(rows)


def time_segments(frame: pl.DataFrame) -> pl.DataFrame:
    if "date" not in frame.columns:
        raise ValueError("time segmentation requires date")
    invalid = frame.filter(
        (pl.col("date") < date(2005, 1, 1))
        | (pl.col("date") > date(2021, 12, 31))
    )
    if invalid.height:
        raise ValueError("time segmentation outside robustness sample")
    return frame.with_columns(
        pl.lit("time_period").alias("segment_type"),
        pl.when(pl.col("date") <= date(2016, 12, 31))
        .then(pl.lit("research_2005_2016"))
        .otherwise(pl.lit("validation_2017_2021"))
        .alias("segment"),
    )


def size_segments(panel: pl.DataFrame) -> pl.DataFrame:
    """Build one monthly cross-sectional return per size group."""
    return (
        panel.filter(pl.col("factor_name") == "log_market_cap")
        .with_columns(
            pl.when(pl.col("raw_value") <= pl.col("raw_value").median().over("date"))
            .then(pl.lit("small"))
            .otherwise(pl.lit("large"))
            .alias("segment"),
            pl.lit("market_size").alias("segment_type"),
            pl.col("forward_return_20").alias("return"),
        )
        .group_by("date", "segment_type", "segment")
        .agg(pl.col("return").mean())
        .select("date", "return", "segment_type", "segment")
        .sort("date", "segment")
    )


def summarize_segment_returns(
    returns: pl.DataFrame,
    *,
    annualization_by_segment_type: dict[str, int] | None = None,
) -> pl.DataFrame:
    required = {"date", "return", "segment_type", "segment"}
    missing = required - set(returns.columns)
    if missing:
        raise ValueError("segment returns are missing: " + ", ".join(sorted(missing)))
    rows: list[dict[str, object]] = []
    annualization = annualization_by_segment_type or {}
    for key, group in returns.sort("date").group_by(
        "segment_type", "segment", maintain_order=True
    ):
        values = group.get_column("return").drop_nulls().to_numpy()
        values = values[np.isfinite(values)]
        observations = len(values)
        mean = float(np.mean(values)) if observations else None
        standard_error = (
            float(np.std(values, ddof=1) / math.sqrt(observations))
            if observations > 1
            else None
        )
        periods_per_year = annualization.get(str(key[0]), 252)
        rows.append(
            {
                "segment_type": key[0],
                "segment": key[1],
                "observations": observations,
                "start_date": group.get_column("date").min(),
                "end_date": group.get_column("date").max(),
                "mean_period_return": mean,
                "annualized_return": (
                    mean * periods_per_year if mean is not None else None
                ),
                "annualized_volatility": (
                    float(np.std(values, ddof=1) * math.sqrt(periods_per_year))
                    if observations > 1
                    else None
                ),
                "standard_error": standard_error,
                "ci95_low": (
                    mean - 1.96 * standard_error
                    if mean is not None and standard_error is not None
                    else None
                ),
                "ci95_high": (
                    mean + 1.96 * standard_error
                    if mean is not None and standard_error is not None
                    else None
                ),
            }
        )
    return pl.DataFrame(rows).sort("segment_type", "segment")


def industry_limitation() -> dict[str, object]:
    return {
        "status": "descriptive_only",
        "reason": "historical_industry_point_in_time_unverified",
        "eligible_for_primary_conclusion": False,
    }
