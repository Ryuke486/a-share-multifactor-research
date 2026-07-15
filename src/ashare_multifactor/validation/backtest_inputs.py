from __future__ import annotations

from datetime import date

import polars as pl

from ashare_multifactor.execution.market_state import build_execution_panel


_RESEARCH_PORTFOLIO = {
    "family_equal_size_stratified_buffered": "size_stratified_buffered",
    "rolling_ic_family_size_stratified_buffered": "size_stratified_buffered",
    "family_equal_top100_equal": "size_stratified_buffered",
}


def build_continuous_targets(
    research_targets: pl.DataFrame,
    validation_targets: pl.DataFrame,
    *,
    candidate: str,
) -> pl.DataFrame:
    """Join the frozen research deployment to one validation candidate."""
    try:
        research_portfolio = _RESEARCH_PORTFOLIO[candidate]
    except KeyError as exc:
        raise ValueError(f"unknown validation candidate: {candidate}") from exc
    research = research_targets.filter(
        pl.col("portfolio_name") == research_portfolio
    ).select("date", "symbol", "target_weight")
    validation = validation_targets.filter(
        pl.col("candidate") == candidate
    ).select("date", "symbol", "target_weight")
    if research.is_empty() or validation.is_empty():
        raise ValueError("continuous target inputs are incomplete")
    if research.filter(pl.col("date") >= date(2017, 1, 1)).height:
        raise ValueError("research targets cross into validation")
    if validation.filter(~pl.col("date").is_between(date(2017, 1, 1), date(2021, 12, 31))).height:
        raise ValueError("validation targets cross a sealed boundary")
    combined = pl.concat((research, validation)).sort("date", "symbol")
    if combined.select(pl.struct("date", "symbol").is_duplicated().any()).item():
        raise ValueError("duplicate continuous target key")
    return combined


def extend_execution_panel(
    research_execution: pl.DataFrame,
    raw_extension: pl.DataFrame,
    *,
    adv_lookback: int,
) -> pl.DataFrame:
    """Compute validation market state with research rows as rolling warmup."""
    if research_execution.filter(pl.col("date") >= date(2017, 1, 1)).height:
        raise ValueError("research execution panel crosses into validation")
    if raw_extension.filter(pl.col("date") > date(2021, 12, 31)).height:
        raise ValueError("raw execution extension crosses the sealed final test")
    validation = (
        build_execution_panel(raw_extension, adv_lookback=adv_lookback)
        .filter(pl.col("date") >= date(2017, 1, 1))
        .select(research_execution.columns)
    )
    combined = pl.concat((research_execution, validation), how="vertical_relaxed").sort(
        "date", "symbol"
    )
    if combined.select(pl.struct("date", "symbol").is_duplicated().any()).item():
        raise ValueError("duplicate continuous execution key")
    return combined
