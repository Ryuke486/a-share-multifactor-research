"""Monthly cross-sectional factor correlations and non-destructive redundancy flags."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np
import polars as pl
from scipy import stats

from ashare_multifactor.config import FactorResearchSettings
from ashare_multifactor.factors.definitions import FactorDefinition
from ashare_multifactor.research.factor_metrics import MINIMUM_CROSS_SECTION


_REQUIRED_SCHEMA = {
    "date": pl.Date,
    "symbol": pl.String,
    "factor_name": pl.String,
    "family": pl.String,
    "score": pl.Float64,
    "score_size_neutral": pl.Float64,
}
_CORRELATION_SCHEMA = {
    "factor_a": pl.String,
    "factor_b": pl.String,
    "primary_variant_a": pl.String,
    "primary_variant_b": pl.String,
    "mean_correlation": pl.Float64,
    "common_months": pl.Int64,
    "reason": pl.String,
}
_FLAG_SCHEMA = {
    "factor_a": pl.String,
    "factor_b": pl.String,
    "primary_variant_a": pl.String,
    "primary_variant_b": pl.String,
    "mean_correlation": pl.Float64,
    "absolute_correlation": pl.Float64,
    "correlation_direction": pl.String,
    "common_months": pl.Int64,
}


@dataclass(frozen=True)
class FactorRedundancyBundle:
    factor_correlations: pl.DataFrame
    redundancy_flags: pl.DataFrame


def analyze_factor_redundancy(
    panel: pl.DataFrame,
    definitions: Sequence[FactorDefinition],
    settings: FactorResearchSettings,
) -> FactorRedundancyBundle:
    """Average monthly Spearman correlations using each factor's registered variant."""
    definitions = tuple(definitions)
    _validate_inputs(panel, definitions, settings)
    monthly = _partition_primary_scores(panel, definitions)
    pair_results: dict[tuple[str, str], tuple[float | None, int, str | None]] = {}
    for left_index, left in enumerate(definitions):
        for right in definitions[left_index:]:
            result = _pair_summary(monthly.get(left.name, {}), monthly.get(right.name, {}))
            pair_results[(left.name, right.name)] = result
            pair_results[(right.name, left.name)] = result

    rows = []
    for left in definitions:
        for right in definitions:
            mean, common_months, reason = pair_results[(left.name, right.name)]
            rows.append(
                {
                    "factor_a": left.name,
                    "factor_b": right.name,
                    "primary_variant_a": _primary_variant(left),
                    "primary_variant_b": _primary_variant(right),
                    "mean_correlation": mean,
                    "common_months": common_months,
                    "reason": reason,
                }
            )
    correlations = pl.DataFrame(rows, schema=_CORRELATION_SCHEMA, strict=False)
    flags = _redundancy_flags(correlations, definitions, settings.redundancy_threshold)
    return FactorRedundancyBundle(correlations, flags)


def _validate_inputs(
    panel: pl.DataFrame,
    definitions: Sequence[FactorDefinition],
    settings: FactorResearchSettings,
) -> None:
    definition_names = [definition.name for definition in definitions]
    if len(definition_names) != len(set(definition_names)):
        raise ValueError("duplicate factor definitions are not allowed")
    label_columns = [column for column in panel.columns if column.startswith("forward_")]
    if label_columns:
        raise ValueError("factor redundancy input must not contain forward-return labels")
    missing = [column for column in _REQUIRED_SCHEMA if column not in panel.columns]
    if missing:
        raise ValueError("factor redundancy panel is missing columns: " + ", ".join(missing))
    wrong_types = [
        f"{column}={panel.schema[column]}"
        for column, dtype in _REQUIRED_SCHEMA.items()
        if panel.schema[column] != dtype
    ]
    if wrong_types:
        raise ValueError("factor redundancy panel has invalid schema: " + ", ".join(wrong_types))
    keys = panel.select("date", "symbol", "factor_name")
    if any(keys.get_column(column).null_count() for column in keys.columns):
        raise ValueError("factor redundancy keys must be non-null")
    if panel.filter(pl.col("symbol").str.strip_chars() == "").height:
        raise ValueError("factor redundancy symbol must be non-blank")
    if panel.filter(pl.col("factor_name").str.strip_chars() == "").height:
        raise ValueError("factor redundancy factor_name must be non-blank")
    if panel.select("date", "symbol", "factor_name").is_duplicated().any():
        raise ValueError("duplicate date, symbol, and factor_name keys are not allowed")
    if panel.is_empty():
        return
    outside = panel.filter(
        (pl.col("date") < settings.analysis_start) | (pl.col("date") > settings.analysis_end)
    )
    if not outside.is_empty():
        raise ValueError("factor redundancy dates must remain inside the analysis period")
    definitions_by_name = {definition.name: definition for definition in definitions}
    panel_names = set(panel.get_column("factor_name").unique().to_list())
    unknown = sorted(panel_names - definitions_by_name.keys())
    if unknown:
        raise ValueError("panel contains unknown factor definitions: " + ", ".join(unknown))
    expected_families = panel.get_column("factor_name").replace_strict(
        {name: definition.family for name, definition in definitions_by_name.items()}
    )
    if panel.get_column("family").ne_missing(expected_families).any():
        raise ValueError("panel factor families do not match factor definitions")
    signal_dates = (
        panel.select("factor_name", "date")
        .unique()
        .with_columns(pl.col("date").dt.truncate("1mo").alias("month"))
        .group_by("factor_name", "month")
        .agg(pl.col("date").n_unique().alias("signal_dates"))
    )
    if not signal_dates.filter(pl.col("signal_dates") > 1).is_empty():
        raise ValueError("each factor must have one signal date per natural month")


