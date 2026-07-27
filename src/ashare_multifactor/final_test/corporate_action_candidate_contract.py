"""Provider-row contract for final-test corporate-action candidates."""

from __future__ import annotations

from collections.abc import Iterable

import polars as pl

from ashare_multifactor.execution.corporate_actions import normalize_corporate_actions
from ashare_multifactor.final_test.gate import FINAL_TEST_END, FINAL_TEST_START


REQUIRED_BAOSTOCK_FIELDS = {
    "code",
    "dividOperateDate",
    "dividPayDate",
    "dividStockMarketDate",
    "dividCashPsBeforeTax",
    "dividStocksPs",
    "dividReserveToStockPs",
}


def normalize_corporate_action_candidates(
    raw: pl.DataFrame,
    *,
    symbols: Iterable[str],
) -> pl.DataFrame:
    """Normalize provider rows into candidates without treating them as facts."""
    required = REQUIRED_BAOSTOCK_FIELDS | {"query_year", "query_year_type"}
    missing = sorted(required - set(raw.columns))
    if missing:
        raise ValueError(f"missing BaoStock final dividend fields: {missing}")
    allowed_symbols = sorted(set(symbols))
    if raw.filter(
        (pl.col("query_year_type") != "operate")
        | ~pl.col("query_year").is_between(FINAL_TEST_START.year, FINAL_TEST_END.year)
    ).height:
        raise ValueError("sealed final-test query metadata is invalid")
    parsed = raw.with_columns(
        pl.col("code").cast(pl.String).str.split(".").list.last().alias("symbol"),
        *(
            pl.col(name)
            .cast(pl.String)
            .replace("", None)
            .str.to_date(strict=False)
            .alias(name)
            for name in ("dividOperateDate", "dividPayDate", "dividStockMarketDate")
        ),
        pl.col("dividCashPsBeforeTax")
        .cast(pl.Float64, strict=False)
        .fill_null(0.0)
        .alias("cash_per_share"),
        (
            pl.col("dividStocksPs").cast(pl.Float64, strict=False).fill_null(0.0)
            + pl.col("dividReserveToStockPs")
            .cast(pl.Float64, strict=False)
            .fill_null(0.0)
        ).alias("share_ratio"),
    )
    if parsed.filter(
        pl.col("dividOperateDate").is_null()
        | (pl.col("dividOperateDate").dt.year() != pl.col("query_year"))
        | ~pl.col("dividOperateDate").is_between(FINAL_TEST_START, FINAL_TEST_END)
    ).height:
        raise ValueError("BaoStock final operate-year response contract failed")
    if parsed.filter(~pl.col("symbol").is_in(allowed_symbols)).height:
        raise ValueError("BaoStock response symbol escapes the prepared scope")
    cash = parsed.filter(pl.col("cash_per_share") > 0)
    if cash.filter(pl.col("dividPayDate").is_null()).height:
        raise ValueError("final cash dividend is missing payment date")
    shares = parsed.filter(pl.col("share_ratio") > 0)
    candidates = pl.concat(
        (
            cash.select(
                "symbol",
                pl.col("dividOperateDate").alias("ex_date"),
                pl.col("dividPayDate").alias("effective_date"),
                "cash_per_share",
                pl.lit(0.0).alias("share_ratio"),
                pl.lit("baostock_dividend_operate_year").alias("source"),
            ),
            shares.select(
                "symbol",
                pl.col("dividOperateDate").alias("ex_date"),
                pl.coalesce("dividStockMarketDate", "dividOperateDate").alias(
                    "effective_date"
                ),
                pl.lit(0.0).alias("cash_per_share"),
                "share_ratio",
                pl.lit("baostock_dividend_operate_year").alias("source"),
            ),
        ),
        how="vertical_relaxed",
    ).unique(
        subset=[
            "symbol",
            "ex_date",
            "effective_date",
            "cash_per_share",
            "share_ratio",
            "source",
        ],
        maintain_order=True,
    )
    if candidates.filter(
        (pl.col("effective_date") < pl.col("ex_date"))
        | ~pl.col("effective_date").is_between(FINAL_TEST_START, FINAL_TEST_END)
    ).height:
        raise ValueError("final corporate action effective date is invalid")
    return normalize_corporate_actions(
        candidates,
        maximum_date=FINAL_TEST_END,
    ).rename({"action_id": "candidate_id"})
