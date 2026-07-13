"""Same-date cross-sectional preprocessing for raw factor values."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import polars as pl

from ashare_multifactor.config import FactorResearchSettings
from ashare_multifactor.data.field_audit import FieldReadiness
from ashare_multifactor.factors.definitions import FACTOR_DEFINITIONS, FactorDefinition


_RAW_COLUMNS = ("date", "symbol", "factor_name", "family", "raw_value")
_OUTPUT_COLUMNS = (
    *_RAW_COLUMNS,
    "winsorized_value",
    "score",
    "score_size_neutral",
    "score_industry_size_neutral",
    "point_in_time_status",
    "preprocessing_reason",
)
_OUTPUT_SCHEMA = {
    "date": pl.Date,
    "symbol": pl.String,
    "factor_name": pl.String,
    "family": pl.String,
    "raw_value": pl.Float64,
    "winsorized_value": pl.Float64,
    "score": pl.Float64,
    "score_size_neutral": pl.Float64,
    "score_industry_size_neutral": pl.Float64,
    "point_in_time_status": pl.String,
    "preprocessing_reason": pl.String,
}


def preprocess_factor_panel(
    panel: pl.DataFrame,
    readiness: FieldReadiness,
    settings: FactorResearchSettings,
) -> pl.DataFrame:
    """Winsorize, direct, and size-neutralize each signal-date cross-section."""
    definitions_by_name = {definition.name: definition for definition in FACTOR_DEFINITIONS}
    factor_names = panel.get_column("factor_name").unique().to_list()
    missing_readiness = sorted(set(factor_names) - readiness.factor_status.keys())
    if missing_readiness:
        raise ValueError(
            "missing point-in-time readiness for factors: " + ", ".join(missing_readiness)
        )
    missing_definitions = sorted(set(factor_names) - definitions_by_name.keys())
    if missing_definitions:
        raise ValueError("unknown factor definitions: " + ", ".join(missing_definitions))
    if readiness.industry_neutralization_enabled:
        raise ValueError("industry neutralization requires an approved historical industry input")
    _validate_panel(panel)
    if panel.is_empty():
        return pl.DataFrame(schema=_OUTPUT_SCHEMA)

    raw_panel = (
        panel.select(*_RAW_COLUMNS)
        .cast(
            {
                "date": pl.Date,
                "symbol": pl.String,
                "factor_name": pl.String,
                "family": pl.String,
                "raw_value": pl.Float64,
            }
        )
        .sort("date", "factor_name", "symbol")
    )
    scored = _score_cross_sections(raw_panel, definitions_by_name, settings)
    neutralized = _neutralize_size(scored, definitions_by_name)
    statuses = pl.DataFrame(
        {
            "factor_name": factor_names,
            "point_in_time_status": [readiness.factor_status[name] for name in factor_names],
        },
        schema={"factor_name": pl.String, "point_in_time_status": pl.String},
    )
    return (
        neutralized.join(statuses, on="factor_name", how="left", validate="m:1")
        .with_columns(pl.lit(None, dtype=pl.Float64).alias("score_industry_size_neutral"))
        .select(*_OUTPUT_COLUMNS)
        .cast(_OUTPUT_SCHEMA)
        .sort("date", "symbol", "factor_name")
    )


def _validate_panel(panel: pl.DataFrame) -> None:
    missing = [column for column in _RAW_COLUMNS if column not in panel.columns]
    if missing:
        raise ValueError("factor panel is missing columns: " + ", ".join(missing))
    if panel.select("date", "symbol", "factor_name").is_duplicated().any():
        raise ValueError("duplicate date, symbol, and factor_name keys are not allowed")


def _score_cross_sections(
    panel: pl.DataFrame,
    definitions_by_name: dict[str, FactorDefinition],
    settings: FactorResearchSettings,
) -> pl.DataFrame:
    groups = []
    for group in panel.partition_by(["date", "factor_name"], maintain_order=True):
        factor_name = group.item(0, "factor_name")
        groups.append(
            _score_group(
                group,
                direction=definitions_by_name[factor_name].direction,
                lower=settings.winsor_lower,
                upper=settings.winsor_upper,
            )
        )
    return pl.concat(groups, how="vertical")


def _score_group(
    group: pl.DataFrame,
    *,
    direction: int,
    lower: float,
    upper: float,
) -> pl.DataFrame:
    raw = group.get_column("raw_value").to_list()
    normalized: list[float | None] = []
    reasons: list[str | None] = []
    for value in raw:
        if value is None:
            normalized.append(None)
            reasons.append("missing_raw_value")
        elif not np.isfinite(value):
            normalized.append(None)
            reasons.append("non_finite_raw_value")
        else:
            normalized.append(float(value))
            reasons.append(None)

    valid_indices = [index for index, value in enumerate(normalized) if value is not None]
    valid_values = np.asarray([normalized[index] for index in valid_indices], dtype=float)
    winsorized: list[float | None] = [None] * group.height
    scores: list[float | None] = [None] * group.height
    if valid_indices:
        lower_bound, upper_bound = np.quantile(
            valid_values,
            [lower, upper],
            method="linear",
        )
        clipped = np.clip(valid_values, lower_bound, upper_bound)
        for index, value in zip(valid_indices, clipped, strict=True):
            winsorized[index] = float(value)

        if len(valid_indices) < 2:
            _set_reason(reasons, valid_indices, "insufficient_valid_values")
        else:
            standard_deviation = float(clipped.std(ddof=0))
            if not np.isfinite(standard_deviation) or standard_deviation == 0.0:
                _set_reason(reasons, valid_indices, "zero_score_variance")
            else:
                directed = direction * (clipped - clipped.mean()) / standard_deviation
                for index, value in zip(valid_indices, directed, strict=True):
                    scores[index] = float(value)

    return group.with_columns(
        pl.Series("raw_value", normalized, dtype=pl.Float64),
        pl.Series("winsorized_value", winsorized, dtype=pl.Float64),
        pl.Series("score", scores, dtype=pl.Float64),
        pl.Series("preprocessing_reason", reasons, dtype=pl.String),
    )


def _neutralize_size(
    scored: pl.DataFrame,
    definitions_by_name: dict[str, FactorDefinition],
) -> pl.DataFrame:
    size_scores = scored.filter(pl.col("factor_name") == "log_market_cap").select(
        "date",
        "symbol",
        pl.col("score").alias("_size_score"),
    )
    joined = scored.join(
        size_scores,
        on=["date", "symbol"],
        how="left",
        validate="m:1",
    )
    groups = []
    for group in joined.partition_by(["date", "factor_name"], maintain_order=True):
        factor_name = group.item(0, "factor_name")
        groups.append(
            _neutralize_group(
                group,
                size_neutralize=definitions_by_name[factor_name].size_neutralize,
            )
        )
    return pl.concat(groups, how="vertical").drop("_size_score")


def _neutralize_group(
    group: pl.DataFrame,
    *,
    size_neutralize: bool,
) -> pl.DataFrame:
    neutral: list[float | None] = [None] * group.height
    if not size_neutralize:
        return group.with_columns(pl.Series("score_size_neutral", neutral, dtype=pl.Float64))

    scores = _finite_array(group.get_column("score").to_list())
    size_scores = _finite_array(group.get_column("_size_score").to_list())
    reasons = group.get_column("preprocessing_reason").to_list()
    score_valid = np.isfinite(scores)
    size_valid = np.isfinite(size_scores)
    missing_size_indices = np.flatnonzero(score_valid & ~size_valid).tolist()
    _set_reason(reasons, missing_size_indices, "missing_size_score")
    common_indices = np.flatnonzero(score_valid & size_valid)
    if common_indices.size < 3:
        _set_reason(
            reasons,
            common_indices.tolist(),
            "insufficient_size_samples",
        )
        return _with_neutral_results(group, neutral, reasons)

    x = size_scores[common_indices]
    y = scores[common_indices]
    x_centered = x - x.mean()
    design = np.column_stack((np.ones(common_indices.size), x_centered))
    if np.linalg.matrix_rank(design) < design.shape[1]:
        _set_reason(reasons, common_indices.tolist(), "singular_size_control")
        return _with_neutral_results(group, neutral, reasons)

    orthogonal_basis, _ = np.linalg.qr(design, mode="reduced")
    fitted = orthogonal_basis @ (orthogonal_basis.T @ y)
    residual = y - fitted
    residual -= orthogonal_basis @ (orthogonal_basis.T @ residual)
    fitted = y - residual
    residual_standard_deviation = float(residual.std(ddof=0))
    scale = max(
        float(np.max(np.abs(y))),
        float(np.max(np.abs(fitted))),
        float(np.linalg.norm(design, ord=np.inf)),
    )
    exact_fit_tolerance = np.finfo(float).eps * max(design.shape) * scale
    if (
        not np.isfinite(residual_standard_deviation)
        or residual_standard_deviation == 0.0
        or float(np.max(np.abs(residual))) <= exact_fit_tolerance
    ):
        _set_reason(reasons, common_indices.tolist(), "constant_size_residual")
        return _with_neutral_results(group, neutral, reasons)

    standardized = (residual - residual.mean()) / residual_standard_deviation
    for index, value in zip(common_indices, standardized, strict=True):
        neutral[index] = float(value)
    return _with_neutral_results(group, neutral, reasons)


def _finite_array(values: Sequence[float | None]) -> np.ndarray:
    return np.asarray(
        [float(value) if value is not None and np.isfinite(value) else np.nan for value in values]
    )


def _set_reason(
    reasons: list[str | None],
    indices: Sequence[int],
    reason: str,
) -> None:
    for index in indices:
        if reasons[index] is None:
            reasons[index] = reason


def _with_neutral_results(
    group: pl.DataFrame,
    neutral: list[float | None],
    reasons: list[str | None],
) -> pl.DataFrame:
    return group.with_columns(
        pl.Series("score_size_neutral", neutral, dtype=pl.Float64),
        pl.Series("preprocessing_reason", reasons, dtype=pl.String),
    )
