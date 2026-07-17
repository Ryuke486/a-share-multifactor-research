from __future__ import annotations

from datetime import date

import polars as pl

from ashare_multifactor.data.security import market_for_symbol
from ashare_multifactor.execution.corporate_actions import normalize_corporate_actions
from ashare_multifactor.final_test.gate import FINAL_TEST_END, FINAL_TEST_START


_SECURITY_SCHEMA = {
    "effective_date": pl.Date,
    "source_symbol": pl.String,
    "event_type": pl.String,
    "target_symbol": pl.String,
    "ratio": pl.Float64,
    "cash_per_share": pl.Float64,
    "source": pl.String,
    "evidence_id": pl.String,
}


def normalize_security_event_rows(frame: pl.DataFrame) -> pl.DataFrame:
    """Validate and normalize the one security-event execution contract."""
    missing = set(_SECURITY_SCHEMA) - set(frame.columns)
    if missing:
        raise ValueError(
            f"official security event execution schema lacks fields: {sorted(missing)}"
        )
    try:
        events = frame.select(
            pl.col(name).cast(dtype, strict=False).alias(name)
            for name, dtype in _SECURITY_SCHEMA.items()
        )
    except pl.exceptions.PolarsError as error:
        raise ValueError("invalid official security event execution contract") from error
    invalid = events.filter(
        pl.col("effective_date").is_null()
        | pl.col("source_symbol").is_null()
        | pl.col("source_symbol").str.strip_chars().eq("")
        | pl.col("event_type").is_null()
        | pl.col("event_type").str.strip_chars().eq("")
        | pl.col("ratio").is_null()
        | ~pl.col("ratio").is_finite()
        | pl.col("cash_per_share").is_null()
        | ~pl.col("cash_per_share").is_finite()
        | pl.col("source").is_null()
        | pl.col("source").str.strip_chars().eq("")
        | pl.col("evidence_id").is_null()
        | pl.col("evidence_id").str.strip_chars().eq("")
        | ~pl.col("effective_date").is_between(FINAL_TEST_START, FINAL_TEST_END)
        | ~pl.col("event_type").is_in(["stock_merger", "write_off"])
        | (
            (pl.col("event_type") == "stock_merger")
            & (
                pl.col("target_symbol").is_null()
                | pl.col("target_symbol").str.strip_chars().eq("")
                | (pl.col("target_symbol") == pl.col("source_symbol"))
                | (pl.col("ratio") <= 0)
                | (pl.col("cash_per_share") != 0)
            )
        )
        | (
            (pl.col("event_type") == "write_off")
            & (
                pl.col("target_symbol").is_not_null()
                | (pl.col("ratio") != 0)
                | (pl.col("cash_per_share") != 0)
            )
        )
    )
    duplicate = events.select(
        pl.struct("effective_date", "source_symbol").is_duplicated().any()
    ).item()
    if invalid.height or duplicate:
        raise ValueError("invalid official security event execution contract")
    for column in ("source_symbol", "target_symbol"):
        populated = events.filter(pl.col(column).is_not_null())
        if populated.filter(
            ~pl.col(column)
            .map_elements(market_for_symbol, return_dtype=pl.String)
            .is_in(("sh", "sz"))
        ).height:
            raise ValueError("invalid official security event execution contract")
    return events.sort("effective_date", "source_symbol")


def normalize_corporate_action_rows(
    frame: pl.DataFrame,
    *,
    maximum_date: date = FINAL_TEST_END,
) -> pl.DataFrame:
    """Validate and normalize the one corporate-action execution contract."""
    required = {
        "symbol",
        "ex_date",
        "effective_date",
        "cash_per_share",
        "share_ratio",
        "source",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(
            f"official corporate-action execution schema lacks fields: {sorted(missing)}"
        )
    try:
        rows = frame.select(
            pl.col("symbol").cast(pl.String).str.zfill(6),
            pl.col("ex_date").cast(pl.Date, strict=False),
            pl.col("effective_date").cast(pl.Date, strict=False),
            pl.col("cash_per_share").cast(pl.Float64, strict=False),
            pl.col("share_ratio").cast(pl.Float64, strict=False),
            pl.col("source").cast(pl.String),
        )
    except pl.exceptions.PolarsError as error:
        raise ValueError("invalid official corporate-action execution contract") from error
    invalid = rows.filter(
        pl.col("symbol").is_null()
        | pl.col("symbol").str.strip_chars().eq("")
        | pl.col("ex_date").is_null()
        | pl.col("effective_date").is_null()
        | pl.col("cash_per_share").is_null()
        | ~pl.col("cash_per_share").is_finite()
        | pl.col("share_ratio").is_null()
        | ~pl.col("share_ratio").is_finite()
        | pl.col("source").is_null()
        | pl.col("source").str.strip_chars().eq("")
        | ~pl.col("ex_date").is_between(FINAL_TEST_START, maximum_date)
        | ~pl.col("effective_date").is_between(FINAL_TEST_START, maximum_date)
        | (pl.col("effective_date") < pl.col("ex_date"))
        | (pl.col("cash_per_share") < 0)
        | (pl.col("share_ratio") < -1)
    )
    key = [
        "symbol",
        "ex_date",
        "effective_date",
        "cash_per_share",
        "share_ratio",
        "source",
    ]
    if invalid.height or rows.select(pl.struct(key).is_duplicated().any()).item():
        raise ValueError("invalid official corporate-action execution contract")
    if rows.filter(
        ~pl.col("symbol")
        .map_elements(market_for_symbol, return_dtype=pl.String)
        .is_in(("sh", "sz"))
    ).height:
        raise ValueError("invalid official corporate-action execution contract")
    return normalize_corporate_actions(rows, maximum_date=maximum_date)
