from datetime import date
from pathlib import Path

import pytest

from ashare_multifactor.data.discovery import discover_daily_pairs


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
