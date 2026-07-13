"""Validated orchestration and statistical summary of single-factor metrics."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import math

import numpy as np
import polars as pl

from ashare_multifactor.config import FactorResearchSettings
from ashare_multifactor.factors.definitions import FactorDefinition
from ashare_multifactor.research.factor_metrics import (
    FactorMetricsBundle,
    build_factor_metrics,
    finite_score_count,
    mean_turnover,
    quantile_metrics,
)
from ashare_multifactor.research.factor_statistics import (
    benjamini_hochberg,
    newey_west_mean_test,
)


_BASE_SCHEMA = {
    "date": pl.Date,
    "symbol": pl.String,
    "factor_name": pl.String,
    "family": pl.String,
    "score": pl.Float64,
    "score_size_neutral": pl.Float64,
    "point_in_time_status": pl.String,
}
_SUMMARY_SCHEMA = {
    "factor_name": pl.String,
    "family": pl.String,
    "score_variant": pl.String,
    "horizon": pl.Int64,
    "coverage": pl.Float64,
    "valid_months": pl.Int64,
    "mean_ic": pl.Float64,
    "ic_std": pl.Float64,
    "icir": pl.Float64,
    "positive_ic_rate": pl.Float64,
    "nw_lag": pl.Int64,
    "nw_se": pl.Float64,
    "nw_t": pl.Float64,
    "nw_p": pl.Float64,
    "bh_q": pl.Float64,
    "q5_q1": pl.Float64,
    "monotonicity": pl.Float64,
    "avg_turnover": pl.Float64,
    "point_in_time_status": pl.String,
    "nw_reason": pl.String,
    "summary_reason": pl.String,
}


@dataclass(frozen=True)
class FactorEvaluationBundle:
    rank_ic: pl.DataFrame
    quantile_returns: pl.DataFrame
    subperiod_metrics: pl.DataFrame
    factor_turnover: pl.DataFrame
    factor_summary: pl.DataFrame


def evaluate_factors(
    panel: pl.DataFrame,
    definitions: Sequence[FactorDefinition],
    settings: FactorResearchSettings,
) -> FactorEvaluationBundle:
    """Evaluate a keyed panel after the orchestration layer has joined labels."""
    definitions = tuple(definitions)
    _validate_inputs(panel, definitions, settings)
    variants_by_factor = {
        definition.name: _score_variants(definition) for definition in definitions
    }
    metrics = build_factor_metrics(panel, definitions, settings, variants_by_factor)
    summary = _build_summary(panel, metrics, definitions, settings, variants_by_factor)
    return FactorEvaluationBundle(
        rank_ic=metrics.rank_ic,
        quantile_returns=metrics.quantile_returns,
        subperiod_metrics=metrics.subperiod_metrics,
        factor_turnover=metrics.factor_turnover,
        factor_summary=summary,
    )


def _validate_inputs(
    panel: pl.DataFrame,
    definitions: Sequence[FactorDefinition],
    settings: FactorResearchSettings,
) -> None:
    definition_names = [definition.name for definition in definitions]
    if len(definition_names) != len(set(definition_names)):
        raise ValueError("duplicate factor definitions are not allowed")
    required_schema = {
        **_BASE_SCHEMA,
        **{f"forward_return_{horizon}": pl.Float64 for horizon in settings.forward_horizons},
    }
    missing = [column for column in required_schema if column not in panel.columns]
    if missing:
        raise ValueError("factor evaluation panel is missing columns: " + ", ".join(missing))
    wrong_types = [
        f"{column}={panel.schema[column]}"
        for column, dtype in required_schema.items()
        if panel.schema[column] != dtype
    ]
    if wrong_types:
        raise ValueError("factor evaluation panel has invalid schema: " + ", ".join(wrong_types))
    if panel.select("date", "symbol", "factor_name").is_duplicated().any():
        raise ValueError("duplicate date, symbol, and factor_name keys are not allowed")
    if panel.is_empty():
        return
    if panel.get_column("date").null_count():
        raise ValueError("factor evaluation dates must be non-null")
    outside = panel.filter(
        (pl.col("date") < settings.analysis_start) | (pl.col("date") > settings.analysis_end)
    )
    if not outside.is_empty():
        raise ValueError("factor evaluation dates must remain inside the analysis period")
    definitions_by_name = {definition.name: definition for definition in definitions}
    panel_names = set(panel.get_column("factor_name").unique())
    unknown = sorted(panel_names - definitions_by_name.keys())
    if unknown:
        raise ValueError("panel contains unknown factor definitions: " + ", ".join(unknown))
    expected_family = panel.get_column("factor_name").replace_strict(
        {name: definition.family for name, definition in definitions_by_name.items()}
    )
    if panel.get_column("family").ne_missing(expected_family).any():
        raise ValueError("panel factor families do not match factor definitions")


def _score_variants(definition: FactorDefinition) -> tuple[str, ...]:
    if definition.size_neutralize:
        return ("score", "score_size_neutral")
    return ("score",)


def _build_summary(
    panel: pl.DataFrame,
    metrics: FactorMetricsBundle,
    definitions: Sequence[FactorDefinition],
    settings: FactorResearchSettings,
    variants_by_factor: dict[str, tuple[str, ...]],
) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for definition in definitions:
        factor_panel = panel.filter(pl.col("factor_name") == definition.name)
        status = _point_in_time_status(factor_panel, definition.name)
        for variant in variants_by_factor[definition.name]:
            coverage = _coverage(factor_panel, variant)
            q5_q1, monotonicity = quantile_metrics(
                metrics.quantile_returns,
                definition.name,
                variant,
                settings.quantile_count,
            )
            avg_turnover = mean_turnover(metrics.factor_turnover, definition.name, variant)
            for horizon in settings.forward_horizons:
                is_primary_horizon = horizon == settings.primary_horizon
                rows.append(
                    _summary_row(
                        metrics.rank_ic,
                        definition,
                        variant,
                        horizon,
                        coverage,
                        q5_q1 if is_primary_horizon else None,
                        monotonicity if is_primary_horizon else None,
                        avg_turnover,
                        status,
                        factor_panel.height,
                    )
                )
    _apply_primary_bh(rows, definitions, settings.primary_horizon)
    return pl.DataFrame(rows, schema=_SUMMARY_SCHEMA, strict=False).sort(
        "factor_name", "score_variant", "horizon"
    )


def _point_in_time_status(panel: pl.DataFrame, factor_name: str) -> str | None:
    statuses = panel.get_column("point_in_time_status").drop_nulls().unique().to_list()
    if len(statuses) > 1:
        raise ValueError(f"factor {factor_name} has inconsistent point-in-time status")
    return statuses[0] if statuses else None


def _coverage(factor_panel: pl.DataFrame, variant: str) -> float | None:
    if factor_panel.is_empty():
        return None
    return finite_score_count(factor_panel, variant) / factor_panel.height


def _summary_row(
    rank_ic: pl.DataFrame,
    definition: FactorDefinition,
    variant: str,
    horizon: int,
    coverage: float | None,
    q5_q1: float | None,
    monotonicity: float | None,
    avg_turnover: float | None,
    point_in_time_status: str | None,
    eligible_rows: int,
) -> dict[str, object]:
    values = (
        rank_ic.filter(
            (pl.col("factor_name") == definition.name)
            & (pl.col("score_variant") == variant)
            & (pl.col("horizon") == horizon)
            & pl.col("rank_ic").is_not_null()
        )
        .get_column("rank_ic")
        .to_list()
    )
    mean_ic = float(np.mean(values)) if values else None
    ic_std = float(np.std(values, ddof=1)) if len(values) >= 2 else None
    icir = mean_ic / ic_std * math.sqrt(12.0) if ic_std and ic_std > 0.0 else None
    nw = newey_west_mean_test(values, horizon=horizon)
    return {
        "factor_name": definition.name,
        "family": definition.family,
        "score_variant": variant,
        "horizon": horizon,
        "coverage": coverage,
        "valid_months": len(values),
        "mean_ic": mean_ic,
        "ic_std": ic_std,
        "icir": icir,
        "positive_ic_rate": (
            sum(value > 0.0 for value in values) / len(values) if values else None
        ),
        "nw_lag": nw["lag"],
        "nw_se": nw["se"],
        "nw_t": nw["t"],
        "nw_p": nw["p"],
        "bh_q": None,
        "q5_q1": q5_q1,
        "monotonicity": monotonicity,
        "avg_turnover": avg_turnover,
        "point_in_time_status": point_in_time_status,
        "nw_reason": nw["reason"],
        "summary_reason": _summary_reason(eligible_rows, coverage, values),
    }


def _summary_reason(
    eligible_rows: int,
    coverage: float | None,
    ic_values: Sequence[float],
) -> str | None:
    if eligible_rows == 0:
        return "no_eligible_rows"
    if coverage == 0.0:
        return "no_finite_scores"
    if not ic_values:
        return "no_valid_monthly_ic"
    return None


def _apply_primary_bh(
    rows: list[dict[str, object]],
    definitions: Sequence[FactorDefinition],
    primary_horizon: int,
) -> None:
    definitions_by_name = {definition.name: definition for definition in definitions}
    primary_indices = []
    for index, row in enumerate(rows):
        definition = definitions_by_name[str(row["factor_name"])]
        primary_variant = "score_size_neutral" if definition.size_neutralize else "score"
        if row["horizon"] == primary_horizon and row["score_variant"] == primary_variant:
            primary_indices.append(index)
    q_values = benjamini_hochberg([rows[index]["nw_p"] for index in primary_indices])
    for index, q_value in zip(primary_indices, q_values):
        rows[index]["bh_q"] = q_value
