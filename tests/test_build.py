import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
import polars as pl

from ashare_multifactor.config import (
    FactorResearchSettings,
    MvpSettings,
    Paths,
    Period,
    ResearchConfig,
)


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
        factor_research=FactorResearchSettings(
            data_start=date(2003, 1, 1),
            analysis_start=date(2005, 1, 1),
            analysis_end=date(2016, 12, 31),
            universe_size=1000,
            minimum_history=252,
            liquidity_lookback=20,
            require_valid_trade_observation=True,
            signal_frequency="month_end",
            forward_horizons=(5, 20, 60),
            primary_horizon=20,
            winsor_lower=0.01,
            winsor_upper=0.99,
            quantile_count=5,
            minimum_coverage=0.8,
            minimum_valid_months=120,
            fdr_q_threshold=0.1,
            redundancy_threshold=0.7,
        ),
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


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


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

    with pytest.raises(ValueError, match="all rows quarantined for 2014-01-02"):
        build_parquet_dataset(config, date(2014, 1, 2), date(2014, 1, 2))

    assert not (config.paths.processed / "daily_panel").exists()


def test_build_quarantines_only_invalid_ohlc_and_records_stable_warning(tmp_path: Path) -> None:
    from ashare_multifactor.data.build import build_parquet_dataset

    config = _config(tmp_path)
    day = date(2014, 1, 2)
    _write_pair(config, day)
    raw_path = next(config.paths.raw_unadjusted.glob("*.csv"))
    adj_path = next(config.paths.raw_backward_adjusted.glob("*.csv"))
    raw = raw_path.read_text(encoding="utf-8").splitlines()
    adj = adj_path.read_text(encoding="utf-8").splitlines()
    raw_bad = raw[1].replace("000001", "000002", 1).replace("10.5,9.8,10.2", "9.0,9.8,10.2", 1)
    adj_bad = adj[1].replace("000001", "000002", 1)
    raw_path.write_text("\n".join([raw[0], raw_bad, raw[1]]) + "\n", encoding="utf-8")
    adj_path.write_text("\n".join([adj[0], adj_bad, adj[1]]) + "\n", encoding="utf-8")

    manifest = build_parquet_dataset(config, day, day)

    panel = pl.read_parquet(config.paths.processed / "daily_panel/year=2014/part-000.parquet")
    assert manifest.rows == 1
    assert panel["symbol"].to_list() == ["000001"]
    assert json.loads((config.paths.processed / "daily_panel/quality_issues.json").read_text()) == [
        {
            "code": "invalid_ohlc_quarantined",
            "count": 1,
            "date": "2014-01-02",
            "message": "rows with non-positive or inconsistent raw/adjusted OHLC were quarantined",
            "severity": "warning",
            "symbols": ["000002"],
        }
    ]
    first_hashes = _tree_hashes(config.paths.processed / "daily_panel")

    build_parquet_dataset(config, day, day)

    assert _tree_hashes(config.paths.processed / "daily_panel") == first_hashes


def test_quarantine_does_not_allow_other_quality_errors(tmp_path: Path) -> None:
    from ashare_multifactor.data.build import build_parquet_dataset

    config = _config(tmp_path)
    _write_pair(config, date(2014, 1, 2), volume="invalid")

    with pytest.raises(ValueError, match="missing_trade_data:1"):
        build_parquet_dataset(config, date(2014, 1, 2), date(2014, 1, 2))

    assert not (config.paths.processed / "daily_panel").exists()


def test_invalid_ohlc_row_with_negative_trade_data_still_blocks(tmp_path: Path) -> None:
    from ashare_multifactor.data.build import build_parquet_dataset

    config = _config(tmp_path)
    _write_pair(config, date(2014, 1, 2), open_raw="-1.0", volume="-1")

    with pytest.raises(ValueError, match="negative_volume_amount:1"):
        build_parquet_dataset(config, date(2014, 1, 2), date(2014, 1, 2))


