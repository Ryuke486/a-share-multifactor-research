"""Pre-write contract validation for factor-card machine inputs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math

import polars as pl

from ashare_multifactor.config import FactorResearchSettings
from ashare_multifactor.factors.definitions import FactorDefinition
from ashare_multifactor.research.factor_evaluation import FactorEvaluationBundle
from ashare_multifactor.research.factor_metrics import (
    QUANTILE_SCHEMA,
    RANK_IC_SCHEMA,
    SUBPERIODS,
    SUBPERIOD_SCHEMA,
    TURNOVER_SCHEMA,
)
from ashare_multifactor.research.factor_redundancy import FactorRedundancyBundle


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
_CLASSIFICATION_SCHEMA = {
    "factor_name": pl.String,
    "family": pl.String,
    "primary_score_variant": pl.String,
    "primary_horizon": pl.Int64,
    "coverage": pl.Float64,
    "valid_months": pl.Int64,
    "mean_ic": pl.Float64,
    "bh_q": pl.Float64,
    "q5_q1": pl.Float64,
    "positive_subperiods": pl.Int64,
    "point_in_time_status": pl.String,
    "coverage_pass": pl.Boolean,
    "valid_months_pass": pl.Boolean,
    "mean_ic_positive": pl.Boolean,
    "fdr_pass": pl.Boolean,
    "q5_q1_positive": pl.Boolean,
    "subperiod_stability_pass": pl.Boolean,
    "point_in_time_verified": pl.Boolean,
    "core_gates_pass": pl.Boolean,
    "candidate_metrics_pass": pl.Boolean,
    "classification": pl.String,
    "reason": pl.String,
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


def validate_factor_card_inputs(
    evaluation: FactorEvaluationBundle,
    classifications: pl.DataFrame,
    redundancy: FactorRedundancyBundle,
    definitions: Sequence[FactorDefinition],
    settings: FactorResearchSettings,
) -> None:
    """Reject malformed Task-8/9 tables before any report path is created."""
    definitions = tuple(definitions)
    names = [definition.name for definition in definitions]
    if len(names) != len(set(names)):
        raise ValueError("duplicate factor definitions are not allowed")
    by_name = {definition.name: definition for definition in definitions}
    variants = {name: _score_variants(definition) for name, definition in by_name.items()}

    _require_schema(evaluation.rank_ic, RANK_IC_SCHEMA, "rank_ic")
    _require_schema(evaluation.quantile_returns, QUANTILE_SCHEMA, "quantile_returns")
    _require_schema(evaluation.factor_turnover, TURNOVER_SCHEMA, "factor_turnover")
    _require_schema(evaluation.factor_summary, _SUMMARY_SCHEMA, "factor_summary")
    _require_schema(evaluation.subperiod_metrics, SUBPERIOD_SCHEMA, "subperiod_metrics")
    _require_schema(classifications, _CLASSIFICATION_SCHEMA, "classification")
    _require_schema(redundancy.factor_correlations, _CORRELATION_SCHEMA, "factor_correlations")
    _require_schema(redundancy.redundancy_flags, _FLAG_SCHEMA, "redundancy_flags")

    _validate_metric_rows(
        evaluation.rank_ic,
        "rank_ic",
        ("date", "factor_name", "score_variant", "horizon"),
        by_name,
        variants,
        settings,
        horizon_column="horizon",
        date_column="date",
    )
    _validate_metric_rows(
        evaluation.quantile_returns,
        "quantile_returns",
        ("date", "factor_name", "score_variant", "quantile"),
        by_name,
        variants,
        settings,
        date_column="date",
    )
    if not evaluation.quantile_returns.is_empty():
        invalid_quantiles = evaluation.quantile_returns.filter(
            pl.col("quantile").is_null()
            | (pl.col("quantile") < 1)
            | (pl.col("quantile") > settings.quantile_count)
        )
        if not invalid_quantiles.is_empty():
            raise ValueError("quantile_returns contains an invalid quantile")
    _validate_metric_rows(
        evaluation.factor_turnover,
        "factor_turnover",
        ("date", "factor_name", "score_variant"),
        by_name,
        variants,
        settings,
        date_column="date",
    )
    _validate_complete_summary(evaluation.factor_summary, definitions, variants, settings)
    _validate_complete_subperiods(evaluation.subperiod_metrics, definitions, variants)
    _validate_classifications(classifications, definitions, settings)
    _validate_correlations(redundancy.factor_correlations, definitions)
    _validate_flags(
        redundancy.redundancy_flags,
        redundancy.factor_correlations,
        definitions,
        settings,
    )


def _require_schema(
    frame: pl.DataFrame,
    expected: Mapping[str, pl.DataType],
    name: str,
) -> None:
    missing = [column for column in expected if column not in frame.columns]
    if missing:
        raise ValueError(f"{name} is missing columns: " + ", ".join(missing))
    wrong = [
        f"{column}={frame.schema[column]}"
        for column, dtype in expected.items()
        if frame.schema[column] != dtype
    ]
    if wrong:
        raise ValueError(f"{name} has invalid dtypes: " + ", ".join(wrong))


def _validate_metric_rows(
    frame: pl.DataFrame,
    name: str,
    keys: Sequence[str],
    definitions: Mapping[str, FactorDefinition],
    variants: Mapping[str, tuple[str, ...]],
    settings: FactorResearchSettings,
    *,
    horizon_column: str | None = None,
    date_column: str | None = None,
) -> None:
    _reject_duplicate_keys(frame, keys, name)
    _validate_factor_rows(frame, name, definitions, variants)
    if horizon_column is not None and not frame.is_empty():
        invalid = frame.filter(
            pl.col(horizon_column).is_null()
            | ~pl.col(horizon_column).is_in(settings.forward_horizons)
        )
        if not invalid.is_empty():
            raise ValueError(f"{name} contains an unknown horizon")
    if date_column is not None:
        _validate_dates(frame, date_column, name, settings)


def _validate_factor_rows(
    frame: pl.DataFrame,
    name: str,
    definitions: Mapping[str, FactorDefinition],
    variants: Mapping[str, tuple[str, ...]],
) -> None:
    for row in frame.select("factor_name", "family", "score_variant").iter_rows(named=True):
        factor_name = row["factor_name"]
        if factor_name not in definitions:
            raise ValueError(f"{name} contains an unknown factor")
        definition = definitions[factor_name]
        if row["family"] != definition.family:
            raise ValueError(f"{name} family does not match factor definitions")
        if row["score_variant"] not in variants[factor_name]:
            raise ValueError(f"{name} contains an unknown score variant")


def _validate_dates(
    frame: pl.DataFrame,
    column: str,
    name: str,
    settings: FactorResearchSettings,
) -> None:
    if frame.is_empty():
        return
    outside = frame.filter(
        pl.col(column).is_null()
        | (pl.col(column) < settings.analysis_start)
        | (pl.col(column) > settings.analysis_end)
    )
    if not outside.is_empty():
        raise ValueError(f"{name} dates must remain inside the analysis period")


def _validate_complete_summary(
    summary: pl.DataFrame,
    definitions: Sequence[FactorDefinition],
    variants: Mapping[str, tuple[str, ...]],
    settings: FactorResearchSettings,
) -> None:
    keys = ("factor_name", "score_variant", "horizon")
    _reject_duplicate_keys(summary, keys, "factor_summary")
    _validate_factor_rows(
        summary,
        "factor_summary",
        {definition.name: definition for definition in definitions},
        variants,
    )
    expected = {
        (definition.name, variant, horizon)
        for definition in definitions
        for variant in variants[definition.name]
        for horizon in settings.forward_horizons
    }
    actual = set(summary.select(keys).iter_rows())
    if actual != expected:
        raise ValueError("factor_summary is not complete for registered variants and horizons")


def _validate_complete_subperiods(
    frame: pl.DataFrame,
    definitions: Sequence[FactorDefinition],
    variants: Mapping[str, tuple[str, ...]],
) -> None:
    keys = ("factor_name", "score_variant", "subperiod")
    _reject_duplicate_keys(frame, keys, "subperiod_metrics")
    by_name = {definition.name: definition for definition in definitions}
    _validate_factor_rows(frame, "subperiod_metrics", by_name, variants)
    periods = {name: (start, end) for name, start, end in SUBPERIODS}
    expected = {
        (definition.name, variant, subperiod)
        for definition in definitions
        for variant in variants[definition.name]
        for subperiod in periods
    }
    actual = set(frame.select(keys).iter_rows())
    if actual != expected:
        raise ValueError("subperiod_metrics is not complete for registered variants and periods")
    for row in frame.select("subperiod", "start", "end").iter_rows(named=True):
        if periods[row["subperiod"]] != (row["start"], row["end"]):
            raise ValueError("subperiod_metrics dates do not match preregistered periods")


def _validate_classifications(
    frame: pl.DataFrame,
    definitions: Sequence[FactorDefinition],
    settings: FactorResearchSettings,
) -> None:
    _reject_duplicate_keys(frame, ("factor_name",), "classification")
    by_name = {definition.name: definition for definition in definitions}
    if set(frame.get_column("factor_name")) != set(by_name):
        raise ValueError("classification is not complete for factor definitions")
    for row in frame.iter_rows(named=True):
        definition = by_name[row["factor_name"]]
        if row["family"] != definition.family:
            raise ValueError("classification family does not match factor definitions")
        if row["primary_score_variant"] != _primary_variant(definition):
            raise ValueError("classification primary score variant does not match definition")
        if row["primary_horizon"] != settings.primary_horizon:
            raise ValueError("classification primary horizon does not match settings")


def _validate_correlations(
    frame: pl.DataFrame,
    definitions: Sequence[FactorDefinition],
) -> None:
    _reject_duplicate_keys(frame, ("factor_a", "factor_b"), "factor_correlations")
    by_name = {definition.name: definition for definition in definitions}
    expected = {(left.name, right.name) for left in definitions for right in definitions}
    actual = set(frame.select("factor_a", "factor_b").iter_rows())
    if actual != expected:
        raise ValueError("factor_correlations is not a complete ordered matrix")
    for row in frame.iter_rows(named=True):
        left = by_name[row["factor_a"]]
        right = by_name[row["factor_b"]]
        if row["primary_variant_a"] != _primary_variant(left) or row[
            "primary_variant_b"
        ] != _primary_variant(right):
            raise ValueError("factor_correlations primary variants do not match definitions")
    rows = {(row["factor_a"], row["factor_b"]): row for row in frame.iter_rows(named=True)}
    for left_index, left in enumerate(definitions):
        for right in definitions[left_index + 1 :]:
            forward = rows[(left.name, right.name)]
            reverse = rows[(right.name, left.name)]
            if (
                not _optional_float_equal(forward["mean_correlation"], reverse["mean_correlation"])
                or forward["common_months"] != reverse["common_months"]
                or forward["reason"] != reverse["reason"]
            ):
                raise ValueError("factor_correlations mirrored rows are inconsistent")


def _validate_flags(
    frame: pl.DataFrame,
    correlations: pl.DataFrame,
    definitions: Sequence[FactorDefinition],
    settings: FactorResearchSettings,
) -> None:
    _reject_duplicate_keys(frame, ("factor_a", "factor_b"), "redundancy_flags")
    by_name = {definition.name: definition for definition in definitions}
    order = {definition.name: index for index, definition in enumerate(definitions)}
    seen: set[frozenset[str]] = set()
    for row in frame.iter_rows(named=True):
        left_name = row["factor_a"]
        right_name = row["factor_b"]
        if left_name not in by_name or right_name not in by_name:
            raise ValueError("redundancy_flags contains an unknown factor")
        pair = frozenset((left_name, right_name))
        if left_name == right_name or pair in seen or order[left_name] >= order[right_name]:
            raise ValueError("redundancy_flags must contain unique unordered different pairs")
        seen.add(pair)
        if row["primary_variant_a"] != _primary_variant(by_name[left_name]) or row[
            "primary_variant_b"
        ] != _primary_variant(by_name[right_name]):
            raise ValueError("redundancy_flags primary variants do not match definitions")
    correlation_rows = {
        (row["factor_a"], row["factor_b"]): row for row in correlations.iter_rows(named=True)
    }
    expected = {
        (left.name, right.name): correlation_rows[(left.name, right.name)]
        for left_index, left in enumerate(definitions)
        for right in definitions[left_index + 1 :]
        if _is_expected_flag(
            correlation_rows[(left.name, right.name)]["mean_correlation"],
            settings.redundancy_threshold,
        )
    }
    actual = {(row["factor_a"], row["factor_b"]): row for row in frame.iter_rows(named=True)}
    if expected.keys() - actual.keys():
        raise ValueError("redundancy_flags is missing expected threshold pairs")
    if actual.keys() - expected.keys():
        raise ValueError("redundancy_flags contains unexpected below-threshold pairs")
    for pair, correlation in expected.items():
        flag = actual[pair]
        mean = float(correlation["mean_correlation"])
        expected_direction = "positive" if mean >= 0.0 else "negative"
        if (
            not _optional_float_equal(flag["mean_correlation"], mean)
            or not _optional_float_equal(flag["absolute_correlation"], abs(mean))
            or flag["correlation_direction"] != expected_direction
            or flag["common_months"] != correlation["common_months"]
            or flag["primary_variant_a"] != correlation["primary_variant_a"]
            or flag["primary_variant_b"] != correlation["primary_variant_b"]
        ):
            raise ValueError("redundancy_flags row does not match factor_correlations")


def _reject_duplicate_keys(frame: pl.DataFrame, keys: Sequence[str], name: str) -> None:
    if frame.select(keys).is_duplicated().any():
        raise ValueError(f"{name} contains duplicate keys")


def _score_variants(definition: FactorDefinition) -> tuple[str, ...]:
    return ("score", "score_size_neutral") if definition.size_neutralize else ("score",)


def _primary_variant(definition: FactorDefinition) -> str:
    return "score_size_neutral" if definition.size_neutralize else "score"


def _is_expected_flag(value: object, threshold: float) -> bool:
    return _finite_number(value) and abs(float(value)) >= threshold


def _optional_float_equal(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if not _finite_number(left) or not _finite_number(right):
        return False
    return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-12)


def _finite_number(value: object) -> bool:
    return value is not None and not isinstance(value, bool) and math.isfinite(float(value))
