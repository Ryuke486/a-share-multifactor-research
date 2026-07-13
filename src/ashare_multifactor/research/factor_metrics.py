"""Pure monthly and cross-sectional metrics for single-factor research."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
import math

import numpy as np
import polars as pl
from scipy import stats

from ashare_multifactor.config import FactorResearchSettings
from ashare_multifactor.factors.definitions import FactorDefinition


MINIMUM_CROSS_SECTION = 20
SUBPERIODS = (
    ("2005-2008", date(2005, 1, 1), date(2008, 12, 31)),
    ("2009-2012", date(2009, 1, 1), date(2012, 12, 31)),
    ("2013-2016", date(2013, 1, 1), date(2016, 12, 31)),
)
RANK_IC_SCHEMA = {
    "date": pl.Date,
    "factor_name": pl.String,
    "family": pl.String,
    "score_variant": pl.String,
    "horizon": pl.Int64,
    "n_obs": pl.Int64,
    "rank_ic": pl.Float64,
    "reason": pl.String,
}
QUANTILE_SCHEMA = {
    "date": pl.Date,
    "factor_name": pl.String,
    "family": pl.String,
    "score_variant": pl.String,
    "quantile": pl.Int64,
    "n_obs": pl.Int64,
    "mean_forward_return_20": pl.Float64,
}
SUBPERIOD_SCHEMA = {
    "factor_name": pl.String,
    "family": pl.String,
    "score_variant": pl.String,
    "subperiod": pl.String,
    "start": pl.Date,
    "end": pl.Date,
    "valid_months": pl.Int64,
    "mean_ic": pl.Float64,
}
TURNOVER_SCHEMA = {
    "date": pl.Date,
    "factor_name": pl.String,
    "family": pl.String,
    "score_variant": pl.String,
    "previous_date": pl.Date,
    "top_count": pl.Int64,
    "previous_top_count": pl.Int64,
    "turnover": pl.Float64,
}


@dataclass(frozen=True)
class FactorMetricsBundle:
    rank_ic: pl.DataFrame
    quantile_returns: pl.DataFrame
    subperiod_metrics: pl.DataFrame
    factor_turnover: pl.DataFrame


def build_factor_metrics(
    panel: pl.DataFrame,
    definitions: Sequence[FactorDefinition],
    settings: FactorResearchSettings,
    variants_by_factor: Mapping[str, Sequence[str]],
) -> FactorMetricsBundle:
    """Build reproducible monthly metrics without statistical selection."""
    groups_by_factor = _partition_panel(panel)
    rank_ic = _build_rank_ic(groups_by_factor, definitions, settings, variants_by_factor)
    return FactorMetricsBundle(
        rank_ic=rank_ic,
        quantile_returns=_build_quantile_returns(
            groups_by_factor, definitions, settings, variants_by_factor
        ),
        subperiod_metrics=_build_subperiod_metrics(
            rank_ic, definitions, variants_by_factor, settings.primary_horizon
        ),
        factor_turnover=_build_turnover(groups_by_factor, definitions, variants_by_factor),
    )


def quantile_metrics(
    quantiles: pl.DataFrame,
    factor_name: str,
    variant: str,
    quantile_count: int,
) -> tuple[float | None, float | None]:
    """Return mean monthly Q-high minus Q-low and time-mean monotonicity."""
    selected = quantiles.filter(
        (pl.col("factor_name") == factor_name) & (pl.col("score_variant") == variant)
    )
    if selected.is_empty():
        return None, None
    monthly = selected.pivot(on="quantile", index="date", values="mean_forward_return_20").sort(
        "date"
    )
    spread = (monthly.get_column(str(quantile_count)) - monthly.get_column("1")).mean()
    group_means = (
        selected.group_by("quantile").agg(pl.col("mean_forward_return_20").mean()).sort("quantile")
    )
    values = group_means.get_column("mean_forward_return_20").to_numpy()
    monotonicity = None
    if len(values) == quantile_count and np.ptp(values) > 0.0:
        monotonicity = float(stats.spearmanr(np.arange(1, quantile_count + 1), values).statistic)
    return spread, monotonicity


def mean_turnover(
    turnover: pl.DataFrame,
    factor_name: str,
    variant: str,
) -> float | None:
    """Average non-initial monthly Top-20% membership turnover."""
    values = (
        turnover.filter(
            (pl.col("factor_name") == factor_name) & (pl.col("score_variant") == variant)
        )
        .get_column("turnover")
        .drop_nulls()
    )
    return values.mean() if len(values) else None


def finite_score_count(panel: pl.DataFrame, variant: str) -> int:
    """Count finite scores without consulting forward-return labels."""
    return _finite_group(panel, variant).height


def _build_rank_ic(
    groups_by_factor: Mapping[str, Sequence[tuple[date, pl.DataFrame]]],
    definitions: Sequence[FactorDefinition],
    settings: FactorResearchSettings,
    variants_by_factor: Mapping[str, Sequence[str]],
) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for definition in definitions:
        for day, group in groups_by_factor.get(definition.name, ()):
            for variant in variants_by_factor[definition.name]:
                for horizon in settings.forward_horizons:
                    scores, labels = _finite_pairs(group, variant, f"forward_return_{horizon}")
                    rank_ic, reason = _rank_ic(scores, labels)
                    rows.append(
                        {
                            "date": day,
                            "factor_name": definition.name,
                            "family": definition.family,
                            "score_variant": variant,
                            "horizon": horizon,
                            "n_obs": len(scores),
                            "rank_ic": rank_ic,
                            "reason": reason,
                        }
                    )
    return _dataframe(rows, RANK_IC_SCHEMA).sort("date", "factor_name", "score_variant", "horizon")


def _rank_ic(scores: np.ndarray, labels: np.ndarray) -> tuple[float | None, str | None]:
    if len(scores) < MINIMUM_CROSS_SECTION:
        return None, "insufficient_observations"
    if np.ptp(scores) == 0.0:
        return None, "constant_score"
    if np.ptp(labels) == 0.0:
        return None, "constant_forward_return"
    correlation = float(stats.spearmanr(scores, labels).statistic)
    if not np.isfinite(correlation):
        return None, "undefined_rank_correlation"
    return correlation, None


def _build_quantile_returns(
    groups_by_factor: Mapping[str, Sequence[tuple[date, pl.DataFrame]]],
    definitions: Sequence[FactorDefinition],
    settings: FactorResearchSettings,
    variants_by_factor: Mapping[str, Sequence[str]],
) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    label = f"forward_return_{settings.primary_horizon}"
    for definition in definitions:
        for day, group in groups_by_factor.get(definition.name, ()):
            for variant in variants_by_factor[definition.name]:
                valid = _finite_group(group, variant, label)
                if valid.height < MINIMUM_CROSS_SECTION:
                    continue
                ordered = valid.sort(variant, "symbol")
                quantiles = [
                    index * settings.quantile_count // ordered.height + 1
                    for index in range(ordered.height)
                ]
                assigned = ordered.with_columns(pl.Series("quantile", quantiles, dtype=pl.Int64))
                rows.extend(
                    _quantile_rows(
                        assigned,
                        definition,
                        variant,
                        day,
                        label,
                        settings.quantile_count,
                    )
                )
    return _dataframe(rows, QUANTILE_SCHEMA).sort(
        "date", "factor_name", "score_variant", "quantile"
    )


def _quantile_rows(
    assigned: pl.DataFrame,
    definition: FactorDefinition,
    variant: str,
    day: date,
    label: str,
    quantile_count: int,
) -> list[dict[str, object]]:
    rows = []
    for quantile in range(1, quantile_count + 1):
        members = assigned.filter(pl.col("quantile") == quantile)
        rows.append(
            {
                "date": day,
                "factor_name": definition.name,
                "family": definition.family,
                "score_variant": variant,
                "quantile": quantile,
                "n_obs": members.height,
                "mean_forward_return_20": members.get_column(label).mean(),
            }
        )
    return rows


def _build_turnover(
    groups_by_factor: Mapping[str, Sequence[tuple[date, pl.DataFrame]]],
    definitions: Sequence[FactorDefinition],
    variants_by_factor: Mapping[str, Sequence[str]],
) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for definition in definitions:
        for variant in variants_by_factor[definition.name]:
            previous_date: date | None = None
            previous_top: set[str] | None = None
            for day, group in groups_by_factor.get(definition.name, ()):
                valid = _finite_group(group, variant).sort(
                    [variant, "symbol"], descending=[True, False]
                )
                count = math.ceil(0.2 * valid.height) if valid.height else 0
                top = set(valid.head(count).get_column("symbol").to_list()) if count else set()
                rows.append(
                    {
                        "date": day,
                        "factor_name": definition.name,
                        "family": definition.family,
                        "score_variant": variant,
                        "previous_date": previous_date,
                        "top_count": len(top),
                        "previous_top_count": (
                            len(previous_top) if previous_top is not None else None
                        ),
                        "turnover": _set_turnover(top, previous_top),
                    }
                )
                previous_date, previous_top = day, top
    return _dataframe(rows, TURNOVER_SCHEMA).sort("date", "factor_name", "score_variant")


def _set_turnover(current: set[str], previous: set[str] | None) -> float | None:
    if previous is None:
        return None
    current_weight = 1.0 / len(current) if current else 0.0
    previous_weight = 1.0 / len(previous) if previous else 0.0
    distance = sum(
        abs(
            (current_weight if symbol in current else 0.0)
            - (previous_weight if symbol in previous else 0.0)
        )
        for symbol in current | previous
    )
    return min(1.0, max(0.0, 0.5 * distance))


def _build_subperiod_metrics(
    rank_ic: pl.DataFrame,
    definitions: Sequence[FactorDefinition],
    variants_by_factor: Mapping[str, Sequence[str]],
    primary_horizon: int,
) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for definition in definitions:
        for variant in variants_by_factor[definition.name]:
            primary = rank_ic.filter(
                (pl.col("factor_name") == definition.name)
                & (pl.col("score_variant") == variant)
                & (pl.col("horizon") == primary_horizon)
                & pl.col("rank_ic").is_not_null()
            )
            for name, start, end in SUBPERIODS:
                values = primary.filter(
                    (pl.col("date") >= start) & (pl.col("date") <= end)
                ).get_column("rank_ic")
                rows.append(
                    {
                        "factor_name": definition.name,
                        "family": definition.family,
                        "score_variant": variant,
                        "subperiod": name,
                        "start": start,
                        "end": end,
                        "valid_months": len(values),
                        "mean_ic": values.mean() if len(values) else None,
                    }
                )
    return _dataframe(rows, SUBPERIOD_SCHEMA).sort("factor_name", "score_variant", "start")


def _partition_panel(panel: pl.DataFrame) -> dict[str, list[tuple[date, pl.DataFrame]]]:
    groups_by_factor: dict[str, list[tuple[date, pl.DataFrame]]] = {}
    for group in panel.sort("factor_name", "date", "symbol").partition_by(
        ["factor_name", "date"], maintain_order=True
    ):
        factor_name = group.item(0, "factor_name")
        day = group.item(0, "date")
        groups_by_factor.setdefault(factor_name, []).append((day, group))
    return groups_by_factor


def _finite_pairs(group: pl.DataFrame, left: str, right: str) -> tuple[np.ndarray, np.ndarray]:
    values = group.select(left, right).to_numpy()
    if values.size == 0:
        return np.asarray([], dtype=float), np.asarray([], dtype=float)
    left_values = values[:, 0].astype(float)
    right_values = values[:, 1].astype(float)
    valid = np.isfinite(left_values) & np.isfinite(right_values)
    return left_values[valid], right_values[valid]


def _finite_group(group: pl.DataFrame, *columns: str) -> pl.DataFrame:
    condition = pl.lit(True)
    for column in columns:
        condition &= pl.col(column).is_not_null() & pl.col(column).is_finite()
    return group.filter(condition)


def _dataframe(rows: list[dict[str, object]], schema: dict[str, pl.DataType]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=schema, strict=False)
