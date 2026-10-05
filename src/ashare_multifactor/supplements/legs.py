"""Split a top-minus-bottom quantile spread into its long and short legs."""

from __future__ import annotations

import polars as pl

from ashare_multifactor.research.factor_statistics import newey_west_mean_test


_FORWARD_HORIZON = 20


def quantile_leg_decomposition(quantiles: pl.DataFrame, *, quantile_count: int) -> pl.DataFrame:
    """Measure the top quantile and the bottom quantile against the universe mean.

    ``quantiles`` holds one row per (date, object_name, quantile) with the member count
    and mean forward return; the highest quantile holds the highest scores. The
    universe mean is the count-weighted mean over all quantiles of that month, so the
    long leg is what a long-only top-quantile portfolio earns over an equal-weight
    universe and the short leg is what avoiding (or shorting) the bottom quantile adds.
    """
    keys = ["date", "object_name", "quantile"]
    if quantiles.select(keys).is_duplicated().any():
        raise ValueError("duplicate quantile rows are not allowed")
    monthly = (
        quantiles.group_by("date", "object_name")
        .agg(
            ((pl.col("mean_return") * pl.col("n_obs")).sum() / pl.col("n_obs").sum()).alias(
                "universe"
            ),
            pl.col("mean_return").filter(pl.col("quantile") == quantile_count).first().alias("top"),
            pl.col("mean_return").filter(pl.col("quantile") == 1).first().alias("bottom"),
        )
        .drop_nulls(["top", "bottom", "universe"])
        .with_columns(
            (pl.col("top") - pl.col("universe")).alias("long_leg"),
            (pl.col("universe") - pl.col("bottom")).alias("short_leg"),
        )
        .sort("object_name", "date")
    )
    rows: list[dict[str, object]] = []
    for (object_name,), frame in monthly.group_by("object_name", maintain_order=True):
        long_test = newey_west_mean_test(frame["long_leg"].to_list(), horizon=_FORWARD_HORIZON)
        short_test = newey_west_mean_test(frame["short_leg"].to_list(), horizon=_FORWARD_HORIZON)
        long_leg = float(frame["long_leg"].mean())
        short_leg = float(frame["short_leg"].mean())
        spread = long_leg + short_leg
        rows.append(
            {
                "object_name": object_name,
                "months": frame.height,
                "long_leg": long_leg,
                "long_leg_t": long_test["t"],
                "short_leg": short_leg,
                "short_leg_t": short_test["t"],
                "spread": spread,
                "long_share": long_leg / spread if spread > 0 else None,
            }
        )
    return pl.DataFrame(
        rows,
        schema={
            "object_name": pl.String,
            "months": pl.Int64,
            "long_leg": pl.Float64,
            "long_leg_t": pl.Float64,
            "short_leg": pl.Float64,
            "short_leg_t": pl.Float64,
            "spread": pl.Float64,
            "long_share": pl.Float64,
        },
    )
