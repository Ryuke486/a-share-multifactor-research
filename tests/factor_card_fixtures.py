from collections.abc import Sequence
from dataclasses import replace
from datetime import date

import polars as pl

from ashare_multifactor.config import FactorResearchSettings
from ashare_multifactor.factors.definitions import FactorDefinition
from ashare_multifactor.research.factor_evaluation import (
    FactorEvaluationBundle,
    evaluate_factors,
)
from ashare_multifactor.research.factor_redundancy import FactorRedundancyBundle
from ashare_multifactor.research.factor_redundancy import analyze_factor_redundancy
from ashare_multifactor.research.factor_selection import classify_factors


SUMMARY_SCHEMA = {
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


def factor_settings(**overrides: object) -> FactorResearchSettings:
    settings = FactorResearchSettings(
        data_start=date(2003, 1, 1),
        analysis_start=date(2005, 1, 1),
        analysis_end=date(2016, 12, 31),
        universe_size=1_000,
        minimum_history=252,
        liquidity_lookback=20,
        require_valid_trade_observation=True,
        signal_frequency="month_end",
        forward_horizons=(5, 20, 60),
        primary_horizon=20,
        winsor_lower=0.01,
        winsor_upper=0.99,
        quantile_count=5,
        minimum_coverage=0.8,
        minimum_valid_months=120,
        fdr_q_threshold=0.1,
        redundancy_threshold=0.7,
    )
    return replace(settings, **overrides)


def score_variants(definition: FactorDefinition) -> tuple[str, ...]:
    return ("score", "score_size_neutral") if definition.size_neutralize else ("score",)


def primary_variant(definition: FactorDefinition) -> str:
    return "score_size_neutral" if definition.size_neutralize else "score"


def complete_empty_card_inputs(
    definitions: Sequence[FactorDefinition],
    settings: FactorResearchSettings | None = None,
) -> tuple[FactorEvaluationBundle, pl.DataFrame, FactorRedundancyBundle]:
    settings = settings or factor_settings()
    definitions = tuple(definitions)
    evaluation_panel = pl.DataFrame(
        schema={
            "date": pl.Date,
            "symbol": pl.String,
            "factor_name": pl.String,
            "family": pl.String,
            "score": pl.Float64,
            "score_size_neutral": pl.Float64,
            "point_in_time_status": pl.String,
            **{f"forward_return_{horizon}": pl.Float64 for horizon in settings.forward_horizons},
        }
    )
    evaluation = evaluate_factors(evaluation_panel, definitions, settings)
    classifications = classify_factors(
        evaluation.factor_summary,
        evaluation.subperiod_metrics,
        definitions,
        settings,
    )
    factor_panel = evaluation_panel.select(
        "date",
        "symbol",
        "factor_name",
        "family",
        "score",
        "score_size_neutral",
    )
    redundancy = analyze_factor_redundancy(factor_panel, definitions, settings)
    return (
        evaluation,
        classifications,
        redundancy,
    )


def replace_keyed_rows(
    base: pl.DataFrame,
    replacements: pl.DataFrame,
    keys: Sequence[str],
) -> pl.DataFrame:
    replacement_keys = replacements.select(keys)
    retained = base.join(replacement_keys, on=keys, how="anti")
    return pl.concat([retained, replacements], how="vertical").sort(keys)


def set_correlation_pair(
    redundancy: FactorRedundancyBundle,
    left: FactorDefinition,
    right: FactorDefinition,
    *,
    mean_correlation: float,
    common_months: int,
) -> FactorRedundancyBundle:
    pair = ((pl.col("factor_a") == left.name) & (pl.col("factor_b") == right.name)) | (
        (pl.col("factor_a") == right.name) & (pl.col("factor_b") == left.name)
    )
    correlations = redundancy.factor_correlations.with_columns(
        pl.when(pair)
        .then(pl.lit(mean_correlation))
        .otherwise(pl.col("mean_correlation"))
        .alias("mean_correlation"),
        pl.when(pair)
        .then(pl.lit(common_months))
        .otherwise(pl.col("common_months"))
        .alias("common_months"),
        pl.when(pair)
        .then(pl.lit(None, dtype=pl.String))
        .otherwise(pl.col("reason"))
        .alias("reason"),
    )
    return replace(redundancy, factor_correlations=correlations)


def set_redundancy_flag(
    redundancy: FactorRedundancyBundle,
    left: FactorDefinition,
    right: FactorDefinition,
    *,
    mean_correlation: float,
    common_months: int,
) -> FactorRedundancyBundle:
    row = pl.DataFrame(
        [
            {
                "factor_a": left.name,
                "factor_b": right.name,
                "primary_variant_a": primary_variant(left),
                "primary_variant_b": primary_variant(right),
                "mean_correlation": mean_correlation,
                "absolute_correlation": abs(mean_correlation),
                "correlation_direction": "positive" if mean_correlation >= 0.0 else "negative",
                "common_months": common_months,
            }
        ],
        schema=redundancy.redundancy_flags.schema,
    )
    retained = redundancy.redundancy_flags.filter(
        ~((pl.col("factor_a") == left.name) & (pl.col("factor_b") == right.name))
    )
    return replace(
        redundancy,
        redundancy_flags=pl.concat([retained, row], how="vertical"),
    )
