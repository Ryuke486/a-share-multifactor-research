from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from ashare_multifactor.config import FactorResearchSettings
from ashare_multifactor.data.field_audit import FieldReadiness
from ashare_multifactor.factors.definitions import FACTOR_DEFINITIONS
from ashare_multifactor.factors.panel import build_monthly_factor_panel
from ashare_multifactor.research.factor_preprocessing import preprocess_factor_panel
from ashare_multifactor.research.labels import add_forward_returns
from ashare_multifactor.validation.protocol import assert_validation_read_allowed


@dataclass(frozen=True)
class ValidationFactorExtension:
    features: pl.DataFrame
    forward_returns: pl.DataFrame
    panel: pl.DataFrame
    audit: pl.DataFrame


def build_validation_factor_extension(
    daily: pl.DataFrame,
    readiness: FieldReadiness,
    settings: FactorResearchSettings,
) -> ValidationFactorExtension:
    """Apply the frozen stage-four registry and preprocessing to validation dates."""
    assert_validation_read_allowed(settings.data_start, settings.analysis_end)
    raw = build_monthly_factor_panel(daily, FACTOR_DEFINITIONS, settings)
    features = preprocess_factor_panel(raw, readiness, settings)
    labelled = add_forward_returns(
        daily.select("date", "symbol", "close_adj", "volume", "amount"),
        settings.forward_horizons,
    )
    label_columns = [
        column
        for column in labelled.columns
        if column.startswith("forward_return_")
        or column.startswith("forward_calendar_days_")
        or column.startswith("forward_skipped_observations_")
    ]
    keys = features.select("date", "symbol").unique().sort("date", "symbol")
    forward_returns = keys.join(
        labelled.select("date", "symbol", *label_columns),
        on=["date", "symbol"],
        how="left",
        validate="1:1",
    ).sort("date", "symbol")
    return_columns = [
        f"forward_return_{horizon}" for horizon in settings.forward_horizons
    ]
    panel = features.join(
        forward_returns.select("date", "symbol", *return_columns),
        on=["date", "symbol"],
        how="left",
        validate="m:1",
    ).sort("date", "symbol", "factor_name")
    audit = panel.group_by("factor_name", "family", "point_in_time_status").agg(
        pl.len().alias("rows"),
        pl.col("score_size_neutral").is_finite().sum().alias("finite_scores"),
        pl.col("score_size_neutral").is_null().sum().alias("missing_scores"),
    ).with_columns(
        (pl.col("finite_scores") / pl.col("rows")).alias("coverage")
    ).sort("factor_name")
    return ValidationFactorExtension(features, forward_returns, panel, audit)