def _partition_primary_scores(
    panel: pl.DataFrame,
    definitions: Sequence[FactorDefinition],
) -> dict[str, dict[date, pl.DataFrame]]:
    neutral_names = [definition.name for definition in definitions if definition.size_neutralize]
    primary = panel.select(
        "date",
        "symbol",
        "factor_name",
        pl.when(pl.col("factor_name").is_in(neutral_names))
        .then(pl.col("score_size_neutral"))
        .otherwise(pl.col("score"))
        .alias("primary_score"),
    )
    result: dict[str, dict[date, pl.DataFrame]] = {}
    for group in primary.sort("factor_name", "date", "symbol").partition_by(
        ["factor_name", "date"], maintain_order=True
    ):
        factor_name = group.item(0, "factor_name")
        day = group.item(0, "date")
        result.setdefault(factor_name, {})[day] = group.select("symbol", "primary_score")
    return result


def _pair_summary(
    left: dict[date, pl.DataFrame],
    right: dict[date, pl.DataFrame],
) -> tuple[float | None, int, str | None]:
    common_dates = sorted(left.keys() & right.keys())
    if not common_dates:
        return None, 0, "no_common_dates"
    correlations: list[float] = []
    invalid_reasons: Counter[str] = Counter()
    for day in common_dates:
        correlation, reason = _monthly_correlation(left[day], right[day])
        if correlation is None:
            invalid_reasons[reason or "undefined_rank_correlation"] += 1
        else:
            correlations.append(correlation)
    if correlations:
        return float(np.mean(correlations)), len(correlations), None
    reason_text = ",".join(
        reason if count == 1 else f"{reason}={count}"
        for reason, count in sorted(invalid_reasons.items())
    )
    return None, 0, f"no_valid_common_months:{reason_text}"


def _monthly_correlation(
    left: pl.DataFrame,
    right: pl.DataFrame,
) -> tuple[float | None, str | None]:
    common = left.join(right, on="symbol", how="inner", suffix="_right").filter(
        pl.col("primary_score").is_not_null()
        & pl.col("primary_score").is_finite()
        & pl.col("primary_score_right").is_not_null()
        & pl.col("primary_score_right").is_finite()
    )
    if common.height < MINIMUM_CROSS_SECTION:
        return None, "insufficient_common_observations"
    left_values = common.get_column("primary_score").to_numpy()
    right_values = common.get_column("primary_score_right").to_numpy()
    if np.ptp(left_values) == 0.0 or np.ptp(right_values) == 0.0:
        return None, "constant_score"
    correlation = float(stats.spearmanr(left_values, right_values).statistic)
    if not np.isfinite(correlation):
        return None, "undefined_rank_correlation"
    return correlation, None


def _redundancy_flags(
    correlations: pl.DataFrame,
    definitions: Sequence[FactorDefinition],
    threshold: float,
) -> pl.DataFrame:
    rows = []
    for left_index, left in enumerate(definitions):
        for right in definitions[left_index + 1 :]:
            pair = correlations.filter(
                (pl.col("factor_a") == left.name) & (pl.col("factor_b") == right.name)
            ).row(0, named=True)
            mean = pair["mean_correlation"]
            if mean is None or abs(float(mean)) < threshold:
                continue
            rows.append(
                {
                    "factor_a": left.name,
                    "factor_b": right.name,
                    "primary_variant_a": pair["primary_variant_a"],
                    "primary_variant_b": pair["primary_variant_b"],
                    "mean_correlation": mean,
                    "absolute_correlation": abs(float(mean)),
                    "correlation_direction": "positive" if float(mean) >= 0.0 else "negative",
                    "common_months": pair["common_months"],
                }
            )
    return pl.DataFrame(rows, schema=_FLAG_SCHEMA, strict=False)


def _primary_variant(definition: FactorDefinition) -> str:
    return "score_size_neutral" if definition.size_neutralize else "score"
