from pathlib import Path

import polars as pl

from ashare_multifactor.data.discovery import DailyFilePair
from ashare_multifactor.data.schema import CANONICAL_COLUMNS


RAW_RENAME = {
    "日期": "date",
    "代码": "symbol",
    "名称": "name",
    "所属行业": "industry",
    "开盘价": "open_raw",
    "最高价": "high_raw",
    "最低价": "low_raw",
    "收盘价": "close_raw",
    "前收盘价": "prev_close_raw",
    "成交量（股）": "volume",
    "成交额（元）": "amount",
    "换手率": "turnover",
    "是否ST": "is_st",
    "是否涨停": "is_limit_up",
    "总股本（股）": "total_shares",
    "流通股本（股）": "float_shares",
    "总市值（元）": "total_market_cap",
    "流通市值（元）": "float_market_cap",
    "滚动市盈率": "pe_ttm",
    "市净率": "pb",
    "滚动市销率": "ps_ttm",
    "上市时间": "list_date",
    "退市时间": "delist_date",
    "是否融资融券": "is_margin",
}

ADJ_RENAME = {
    "日期": "date",
    "代码": "symbol",
    "开盘价": "open_adj",
    "最高价": "high_adj",
    "最低价": "low_adj",
    "收盘价": "close_adj",
    "前收盘价": "prev_close_adj",
}

NUMERIC_SCHEMA = {
    "open_raw": pl.Float64,
    "high_raw": pl.Float64,
    "low_raw": pl.Float64,
    "close_raw": pl.Float64,
    "prev_close_raw": pl.Float64,
    "open_adj": pl.Float64,
    "high_adj": pl.Float64,
    "low_adj": pl.Float64,
    "close_adj": pl.Float64,
    "prev_close_adj": pl.Float64,
    "volume": pl.Int64,
    "amount": pl.Float64,
    "turnover": pl.Float64,
    "total_shares": pl.Int64,
    "float_shares": pl.Int64,
    "total_market_cap": pl.Float64,
    "float_market_cap": pl.Float64,
    "pe_ttm": pl.Float64,
    "pb": pl.Float64,
    "ps_ttm": pl.Float64,
}


def _read_selected(path: Path, rename: dict[str, str]) -> pl.DataFrame:
    frame = pl.read_csv(path, schema_overrides={"代码": pl.String})
    if "是否融资融券" in rename and "是否融资融券" not in frame.columns:
        frame = frame.with_columns(pl.lit(None, dtype=pl.Boolean).alias("是否融资融券"))
    return (
        frame.select(*rename)
        .rename(rename)
        .with_columns(
            pl.col("date").str.to_date(),
            pl.col("symbol").str.zfill(6),
        )
    )


def _parse_boolean(column: str) -> pl.Expr:
    return pl.col(column).cast(pl.String).replace_strict(
        {"是": True, "否": False}, default=None, return_dtype=pl.Boolean
    )


def read_daily_pair(pair: DailyFilePair) -> pl.DataFrame:
    raw = _read_selected(pair.unadjusted, RAW_RENAME).with_columns(
        _parse_boolean("is_st"),
        _parse_boolean("is_limit_up"),
        _parse_boolean("is_margin"),
        pl.col("list_date").str.to_date("%Y-%m-%d", strict=False),
        pl.col("delist_date").str.to_date("%Y-%m-%d", strict=False),
    )
    adj = _read_selected(pair.backward_adjusted, ADJ_RENAME)
    expected_dates = {pair.trading_date}
    for label, frame in (("unadjusted", raw), ("backward-adjusted", adj)):
        dates = set(frame.get_column("date"))
        if dates != expected_dates:
            raise ValueError(f"{label} CSV date must be {pair.trading_date.isoformat()}")
    raw_symbols = set(raw.get_column("symbol"))
    adj_symbols = set(adj.get_column("symbol"))
    if raw_symbols != adj_symbols:
        raise ValueError("unadjusted and backward-adjusted symbol sets differ")
    raw_keys = set(raw.select("date", "symbol").iter_rows())
    adj_keys = set(adj.select("date", "symbol").iter_rows())
    if raw_keys != adj_keys:
        raise ValueError("unadjusted and backward-adjusted (date, symbol) key sets differ")
    joined = raw.join(adj, on=["date", "symbol"], how="inner", validate="1:1").cast(
        NUMERIC_SCHEMA, strict=False
    )
    return joined.select(CANONICAL_COLUMNS).sort(["date", "symbol"])
