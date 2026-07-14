from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import polars as pl

from ashare_multifactor.audit.publication import PublishedRelease, resolve_current
from ashare_multifactor.execution.corporate_actions import normalize_corporate_actions
from ashare_multifactor.execution.market_state import build_execution_panel


@dataclass(frozen=True)
class FormalBacktestInputs:
    stage_five: PublishedRelease
    target_weights: pl.DataFrame
    execution_panel: pl.DataFrame
    corporate_actions: pl.DataFrame
    security_events: pl.DataFrame


def load_formal_backtest_inputs(
    data_root: Path,
    source_cache: Path,
    *,
    adv_lookback: int = 20,
) -> FormalBacktestInputs:
    stage_five = resolve_current(data_root / "processed" / "factor_combination")
    targets = (
        pl.read_parquet(stage_five.datasets / "target_weights.parquet")
        .filter(pl.col("portfolio_name") == "size_stratified_buffered")
        .select("date", "symbol", "target_weight")
        .sort("date", "symbol")
    )
    _validate_targets(targets)
    symbols = targets["symbol"].unique().sort().to_list()
    parts = [
        data_root
        / "processed"
        / "factor_research"
        / "daily_panel"
        / f"year={year}"
        / "part-000.parquet"
        for year in range(2005, 2017)
    ]
    if any(not path.is_file() for path in parts):
        raise FileNotFoundError("formal backtest daily panel is incomplete")
    panel = (
        pl.scan_parquet(parts)
        .filter(pl.col("symbol").is_in(symbols))
        .select(
            "date",
            "symbol",
            "open_raw",
            "close_raw",
            "prev_close_raw",
            "close_adj",
            "prev_close_adj",
            "amount",
            "is_st",
        )
        .collect()
    )
    panel = build_execution_panel(panel, adv_lookback=adv_lookback).select(
        "date",
        "symbol",
        "open_raw",
        "close_raw",
        "prev_close_raw",
        "close_adj",
        "prev_close_adj",
        "adv20",
        "limit_rate",
        "is_suspended_proxy",
    )
    actions = _load_actions(source_cache, symbols)
    security_events = pl.read_csv(
        Path(__file__).parents[3] / "configs" / "corporate_action_exceptions.csv",
        schema_overrides={"source_symbol": pl.String, "target_symbol": pl.String},
        try_parse_dates=True,
    )
    return FormalBacktestInputs(stage_five, targets, panel, actions, security_events)


def _validate_targets(targets: pl.DataFrame) -> None:
    if targets.is_empty() or targets.filter(pl.col("date") >= pl.date(2017, 1, 1)).height:
        raise ValueError("invalid stage five targets")
    summary = targets.group_by("date").agg(
        pl.len().alias("count"), pl.col("target_weight").sum().alias("weight")
    )
    if summary.filter((pl.col("count") != 100) | ((pl.col("weight") - 1).abs() > 1e-10)).height:
        raise ValueError("stage five target contract failed")
    if targets.filter(pl.col("target_weight") < 0).height:
        raise ValueError("negative target weight")


def _load_actions(source_cache: Path, symbols: list[str]) -> pl.DataFrame:
    east = pl.read_parquet(source_cache / "eastmoney_fhps" / "normalized_actions.parquet")
    sina = pl.read_parquet(
        source_cache / "sina_corporate_actions" / "normalized_dividends.parquet"
    )
    columns = [
        "symbol",
        "ex_date",
        "effective_date",
        "cash_per_share",
        "share_ratio",
        "source",
    ]
    east = (
        east.filter(pl.col("share_ratio") > 0)
        .with_columns(pl.col("effective_date").alias("ex_date"))
        .select(columns)
        .with_columns(pl.lit(0.0).alias("cash_per_share"))
    )
    supplementary = (
        sina.filter(pl.col("share_ratio") > 0)
        .with_columns(pl.col("effective_date").alias("ex_date"))
        .select(columns)
        .with_columns(pl.lit(0.0).alias("cash_per_share"))
        .join(
        east.select("symbol", "effective_date"),
        on=["symbol", "effective_date"],
        how="anti",
        )
    )
    cash = (
        pl.read_parquet(
            source_cache / "cninfo_dividend" / "normalized_payment_dates.parquet"
        )
        .unique(
            subset=["symbol", "effective_date", "ex_date", "cash_per_share"],
            maintain_order=True,
        )
        .select(columns)
    )
    special_path = source_cache / "cninfo_official_pdfs" / "official_special_actions.csv"
    special = (
        pl.read_csv(
            special_path,
            schema_overrides={"symbol": pl.String},
            try_parse_dates=True,
        )
        .select(
            "symbol",
            pl.col("effective_date").alias("ex_date"),
            "effective_date",
            "cash_per_share",
            pl.col("share_ratio"),
            pl.lit("cninfo_official_implementation_pdf").alias("source"),
        )
        .join(
            pl.concat([east, supplementary, cash]).select("symbol", "effective_date"),
            on=["symbol", "effective_date"],
            how="anti",
        )
    )
    combined = pl.concat([east, supplementary, cash, special]).filter(
        pl.col("symbol").is_in(symbols)
    )
    return normalize_corporate_actions(combined)