def test_duplicate_invalid_ohlc_rows_still_block(tmp_path: Path) -> None:
    from ashare_multifactor.data.build import build_parquet_dataset

    config = _config(tmp_path)
    _write_pair(config, date(2014, 1, 2), open_raw="-1.0")
    for root in (config.paths.raw_unadjusted, config.paths.raw_backward_adjusted):
        path = next(root.glob("*.csv"))
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join([lines[0], lines[1], lines[1]]) + "\n", encoding="utf-8")

    with pytest.raises(pl.exceptions.ComputeError, match="join keys did not fulfill 1:1 validation"):
        build_parquet_dataset(config, date(2014, 1, 2), date(2014, 1, 2))


def test_invalid_ohlc_row_with_date_mismatch_still_blocks(tmp_path: Path) -> None:
    from ashare_multifactor.data.build import build_parquet_dataset

    config = _config(tmp_path)
    _write_pair(config, date(2014, 1, 2), open_raw="-1.0")
    for root in (config.paths.raw_unadjusted, config.paths.raw_backward_adjusted):
        path = next(root.glob("*.csv"))
        path.write_text(
            path.read_text(encoding="utf-8").replace("2014-01-02", "2014-01-03"),
            encoding="utf-8",
        )

    with pytest.raises(ValueError, match="unadjusted CSV date must be 2014-01-02"):
        build_parquet_dataset(config, date(2014, 1, 2), date(2014, 1, 2))


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


def test_explicit_output_root_isolates_research_panel_from_mvp(tmp_path: Path) -> None:
    from ashare_multifactor.data.build import build_parquet_dataset

    config = _config(tmp_path)
    _write_pair(config, date(2005, 1, 4))
    mvp_root = config.paths.processed / "daily_panel"
    mvp_root.mkdir(parents=True)
    (mvp_root / "manifest.json").write_text('{"stage": "mvp"}\n', encoding="utf-8")
    before = _tree_hashes(mvp_root)
    research_root = config.paths.processed / "factor_research/daily_panel"

    manifest = build_parquet_dataset(
        config,
        date(2005, 1, 4),
        date(2005, 1, 4),
        output_root=research_root,
    )

    assert manifest.min_date == date(2005, 1, 4)
    assert (research_root / "year=2005/part-000.parquet").exists()
    assert _tree_hashes(mvp_root) == before


@pytest.mark.parametrize(
    "destination",
    ("mvp", "processed", "raw_unadjusted", "external"),
)
def test_explicit_output_root_rejects_every_non_research_destination_before_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination: str,
) -> None:
    import ashare_multifactor.data.build as build_module

    config = _config(tmp_path)
    mvp_root = config.paths.processed / "daily_panel"
    research_root = config.paths.processed / "factor_research/daily_panel"
    for root, marker in ((mvp_root, "mvp"), (research_root, "research")):
        root.mkdir(parents=True)
        (root / "old-marker").write_text(marker, encoding="utf-8")
    destinations = {
        "mvp": mvp_root,
        "processed": config.paths.processed,
        "raw_unadjusted": config.paths.raw_unadjusted,
        "external": tmp_path / "external/daily_panel",
    }
    attempted = destinations[destination]
    attempted.mkdir(parents=True, exist_ok=True)
    (attempted / "attempted-marker").write_text("unchanged", encoding="utf-8")
    processed_before = _tree_hashes(config.paths.processed)
    attempted_before = _tree_hashes(attempted)

    monkeypatch.setattr(
        build_module,
        "discover_daily_pairs",
        lambda *args, **kwargs: pytest.fail("invalid path reached raw discovery"),
    )

    with pytest.raises(ValueError, match="configured factor research daily panel"):
        build_module.build_parquet_dataset(
            config,
            date(2005, 1, 4),
            date(2005, 1, 4),
            output_root=attempted,
        )

    assert _tree_hashes(config.paths.processed) == processed_before
    assert _tree_hashes(attempted) == attempted_before


