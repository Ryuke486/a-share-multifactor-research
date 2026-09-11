from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from ashare_multifactor.execution.corporate_actions import normalize_corporate_actions


VALIDATION_START = date(2017, 1, 1)
VALIDATION_END = date(2021, 12, 31)
VALIDATION_YEARS = range(VALIDATION_START.year, VALIDATION_END.year + 1)
_RAW_DATE_COLUMNS = (
    "dividPreNoticeDate",
    "dividAgmPumDate",
    "dividPlanAnnounceDate",
    "dividPlanDate",
    "dividRegistDate",
    "dividOperateDate",
    "dividPayDate",
    "dividStockMarketDate",
)


def assert_baostock_query_scope(year: int, year_type: str) -> None:
    """Allow only source-side queries keyed by validation ex-dividend year."""
    if year_type != "operate":
        raise ValueError("BaoStock validation query must use yearType=operate")
    if year not in VALIDATION_YEARS:
        raise ValueError(f"sealed final test query rejected: {year}")


def load_validation_corporate_actions(
    research_actions: pl.DataFrame,
    raw_path: Path,
    *,
    symbols: list[str],
    payment_date_overrides: pl.DataFrame | None = None,
    action_corrections: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Extend audited research actions using source-bounded BaoStock rows."""
    raw = pl.read_parquet(raw_path)
    validation = _normalize_baostock_actions(
        raw,
        set(symbols),
        payment_date_overrides=payment_date_overrides,
        action_corrections=action_corrections,
    )
    schema = {
        "symbol": pl.String,
        "ex_date": pl.Date,
        "effective_date": pl.Date,
        "cash_per_share": pl.Float64,
        "share_ratio": pl.Float64,
        "source": pl.String,
    }
    combined = pl.concat(
        (
            research_actions.select(*schema),
            validation,
        ),
        how="vertical_relaxed",
    ).unique(
        subset=[
            "symbol",
            "ex_date",
            "effective_date",
            "cash_per_share",
            "share_ratio",
        ],
        maintain_order=True,
    )
    return normalize_corporate_actions(
        combined.sort("effective_date", "symbol"),
        maximum_date=VALIDATION_END,
    )


def _normalize_baostock_actions(
    raw: pl.DataFrame,
    symbols: set[str],
    *,
    payment_date_overrides: pl.DataFrame | None = None,
    action_corrections: pl.DataFrame | None = None,
) -> pl.DataFrame:
    required = {
        "code",
        "query_year",
        "query_year_type",
        "dividPlanAnnounceDate",
        "dividRegistDate",
        "dividOperateDate",
        "dividPayDate",
        "dividStockMarketDate",
        "dividCashPsBeforeTax",
        "dividStocksPs",
        "dividReserveToStockPs",
    }
    missing = sorted(required - set(raw.columns))
    if missing:
        raise ValueError(f"missing BaoStock dividend fields: {missing}")
    if raw.filter(
        (pl.col("query_year_type") != "operate")
        | ~pl.col("query_year").is_between(2017, 2021)
    ).height:
        raise ValueError("sealed final test query metadata found in BaoStock cache")

    parsed = raw.with_columns(
        *(
            pl.col(column)
            .cast(pl.String)
            .replace("", None)
            .str.to_date(strict=False)
            .alias(column)
            for column in _RAW_DATE_COLUMNS
            if column in raw.columns
        ),
        pl.col("code").cast(pl.String).str.split(".").list.last().alias("symbol"),
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
    for column in _RAW_DATE_COLUMNS:
        if column in parsed.columns and parsed.filter(
            pl.col(column) >= date(2022, 1, 1)
        ).height:
            raise ValueError(f"sealed final test date found in BaoStock cache: {column}")
    parsed = _apply_official_action_corrections(parsed, action_corrections)
    parsed = _apply_payment_date_overrides(parsed, payment_date_overrides)
    if parsed.filter(
        pl.col("dividOperateDate").is_null()
        | (pl.col("dividOperateDate").dt.year() != pl.col("query_year"))
    ).height:
        raise ValueError("BaoStock operate-year response contract failed")
    parsed = parsed.filter(pl.col("symbol").is_in(symbols))
    cash = parsed.filter(pl.col("cash_per_share") > 0)
    if cash.filter(pl.col("dividPayDate").is_null()).height:
        raise ValueError("cash dividend missing payment date")
    shares = parsed.filter(pl.col("share_ratio") > 0)
    rows = pl.concat(
        (
            cash.select(
                "symbol",
                pl.col("dividOperateDate").alias("ex_date"),
                pl.col("dividPayDate").alias("effective_date"),
                "cash_per_share",
                pl.lit(0.0).alias("share_ratio"),
                pl.when(pl.col("official_action_correction"))
                .then(pl.lit("baostock_dividend_operate_year+cninfo_action_correction"))
                .when(pl.col("official_payment_date").is_not_null())
                .then(pl.lit("baostock_dividend_operate_year+cninfo_payment_date"))
                .otherwise(pl.lit("baostock_dividend_operate_year"))
                .alias("source"),
            ),
            shares.select(
                "symbol",
                pl.col("dividOperateDate").alias("ex_date"),
                pl.coalesce("dividStockMarketDate", "dividOperateDate").alias(
                    "effective_date"
                ),
                pl.lit(0.0).alias("cash_per_share"),
                "share_ratio",
                pl.when(pl.col("official_action_correction"))
                .then(pl.lit("baostock_dividend_operate_year+cninfo_action_correction"))
                .otherwise(pl.lit("baostock_dividend_operate_year"))
                .alias("source"),
            ),
        ),
        how="vertical_relaxed",
    )
    economic_key = [
        "symbol",
        "ex_date",
        "effective_date",
        "cash_per_share",
        "share_ratio",
    ]
    return (
        rows.with_columns(
            pl.when(pl.col("source").str.ends_with("+cninfo_action_correction"))
            .then(2)
            .when(pl.col("source").str.ends_with("+cninfo_payment_date"))
            .then(1)
            .otherwise(0)
            .alias("_provenance_priority")
        )
        .sort("_provenance_priority", descending=True)
        .unique(subset=economic_key, maintain_order=True)
        .drop("_provenance_priority")
        .sort("effective_date", "symbol")
    )


def _apply_official_action_corrections(
    parsed: pl.DataFrame,
    corrections: pl.DataFrame | None,
) -> pl.DataFrame:
    if corrections is None:
        return parsed.with_columns(pl.lit(False).alias("official_action_correction"))
    required = {
        "symbol",
        "source_ex_date",
        "corrected_ex_date",
        "payment_date",
        "source_url",
    }
    missing = sorted(required - set(corrections.columns))
    if missing:
        raise ValueError(f"action-correction fields missing: {missing}")
    key = ["symbol", "source_ex_date"]
    if corrections.select(pl.struct(*key).is_duplicated().any()).item():
        raise ValueError("duplicate action-correction key")
    source_keys = parsed.select(
        "symbol", pl.col("dividOperateDate").alias("source_ex_date")
    )
    if corrections.join(source_keys, on=key, how="anti").height:
        raise ValueError("official action correction does not match BaoStock row")
    joined = parsed.join(
        corrections.select(
            "symbol",
            "source_ex_date",
            "corrected_ex_date",
            pl.col("payment_date").alias("corrected_payment_date"),
        ),
        left_on=["symbol", "dividOperateDate"],
        right_on=["symbol", "source_ex_date"],
        how="left",
    )
    return joined.with_columns(
        pl.col("corrected_ex_date").is_not_null().alias("official_action_correction"),
        pl.coalesce("corrected_ex_date", "dividOperateDate").alias(
            "dividOperateDate"
        ),
        pl.coalesce("corrected_payment_date", "dividPayDate").alias("dividPayDate"),
    ).drop("corrected_ex_date", "corrected_payment_date")


def _apply_payment_date_overrides(
    parsed: pl.DataFrame,
    overrides: pl.DataFrame | None,
) -> pl.DataFrame:
    if overrides is None:
        return parsed.with_columns(
            pl.lit(None, dtype=pl.Date).alias("official_payment_date")
        )
    required = {"symbol", "ex_date", "payment_date", "source_url"}
    missing = sorted(required - set(overrides.columns))
    if missing:
        raise ValueError(f"payment-date override fields missing: {missing}")
    if overrides.select(pl.struct("symbol", "ex_date").is_duplicated().any()).item():
        raise ValueError("duplicate payment-date override key")
    keys = parsed.select(
        "symbol", pl.col("dividOperateDate").alias("ex_date")
    ).unique()
    if overrides.join(keys, on=["symbol", "ex_date"], how="anti").height:
        raise ValueError("official payment-date evidence does not match BaoStock row")
    joined = parsed.join(
        overrides.select(
            "symbol",
            pl.col("ex_date").alias("override_ex_date"),
            pl.col("payment_date").alias("official_payment_date"),
        ),
        left_on=["symbol", "dividOperateDate"],
        right_on=["symbol", "override_ex_date"],
        how="left",
    )
    if joined.filter(
        pl.col("dividPayDate").is_not_null()
        & pl.col("official_payment_date").is_not_null()
        & (pl.col("dividPayDate") != pl.col("official_payment_date"))
    ).height:
        raise ValueError("official payment date conflicts with BaoStock")
    return joined.with_columns(
        pl.coalesce("dividPayDate", "official_payment_date").alias("dividPayDate")
    )
