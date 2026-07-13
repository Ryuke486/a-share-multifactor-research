from datetime import date
from pathlib import Path

import polars as pl
import pytest

from ashare_multifactor.data.discovery import DailyFilePair
from ashare_multifactor.data.schema import CANONICAL_COLUMNS


RAW_COLUMNS = (
    "日期",
    "代码",
    "名称",
    "所属行业",
    "开盘价",
    "最高价",
    "最低价",
    "收盘价",
    "前收盘价",
    "成交量（股）",
    "成交额（元）",
    "换手率",
    "是否ST",
    "是否涨停",
    "总股本（股）",
    "流通股本（股）",
    "总市值（元）",
    "流通市值（元）",
    "滚动市盈率",
    "市净率",
    "滚动市销率",
    "上市时间",
    "退市时间",
    "是否融资融券",
)
ADJ_COLUMNS = ("日期", "代码", "开盘价", "最高价", "最低价", "收盘价", "前收盘价")


def _raw_row(symbol: str = "000001") -> tuple[str, ...]:
    return (
        "2014-01-02",
        symbol,
        "平安银行",
        "银行",
        "10.0",
        "10.5",
        "9.8",
        "10.2",
        "9.9",
        "1000",
        "10000",
        "1.5",
        "否",
        "是",
        "100000",
        "80000",
        "1020000",
        "816000",
        "8.0",
        "1.1",
        "2.0",
        "1991-04-03",
        "-",
        "是",
    )


def _adj_row(symbol: str = "000001", open_price: str = "20.0") -> tuple[str, ...]:
    return ("2014-01-02", symbol, open_price, "21.0", "19.6", "20.4", "19.8")


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    lines = [",".join(columns), *(",".join(row) for row in rows)]
    path.write_text("\n".join(lines), encoding="utf-8")


def _pair(
    tmp_path: Path, raw_rows: list[tuple[str, ...]], adj_rows: list[tuple[str, ...]]
) -> DailyFilePair:
    raw_path = tmp_path / "raw.csv"
    adj_path = tmp_path / "adj.csv"
    _write_csv(raw_path, RAW_COLUMNS, raw_rows)
    _write_csv(adj_path, ADJ_COLUMNS, adj_rows)
    return DailyFilePair(date(2014, 1, 2), raw_path, adj_path)


def test_reader_preserves_six_digit_symbol_and_canonical_column_order(tmp_path: Path):
    from ashare_multifactor.data.reader import read_daily_pair

    frame = read_daily_pair(_pair(tmp_path, [_raw_row()], [_adj_row()]))

    assert frame.row(0, named=True)["symbol"] == "000001"
    assert frame.columns == list(CANONICAL_COLUMNS)


def test_reader_converts_chinese_yes_no_flags_to_booleans(tmp_path: Path):
    from ashare_multifactor.data.reader import read_daily_pair

    row = read_daily_pair(_pair(tmp_path, [_raw_row()], [_adj_row()])).row(0, named=True)

    assert row["is_st"] is False
    assert row["is_limit_up"] is True
    assert row["is_margin"] is True


def test_reader_converts_dash_delist_date_to_null(tmp_path: Path):
    from ashare_multifactor.data.reader import read_daily_pair

    row = read_daily_pair(_pair(tmp_path, [_raw_row()], [_adj_row()])).row(0, named=True)

    assert row["delist_date"] is None


def test_reader_adds_null_margin_for_37_column_schema(tmp_path: Path):
    from ashare_multifactor.data.reader import read_daily_pair

    pair = _pair(tmp_path, [_raw_row()], [_adj_row()])
    _write_csv(pair.unadjusted, RAW_COLUMNS[:-1], [_raw_row()[:-1]])

    frame = read_daily_pair(pair)

    assert frame["is_margin"].dtype == pl.Boolean
    assert frame.row(0, named=True)["is_margin"] is None


def test_reader_joins_adjusted_prices_by_date_and_symbol(tmp_path: Path):
    from ashare_multifactor.data.reader import read_daily_pair

    pair = _pair(
        tmp_path,
        [_raw_row("000002"), _raw_row("000001")],
        [_adj_row("000001", "21.0"), _adj_row("000002", "22.0")],
    )

    frame = read_daily_pair(pair)

    assert frame.select("symbol", "open_adj").rows() == [("000001", 21.0), ("000002", 22.0)]


def test_reader_rejects_mismatched_symbol_sets(tmp_path: Path):
    from ashare_multifactor.data.reader import read_daily_pair

    pair = _pair(tmp_path, [_raw_row("000001")], [_adj_row("000002")])

    with pytest.raises(ValueError, match="symbol sets differ"):
        read_daily_pair(pair)


def test_reader_keeps_numeric_types_when_values_are_missing(tmp_path: Path):
    from ashare_multifactor.data.reader import read_daily_pair

    raw_row = list(_raw_row())
    for index in range(4, 21):
        raw_row[index] = "-"
    adj_row = list(_adj_row())
    for index in range(2, 7):
        adj_row[index] = "-"

    frame = read_daily_pair(_pair(tmp_path, [tuple(raw_row)], [tuple(adj_row)]))

    assert frame.schema["open_raw"] == pl.Float64
    assert frame.schema["open_adj"] == pl.Float64
    assert frame.schema["volume"] == pl.Int64
