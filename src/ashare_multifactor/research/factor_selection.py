"""Transparent candidate, watch, and reject rules for evaluated factors."""

from __future__ import annotations

import math

import polars as pl

from ashare_multifactor.config import FactorResearchSettings


_SUMMARY_COLUMNS = (
    "factor_name",
    "family",
    "score_variant",
    "horizon",
    "coverage",
    "valid_months",
    "mean_ic",
    "bh_q",
    "q5_q1",
    "point_in_time_status",
)
_SUBPERIOD_COLUMNS = ("factor_name", "score_variant", "subperiod", "mean_ic")
_PREREGISTERED_SUBPERIODS = frozenset({"2005-2008", "2009-2012", "2013-2016"})
_OUTPUT_SCHEMA = {
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


def classify_factors(
    factor_summary: pl.DataFrame,
    subperiod_metrics: pl.DataFrame,
    settings: FactorResearchSettings,
) -> pl.DataFrame:
    """Classify each factor from its preregistered primary test and subperiods."""
    _require_columns(factor_summary, _SUMMARY_COLUMNS, "factor_summary")
    _require_columns(subperiod_metrics, _SUBPERIOD_COLUMNS, "subperiod_metrics")
    primary = factor_summary.filter(pl.col("horizon") == settings.primary_horizon)
    rows = [
        _classify_row(
            _primary_row(primary, factor_name),
            subperiod_metrics,
            settings,
        )
        for factor_name in factor_summary.get_column("factor_name").unique().sort().to_list()
    ]
    return pl.DataFrame(rows, schema=_OUTPUT_SCHEMA, strict=False).sort("factor_name")


def _require_columns(frame: pl.DataFrame, columns: tuple[str, ...], name: str) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ValueError(f"{name} is missing columns: " + ", ".join(missing))


def _primary_row(primary: pl.DataFrame, factor_name: str) -> dict[str, object]:
    factor = primary.filter(pl.col("factor_name") == factor_name)
    variants = set(factor.get_column("score_variant"))
    primary_variant = "score_size_neutral" if "score_size_neutral" in variants else "score"
    selected = factor.filter(pl.col("score_variant") == primary_variant)
    if selected.height != 1:
        raise ValueError(
            f"factor {factor_name} must have exactly one {primary_variant} primary summary row"
        )
    return selected.row(0, named=True)


def _classify_row(
    primary: dict[str, object],
    subperiod_metrics: pl.DataFrame,
    settings: FactorResearchSettings,
) -> dict[str, object]:
    factor_name = str(primary["factor_name"])
    variant = str(primary["score_variant"])
    subperiods = subperiod_metrics.filter(
        (pl.col("factor_name") == factor_name)
        & (pl.col("score_variant") == variant)
        & pl.col("subperiod").is_in(_PREREGISTERED_SUBPERIODS)
    )
    if subperiods.select("subperiod").is_duplicated().any():
        raise ValueError(f"factor {factor_name} has duplicate subperiod metrics")
    positive_subperiods = sum(
        _finite(mean_ic) and float(mean_ic) > 0.0 for mean_ic in subperiods.get_column("mean_ic")
    )

    coverage_pass = _finite(primary["coverage"]) and (
        float(primary["coverage"]) >= settings.minimum_coverage
    )
    valid_months_pass = primary["valid_months"] is not None and (
        int(primary["valid_months"]) >= settings.minimum_valid_months
    )
    mean_ic_positive = _finite(primary["mean_ic"]) and float(primary["mean_ic"]) > 0.0
    fdr_pass = _finite(primary["bh_q"]) and (float(primary["bh_q"]) <= settings.fdr_q_threshold)
    q5_q1_positive = _finite(primary["q5_q1"]) and float(primary["q5_q1"]) > 0.0
    stability_pass = positive_subperiods >= 2
    point_in_time_verified = (
        primary["point_in_time_status"] is not None
        and primary["point_in_time_status"] != "unverified"
    )
    core_pass = coverage_pass and valid_months_pass and mean_ic_positive
    candidate_metrics_pass = fdr_pass and q5_q1_positive and stability_pass
    classification, reason = _classification(
        coverage_pass=coverage_pass,
        valid_months_pass=valid_months_pass,
        mean_ic_positive=mean_ic_positive,
        fdr_pass=fdr_pass,
        q5_q1_positive=q5_q1_positive,
        stability_pass=stability_pass,
        point_in_time_verified=point_in_time_verified,
    )
    return {
        "factor_name": factor_name,
        "family": primary["family"],
        "primary_score_variant": variant,
        "primary_horizon": primary["horizon"],
        "coverage": primary["coverage"],
        "valid_months": primary["valid_months"],
        "mean_ic": primary["mean_ic"],
        "bh_q": primary["bh_q"],
        "q5_q1": primary["q5_q1"],
        "positive_subperiods": positive_subperiods,
        "point_in_time_status": primary["point_in_time_status"],
        "coverage_pass": coverage_pass,
        "valid_months_pass": valid_months_pass,
        "mean_ic_positive": mean_ic_positive,
        "fdr_pass": fdr_pass,
        "q5_q1_positive": q5_q1_positive,
        "subperiod_stability_pass": stability_pass,
        "point_in_time_verified": point_in_time_verified,
        "core_gates_pass": core_pass,
        "candidate_metrics_pass": candidate_metrics_pass,
        "classification": classification,
        "reason": reason,
    }


def _classification(
    *,
    coverage_pass: bool,
    valid_months_pass: bool,
    mean_ic_positive: bool,
    fdr_pass: bool,
    q5_q1_positive: bool,
    stability_pass: bool,
    point_in_time_verified: bool,
) -> tuple[str, str]:
    reject_reasons = []
    if not coverage_pass:
        reject_reasons.append("coverage_below_or_missing_threshold")
    if not valid_months_pass:
        reject_reasons.append("insufficient_valid_months")
    if not mean_ic_positive:
        reject_reasons.append("non_positive_or_missing_mean_ic")
    if reject_reasons:
        return "reject", "reject:" + ";".join(reject_reasons)

    watch_reasons = []
    if not fdr_pass:
        watch_reasons.append("fdr_threshold_not_met")
    if not q5_q1_positive:
        watch_reasons.append("non_positive_or_missing_q5_q1")
    if not stability_pass:
        watch_reasons.append("subperiod_stability_not_met")
    if not point_in_time_verified:
        watch_reasons.append("point_in_time_unverified")
    if watch_reasons:
        return "watch", "watch:" + ";".join(watch_reasons)
    return "candidate", "candidate:all_thresholds_passed"


def _finite(value: object) -> bool:
    return value is not None and not isinstance(value, bool) and math.isfinite(float(value))
