from __future__ import annotations

from datetime import date

import polars as pl


_FHPS_COLUMNS = {
    "代码",
    "送转股份-送转总比例",
    "现金分红-现金分红比例",
    "股权登记日",
    "除权除息日",
    "方案进度",
    "最新公告日期",
}


def normalize_fhps_em(
    frame: pl.DataFrame,
    *,
    report_period: str,
    filter_to_research_period: bool = False,
) -> pl.DataFrame:
    """Normalize implemented Eastmoney distributions returned by AKShare."""
    missing = sorted(_FHPS_COLUMNS - set(frame.columns))
    if missing:
        raise ValueError(f"missing provider columns: {missing}")
    result = (
        frame.filter(
            (pl.col("方案进度") == "实施分配")
            & pl.col("除权除息日").is_not_null()
        )
        .select(
            pl.col("代码").cast(pl.String).str.zfill(6).alias("symbol"),
            pl.col("除权除息日").cast(pl.Date).alias("effective_date"),
            pl.col("股权登记日").cast(pl.Date).alias("record_date"),
            (pl.col("现金分红-现金分红比例").fill_null(0.0) / 10.0).alias(
                "cash_per_share"
            ),
            (pl.col("送转股份-送转总比例").fill_null(0.0) / 10.0).alias(
                "share_ratio"
            ),
            pl.lit(report_period).alias("report_period"),
            pl.col("最新公告日期").cast(pl.Date).alias("source_announcement_date"),
            pl.lit("eastmoney_fhps_via_akshare").alias("source"),
        )
        .sort("effective_date", "symbol")
    )
    outside_period = (
        (pl.col("effective_date") < date(2005, 1, 1))
        | (pl.col("effective_date") > date(2016, 12, 31))
    )
    if filter_to_research_period:
        return result.filter(~outside_period)
    if result.filter(outside_period).height:
        raise ValueError("corporate action outside research period")
    return result


def normalize_cninfo_dividend(frame: pl.DataFrame, *, symbol: str) -> pl.DataFrame:
    required = {
        "实施方案公告日期",
        "送股比例",
        "转增比例",
        "派息比例",
        "股权登记日",
        "除权日",
        "派息日",
        "股份到账日",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"missing provider columns: {missing}")
    rows: list[dict[str, object]] = []
    for item in frame.iter_rows(named=True):
        cash_ratio = float(item["派息比例"] or 0.0) / 10.0
        share_ratio = float(item["送股比例"] or 0.0) / 10.0 + float(
            item["转增比例"] or 0.0
        ) / 10.0
        if share_ratio:
            credit_date = _optional_date(item["股份到账日"]) or _optional_date(
                item["除权日"]
            )
            if credit_date is None:
                raise ValueError("share distribution missing credit date")
            rows.append(
                _cninfo_event(
                    symbol,
                    credit_date,
                    "share_distribution",
                    cash_per_share=0.0,
                    share_ratio=share_ratio,
                    item=item,
                )
            )
        if cash_ratio:
            payment_date = _optional_date(item["派息日"])
            if payment_date is None:
                raise ValueError("cash dividend missing payment date")
            rows.append(
                _cninfo_event(
                    symbol,
                    payment_date,
                    "cash_dividend",
                    cash_per_share=cash_ratio,
                    share_ratio=0.0,
                    item=item,
                )
            )
    schema = {
        "symbol": pl.String,
        "effective_date": pl.Date,
        "event_type": pl.String,
        "cash_per_share": pl.Float64,
        "share_ratio": pl.Float64,
        "record_date": pl.Date,
        "source_announcement_date": pl.Date,
        "source": pl.String,
    }
    return (
        pl.DataFrame(rows, schema=schema)
        .filter(
            pl.col("effective_date").is_between(
                date(2005, 1, 1), date(2016, 12, 31), closed="both"
            )
        )
        .sort("effective_date", "event_type")
    )


def _optional_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "nan", "nat"}:
        return None
    return date.fromisoformat(text[:10])


def _cninfo_event(
    symbol: str,
    effective_date: date,
    event_type: str,
    *,
    cash_per_share: float,
    share_ratio: float,
    item: dict[str, object],
) -> dict[str, object]:
    return {
        "symbol": symbol.zfill(6),
        "effective_date": effective_date,
        "event_type": event_type,
        "cash_per_share": cash_per_share,
        "share_ratio": share_ratio,
        "record_date": _optional_date(item["股权登记日"]),
        "source_announcement_date": _optional_date(item["实施方案公告日期"]),
        "source": "cninfo_dividend_via_akshare",
    }
