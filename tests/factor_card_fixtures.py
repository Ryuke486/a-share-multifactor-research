from collections.abc import Sequence
from dataclasses import replace
from datetime import date

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
CLASSIFICATION_SCHEMA = {
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
CORRELATION_SCHEMA = {
    "factor_a": pl.String,
    "factor_b": pl.String,
    "primary_variant_a": pl.String,
    "primary_variant_b": pl.String,
    "mean_correlation": pl.Float64,
    "common_months": pl.Int64,
    "reason": pl.String,
}
FLAG_SCHEMA = {
    "factor_a": pl.String,
    "factor_b": pl.String,
    "primary_variant_a": pl.String,
    "primary_variant_b": pl.String,
    "mean_correlation": pl.Float64,
    "absolute_correlation": pl.Float64,
    "correlation_direction": pl.String,
    "common_months": pl.Int64,
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
    summary_rows = []
    subperiod_rows = []
    classification_rows = []
    for definition in definitions:
        status = "unverified" if definition.requires_verified_pit else "ready"
        for variant in score_variants(definition):
            for horizon in settings.forward_horizons:
                summary_rows.append(
                    {
                        "factor_name": definition.name,
                        "family": definition.family,
                        "score_variant": variant,
                        "horizon": horizon,
                        "valid_months": 0,
                        "point_in_time_status": status,
                        "nw_reason": "empty_sequence",
                        "summary_reason": "no_eligible_rows",
                    }
                )
            for subperiod, start, end in SUBPERIODS:
                subperiod_rows.append(
                    {
                        "factor_name": definition.name,
                        "family": definition.family,
                        "score_variant": variant,
                        "subperiod": subperiod,
                        "start": start,
                        "end": end,
                        "valid_months": 0,
                        "mean_ic": None,
                    }
                )
        classification_rows.append(
            {
                "factor_name": definition.name,
                "family": definition.family,
                "primary_score_variant": primary_variant(definition),
                "primary_horizon": settings.primary_horizon,
                "valid_months": 0,
                "positive_subperiods": 0,
                "point_in_time_status": status,
                "coverage_pass": False,
                "valid_months_pass": False,
                "mean_ic_positive": False,
                "fdr_pass": False,
                "q5_q1_positive": False,
                "subperiod_stability_pass": False,
                "point_in_time_verified": status == "ready",
                "core_gates_pass": False,
                "candidate_metrics_pass": False,
                "classification": "reject",
                "reason": "reject:no_eligible_rows",
            }
        )
    correlations = [
        {
            "factor_a": left.name,
            "factor_b": right.name,
            "primary_variant_a": primary_variant(left),
            "primary_variant_b": primary_variant(right),
            "common_months": 0,
            "reason": "no_common_dates",
        }
        for left in definitions
        for right in definitions
    ]
    evaluation = FactorEvaluationBundle(
        rank_ic=pl.DataFrame(schema=RANK_IC_SCHEMA),
        quantile_returns=pl.DataFrame(schema=QUANTILE_SCHEMA),
        subperiod_metrics=pl.DataFrame(
            subperiod_rows,
            schema=SUBPERIOD_SCHEMA,
            strict=False,
        ),
        factor_turnover=pl.DataFrame(schema=TURNOVER_SCHEMA),
        factor_summary=pl.DataFrame(summary_rows, schema=SUMMARY_SCHEMA, strict=False),
    )
    return (
        evaluation,
        pl.DataFrame(classification_rows, schema=CLASSIFICATION_SCHEMA, strict=False),
        FactorRedundancyBundle(
            factor_correlations=pl.DataFrame(
                correlations,
                schema=CORRELATION_SCHEMA,
                strict=False,
            ),
            redundancy_flags=pl.DataFrame(schema=FLAG_SCHEMA),
        ),
    )


def replace_keyed_rows(
    base: pl.DataFrame,
    replacements: pl.DataFrame,
    keys: Sequence[str],
) -> pl.DataFrame:
    replacement_keys = replacements.select(keys)
    retained = base.join(replacement_keys, on=keys, how="anti")
    return pl.concat([retained, replacements], how="vertical").sort(keys)
