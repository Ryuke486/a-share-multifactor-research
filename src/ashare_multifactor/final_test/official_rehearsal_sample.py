"""Deterministic market and size-stratified validation rehearsal sample."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
import math

import polars as pl

from ashare_multifactor.data.security import market_for_symbol


_MAX_VALIDATION_DATE = date(2021, 12, 31)
_SAMPLE_SCHEMA = {
    "symbol": pl.String,
    "market": pl.String,
    "size_bucket": pl.Int64,
    "latest_date": pl.Date,
    "total_market_cap": pl.Float64,
    "selection_reason": pl.String,
}


def select_rehearsal_sample(
    panel: pl.DataFrame,
    *,
    sample_size: int,
    required_symbols: tuple[str, ...],
) -> pl.DataFrame:
    """Select an exact deterministic sample without crossing the validation fence."""
    required_columns = {"date", "symbol", "total_market_cap"}
    if (
        not isinstance(panel, pl.DataFrame)
        or not required_columns <= set(panel.columns)
        or not isinstance(sample_size, int)
        or isinstance(sample_size, bool)
        or sample_size <= 0
    ):
        raise ValueError("official rehearsal sample input is invalid")
    if panel.is_empty() or panel.get_column("date").max() > _MAX_VALIDATION_DATE:
        raise ValueError("official rehearsal sample must remain inside 2021 validation")
    if len(set(required_symbols)) != len(required_symbols):
        raise ValueError("official rehearsal required symbols are duplicated")

    latest = (
        panel.select("date", "symbol", "total_market_cap")
        .drop_nulls()
        .sort(["symbol", "date"])
        .unique(subset=["symbol"], keep="last", maintain_order=True)
        .filter(
            pl.col("total_market_cap").is_finite()
            & (pl.col("total_market_cap") > 0)
        )
        .sort("symbol")
    )
    rows = [
        {
            "symbol": row["symbol"],
            "market": market_for_symbol(row["symbol"]),
            "latest_date": row["date"],
            "total_market_cap": float(row["total_market_cap"]),
        }
        for row in latest.iter_rows(named=True)
    ]
    by_symbol = {row["symbol"]: row for row in rows}
    missing = sorted(set(required_symbols) - set(by_symbol))
    if missing:
        raise ValueError(f"official rehearsal required symbols are missing: {missing}")
    if len(rows) < sample_size or len(required_symbols) > sample_size:
        raise ValueError("official rehearsal sample scope is insufficient")

    ranked = _with_size_buckets(rows)
    ranked_by_symbol = {row["symbol"]: row for row in ranked}
    selected = [
        {**ranked_by_symbol[symbol], "selection_reason": "required_known_case"}
        for symbol in required_symbols
    ]
    selected_symbols = set(required_symbols)
    strata: dict[tuple[str, int], list[dict[str, object]]] = defaultdict(list)
    for row in ranked:
        if row["symbol"] not in selected_symbols:
            strata[(str(row["market"]), int(row["size_bucket"]))].append(row)
    positions = {key: 0 for key in strata}
    keys = sorted(strata)
    while len(selected) < sample_size:
        advanced = False
        for key in keys:
            position = positions[key]
            if position >= len(strata[key]):
                continue
            selected.append(
                {
                    **strata[key][position],
                    "selection_reason": "market_size_stratum",
                }
            )
            positions[key] += 1
            advanced = True
            if len(selected) == sample_size:
                break
        if not advanced:
            raise ValueError("official rehearsal sample cannot fill every requested row")
    return pl.DataFrame(selected, schema=_SAMPLE_SCHEMA).sort("symbol")


def _with_size_buckets(
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for market in ("sh", "sz"):
        market_rows = sorted(
            (row for row in rows if row["market"] == market),
            key=lambda row: (float(row["total_market_cap"]), str(row["symbol"])),
        )
        count = len(market_rows)
        for rank, row in enumerate(market_rows):
            bucket = min(4, math.floor(rank * 4 / count) + 1)
            result.append({**row, "size_bucket": bucket})
    return sorted(result, key=lambda row: str(row["symbol"]))
