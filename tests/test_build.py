from datetime import date
import hashlib
import json
from pathlib import Path

import pytest

from ashare_multifactor.config import MvpSettings, Paths, Period, ResearchConfig


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


def _write_csv(path: Path, columns: tuple[str, ...], row: tuple[str, ...]) -> None:
    path.write_text(",".join(columns) + "\n" + ",".join(row), encoding="utf-8")


def _config(tmp_path: Path) -> ResearchConfig:
    return ResearchConfig(
        paths=Paths(
            raw_unadjusted=tmp_path / "raw",
            raw_backward_adjusted=tmp_path / "adj",
            processed=tmp_path / "processed",
            artifacts=tmp_path / "artifacts",
        ),
        research=Period(date(2005, 1, 1), date(2016, 12, 31)),
        validation=Period(date(2017, 1, 1), date(2021, 12, 31)),
        test=Period(date(2022, 1, 1), date(2025, 12, 31)),
        smoke_data=Period(date(2012, 1, 1), date(2015, 12, 31)),
        smoke_analysis=Period(date(2014, 1, 1), date(2015, 12, 31)),
        mvp=MvpSettings(200, 60, 20, 20, 10.0, 1_000_000.0),
    )


def _write_pair(
    config: ResearchConfig,
    day: date,
    *,
    open_raw: str = "10.0",
    volume: str = "1000",
    amount: str = "10000",
) -> None:
    config.paths.raw_unadjusted.mkdir(parents=True, exist_ok=True)
    config.paths.raw_backward_adjusted.mkdir(parents=True, exist_ok=True)
    name = f"{day.isoformat()}_金玥数据.csv"
    raw_row = (
        day.isoformat(), "000001", "平安银行", "银行", open_raw, "10.5", "9.8", "10.2",
        "9.9", volume, amount, "1.5", "否", "是", "100000", "80000", "1020000",
        "816000", "8.0", "1.1", "2.0", "1991-04-03", "-", "是",
    )
    adj_row = (day.isoformat(), "000001", "20.0", "21.0", "19.6", "20.4", "19.8")
    _write_csv(config.paths.raw_unadjusted / name, RAW_COLUMNS, raw_row)
    _write_csv(config.paths.raw_backward_adjusted / name, ADJ_COLUMNS, adj_row)


def test_builds_two_days_into_audited_year_partition(tmp_path: Path) -> None:
    from ashare_multifactor.data.build import build_parquet_dataset

    config = _config(tmp_path)
    _write_pair(config, date(2014, 1, 2))
    _write_pair(config, date(2014, 1, 3))

    manifest = build_parquet_dataset(config, date(2014, 1, 2), date(2014, 1, 3))

    assert manifest.file_pairs == 2
    assert manifest.min_date == date(2014, 1, 2)
    assert manifest.max_date == date(2014, 1, 3)
    assert (config.paths.processed / "daily_panel/year=2014/part-000.parquet").exists()
    saved = json.loads((config.paths.processed / "daily_panel/manifest.json").read_text())
    assert saved["min_date"] == "2014-01-02"
    assert saved["max_date"] == "2014-01-03"
    partition_path = config.paths.processed / "daily_panel/year=2014/part-000.parquet"
    assert saved["partitions"] == [
        {
            "max_date": "2014-01-03",
            "min_date": "2014-01-02",
            "relative_path": "year=2014/part-000.parquet",
            "rows": 2,
            "sha256": hashlib.sha256(partition_path.read_bytes()).hexdigest(),
            "size_bytes": partition_path.stat().st_size,
            "year": 2014,
        }
    ]
    assert json.loads(
        (config.paths.processed / "daily_panel/quality_issues.json").read_text()
    ) == []


def test_quality_error_leaves_no_official_dataset(tmp_path: Path) -> None:
    from ashare_multifactor.data.build import build_parquet_dataset

    config = _config(tmp_path)
    _write_pair(config, date(2014, 1, 2), open_raw="-1.0")

    with pytest.raises(ValueError, match="invalid_ohlc:1"):
        build_parquet_dataset(config, date(2014, 1, 2), date(2014, 1, 2))

    assert not (config.paths.processed / "daily_panel").exists()


@pytest.mark.parametrize("field", ["volume", "amount"])
def test_unparseable_trade_data_blocks_publish(tmp_path: Path, field: str) -> None:
    from ashare_multifactor.data.build import build_parquet_dataset

    config = _config(tmp_path)
    _write_pair(config, date(2014, 1, 2), **{field: "invalid"})

    with pytest.raises(ValueError, match="missing_trade_data:1"):
        build_parquet_dataset(config, date(2014, 1, 2), date(2014, 1, 2))

    assert not (config.paths.processed / "daily_panel").exists()


def test_build_rejects_dates_outside_configured_smoke_period(tmp_path: Path) -> None:
    from ashare_multifactor.data.build import build_parquet_dataset

    config = _config(tmp_path)

    with pytest.raises(ValueError, match="inside configured smoke_data period"):
        build_parquet_dataset(config, date(2015, 12, 31), date(2016, 1, 4))


def test_successful_publish_replaces_existing_dataset(tmp_path: Path) -> None:
    from ashare_multifactor.data.build import build_parquet_dataset

    config = _config(tmp_path)
    _write_pair(config, date(2014, 1, 2))
    target = config.paths.processed / "daily_panel"
    target.mkdir(parents=True)
    (target / "old-marker").write_text("old", encoding="utf-8")

    build_parquet_dataset(config, date(2014, 1, 2), date(2014, 1, 2))

    assert not (target / "old-marker").exists()
    assert (target / "manifest.json").exists()
    assert not list(config.paths.processed.glob(".daily_panel-*.tmp"))
    assert not list(config.paths.processed.glob(".daily_panel-*.backup"))


def test_publish_failure_restores_existing_dataset(tmp_path: Path, monkeypatch) -> None:
    import os

    from ashare_multifactor.data.build import build_parquet_dataset

    config = _config(tmp_path)
    _write_pair(config, date(2014, 1, 2))
    target = config.paths.processed / "daily_panel"
    target.mkdir(parents=True)
    marker = target / "old-marker"
    marker.write_text("old", encoding="utf-8")
    real_replace = os.replace

    def fail_staging_publish(source: Path, destination: Path) -> None:
        source_path = Path(source)
        if destination == target and source_path.name.endswith(".tmp"):
            raise OSError("simulated publish failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_staging_publish)

    with pytest.raises(OSError, match="simulated publish failure"):
        build_parquet_dataset(config, date(2014, 1, 2), date(2014, 1, 2))

    assert marker.read_text(encoding="utf-8") == "old"
    assert list(target.iterdir()) == [marker]
    assert not list(config.paths.processed.glob(".daily_panel-*.tmp"))
    assert not list(config.paths.processed.glob(".daily_panel-*.backup"))