def test_explicit_output_root_rejects_symlink_escape_before_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ashare_multifactor.data.build as build_module

    config = _config(tmp_path)
    mvp_root = config.paths.processed / "daily_panel"
    mvp_root.mkdir(parents=True)
    (mvp_root / "old-marker").write_text("mvp", encoding="utf-8")
    outside_parent = tmp_path / "outside-factor-research"
    outside_target = outside_parent / "daily_panel"
    outside_target.mkdir(parents=True)
    (outside_target / "old-marker").write_text("research", encoding="utf-8")
    factor_link = config.paths.processed / "factor_research"
    factor_link.symlink_to(outside_parent, target_is_directory=True)
    mvp_before = _tree_hashes(mvp_root)
    outside_before = _tree_hashes(outside_target)

    monkeypatch.setattr(
        build_module,
        "discover_daily_pairs",
        lambda *args, **kwargs: pytest.fail("symlink escape reached raw discovery"),
    )

    with pytest.raises(ValueError, match="escapes configured processed root"):
        build_module.build_parquet_dataset(
            config,
            date(2005, 1, 4),
            date(2005, 1, 4),
            output_root=factor_link / "daily_panel",
        )

    assert _tree_hashes(mvp_root) == mvp_before
    assert _tree_hashes(outside_target) == outside_before


def test_explicit_output_root_rejects_factor_research_alias_to_processed_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ashare_multifactor.data.build as build_module

    config = _config(tmp_path)
    mvp_root = config.paths.processed / "daily_panel"
    mvp_root.mkdir(parents=True)
    (mvp_root / "old-marker").write_text("mvp", encoding="utf-8")
    factor_link = config.paths.processed / "factor_research"
    factor_link.symlink_to(config.paths.processed.resolve(), target_is_directory=True)
    before = _tree_hashes(mvp_root)
    monkeypatch.setattr(
        build_module,
        "discover_daily_pairs",
        lambda *args, **kwargs: pytest.fail("internal factor alias reached raw discovery"),
    )

    with pytest.raises(ValueError, match="factor_research path uses a symlink alias"):
        build_module.build_parquet_dataset(
            config,
            date(2005, 1, 4),
            date(2005, 1, 4),
            output_root=factor_link / "daily_panel",
        )

    assert _tree_hashes(mvp_root) == before


def test_explicit_output_root_rejects_daily_panel_alias_to_mvp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ashare_multifactor.data.build as build_module

    config = _config(tmp_path)
    mvp_root = config.paths.processed / "daily_panel"
    mvp_root.mkdir(parents=True)
    (mvp_root / "old-marker").write_text("mvp", encoding="utf-8")
    factor_root = config.paths.processed / "factor_research"
    factor_root.mkdir()
    research_link = factor_root / "daily_panel"
    research_link.symlink_to(mvp_root.resolve(), target_is_directory=True)
    before = _tree_hashes(mvp_root)
    monkeypatch.setattr(
        build_module,
        "discover_daily_pairs",
        lambda *args, **kwargs: pytest.fail("internal panel alias reached raw discovery"),
    )

    with pytest.raises(ValueError, match="daily_panel path uses a symlink alias"):
        build_module.build_parquet_dataset(
            config,
            date(2005, 1, 4),
            date(2005, 1, 4),
            output_root=research_link,
        )

    assert _tree_hashes(mvp_root) == before


def test_explicit_output_root_allows_configured_processed_root_symlink(
    tmp_path: Path,
) -> None:
    from ashare_multifactor.data.build import build_parquet_dataset

    config = _config(tmp_path)
    external_processed = tmp_path / "external-processed"
    external_processed.mkdir()
    config.paths.processed.symlink_to(external_processed, target_is_directory=True)
    _write_pair(config, date(2005, 1, 4))
    research_root = config.paths.processed / "factor_research/daily_panel"

    manifest = build_parquet_dataset(
        config,
        date(2005, 1, 4),
        date(2005, 1, 4),
        output_root=research_root,
    )

    assert manifest.rows == 1
    assert (external_processed / "factor_research/daily_panel/manifest.json").exists()


