from datetime import date
from pathlib import Path

import pytest

from ashare_multifactor.data.discovery import discover_daily_pairs
from ashare_multifactor.data.schema import CANONICAL_COLUMNS, SCHEMA_VERSION


def test_canonical_schema_contract():
    assert CANONICAL_COLUMNS == (
        "date",
        "symbol",
        "name",
        "industry",
        "open_raw",
        "high_raw",
        "low_raw",
        "close_raw",
        "prev_close_raw",
        "open_adj",
        "high_adj",
        "low_adj",
        "close_adj",
        "prev_close_adj",
        "volume",
        "amount",
        "turnover",
        "is_st",
        "is_limit_up",
        "total_shares",
        "float_shares",
        "total_market_cap",
        "float_market_cap",
        "pe_ttm",
        "pb",
        "ps_ttm",
        "list_date",
        "delist_date",
        "is_margin",
    )
    assert SCHEMA_VERSION == "1.0.0"


def test_discovery_requires_matching_dates(tmp_path: Path):
    raw = tmp_path / "raw"
    adj = tmp_path / "adj"
    raw.mkdir()
    adj.mkdir()
    (raw / "2014-01-02_金玥数据.csv").touch()

    with pytest.raises(ValueError, match="unpaired trading dates"):
        discover_daily_pairs(raw, adj, date(2014, 1, 1), date(2014, 12, 31))


def test_discovery_sorts_pairs_by_date(tmp_path: Path):
    raw = tmp_path / "raw"
    adj = tmp_path / "adj"
    raw.mkdir()
    adj.mkdir()
    for day in ("2014-01-03", "2014-01-02"):
        (raw / f"{day}_金玥数据.csv").touch()
        (adj / f"{day}_金玥数据.csv").touch()

    pairs = discover_daily_pairs(raw, adj, date(2014, 1, 1), date(2014, 12, 31))
    assert [pair.trading_date.isoformat() for pair in pairs] == [
        "2014-01-02",
        "2014-01-03",
    ]


def test_discovery_rejects_duplicate_files_for_same_date(tmp_path: Path):
    raw = tmp_path / "raw"
    adj = tmp_path / "adj"
    (raw / "nested").mkdir(parents=True)
    adj.mkdir()
    filename = "2014-01-02_金玥数据.csv"
    (raw / filename).touch()
    (raw / "nested" / filename).touch()
    (adj / filename).touch()

    with pytest.raises(ValueError, match="duplicate file for 2014-01-02"):
        discover_daily_pairs(raw, adj, date(2014, 1, 1), date(2014, 12, 31))
