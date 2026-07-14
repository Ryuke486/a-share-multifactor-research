from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

import numpy as np
import polars as pl


def rolling_ic_weights(
    rank_ic: pl.DataFrame,
    *,
    weight_dates: Sequence[date],
    factors: Mapping[str, Sequence[str]],
    window_months: int = 36,
    minimum_months: int = 24,
    shrinkage: float = 0.5,
    realization_dates: pl.DataFrame,
) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    filtered = rank_ic.filter(
        (pl.col("score_variant") == "score_size_neutral")
        & (pl.col("horizon") == 20)
        & pl.col("rank_ic").is_finite()
    )
    filtered = filtered.join(realization_dates, on="date", how="left", validate="m:1")
    if filtered.get_column("realization_date").null_count():
        raise ValueError("every IC date must have a realization date")
    for weight_date in sorted(weight_dates):
        prior_dates = (
            filtered.filter(pl.col("realization_date") < weight_date)
            .get_column("date")
            .unique()
            .sort()
            .tail(window_months)
            .to_list()
        )
        history_start = prior_dates[0] if prior_dates else None
        history_end = prior_dates[-1] if prior_dates else None
        history = filtered.filter(pl.col("date").is_in(prior_dates))
        for family, family_factors in factors.items():
            family_factors = tuple(family_factors)
            equal = np.full(len(family_factors), 1.0 / len(family_factors))
            means: list[float] = []
            valid = True
            for factor in family_factors:
                values = history.filter(pl.col("factor_name") == factor).get_column("rank_ic")
                finite = values.drop_nulls().filter(values.drop_nulls().is_finite())
                if len(finite) < minimum_months:
                    valid = False
                means.append(max(0.0, float(finite.mean())) if len(finite) else 0.0)
            raw = np.asarray(means)
            if not valid or raw.sum() <= 0.0:
                within = equal
                fallback = True
            else:
                within = shrinkage * equal + (1.0 - shrinkage) * raw / raw.sum()
                fallback = False
            for factor, factor_weight in zip(family_factors, within, strict=True):
                rows.append(
                    {
                        "date": weight_date,
                        "family": family,
                        "factor_name": factor,
                        "factor_weight": float(factor_weight),
                        "history_start": history_start,
                        "history_end": history_end,
                        "history_months": len(prior_dates),
                        "fallback_equal": fallback,
                    }
                )
    return pl.DataFrame(rows).sort("date", "family", "factor_name")