@pytest.mark.parametrize(
    "overlap",
    ("same", "processed_inside_raw", "raw_inside_processed"),
)
def test_build_rejects_processed_and_raw_path_overlap_before_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overlap: str,
) -> None:
    import ashare_multifactor.data.build as build_module

    config = _config(tmp_path)
    if overlap == "same":
        paths = replace(config.paths, processed=config.paths.raw_unadjusted)
    elif overlap == "processed_inside_raw":
        paths = replace(
            config.paths,
            processed=config.paths.raw_unadjusted / "processed",
        )
    else:
        paths = replace(
            config.paths,
            raw_unadjusted=config.paths.processed / "raw",
        )
    config = replace(config, paths=paths)
    monkeypatch.setattr(
        build_module,
        "discover_daily_pairs",
        lambda *args, **kwargs: pytest.fail("overlapping paths reached raw discovery"),
    )

    with pytest.raises(ValueError, match="processed path overlaps raw input"):
        build_module.build_parquet_dataset(
            config,
            date(2014, 1, 2),
            date(2014, 1, 2),
        )

    assert not (config.paths.processed / "daily_panel").exists()


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2002, 12, 31), date(2003, 1, 2)),
        (date(2016, 12, 30), date(2017, 1, 3)),
    ],
)
def test_explicit_output_root_rejects_dates_outside_factor_research_data(
    tmp_path: Path,
    start: date,
    end: date,
) -> None:
    from ashare_multifactor.data.build import build_parquet_dataset

    config = _config(tmp_path)

    with pytest.raises(ValueError, match="inside configured factor_research data period"):
        build_parquet_dataset(
            config,
            start,
            end,
            output_root=config.paths.processed / "factor_research/daily_panel",
        )


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


def test_publish_and_rollback_rename_failure_restores_copy_and_retains_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from ashare_multifactor.data.build import build_parquet_dataset

    config = _config(tmp_path)
    _write_pair(config, date(2014, 1, 2))
    target = config.paths.processed / "daily_panel"
    target.mkdir(parents=True)
    marker = target / "old-marker"
    marker.write_text("old", encoding="utf-8")
    real_replace = os.replace

    def fail_publish_and_rollback(source: Path, destination: Path) -> None:
        source_path = Path(source)
        if destination == target and source_path.name.endswith(".tmp"):
            raise OSError("simulated publish failure")
        if destination == target and source_path.name.endswith(".backup"):
            raise OSError("simulated rollback rename failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_publish_and_rollback)

    with pytest.raises(
        RuntimeError,
        match="rollback rename failed.*restored target from backup copy",
    ):
        build_parquet_dataset(config, date(2014, 1, 2), date(2014, 1, 2))

    assert marker.read_text(encoding="utf-8") == "old"
    assert list(target.iterdir()) == [marker]
    backups = list(config.paths.processed.glob(".daily_panel-*.backup"))
    assert len(backups) == 1
    assert (backups[0] / "old-marker").read_text(encoding="utf-8") == "old"
    assert not list(config.paths.processed.glob(".daily_panel-*.tmp"))


def test_successful_publish_ignores_cleanup_failure_and_retains_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    from ashare_multifactor.data.build import build_parquet_dataset

    config = _config(tmp_path)
    _write_pair(config, date(2014, 1, 2))
    target = config.paths.processed / "daily_panel"
    target.mkdir(parents=True)
    (target / "old-marker").write_text("old", encoding="utf-8")
    real_rmtree = shutil.rmtree

    def fail_backup_cleanup(path: Path, *args: object, **kwargs: object) -> None:
        if Path(path).name.endswith(".backup"):
            raise OSError("simulated backup cleanup failure")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", fail_backup_cleanup)

    manifest = build_parquet_dataset(config, date(2014, 1, 2), date(2014, 1, 2))

    assert manifest.rows == 1
    assert (target / "manifest.json").exists()
    assert not (target / "old-marker").exists()
    backups = list(config.paths.processed.glob(".daily_panel-*.backup"))
    assert len(backups) == 1
    assert (backups[0] / "old-marker").read_text(encoding="utf-8") == "old"
