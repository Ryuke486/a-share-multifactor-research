from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

import ashare_multifactor.research.mvp as mvp_module
from ashare_multifactor.config import Period, load_config
from ashare_multifactor.research.mvp import main, run_mvp
from ashare_multifactor.research.report import write_mvp_report


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
EXPECTED_ARTIFACTS = {
    "manifest.json",
    "data_quality.json",
    "rank_ic.csv",
    "target_weights.parquet",
    "trades.csv",
    "holdings.parquet",
    "blocked_orders.csv",
    "nav.csv",
    "gross_nav.csv",
    "nav.png",
    "drawdown.png",
    "summary.json",
    "report.md",
}


def _trading_dates(count: int) -> list[date]:
    days: list[date] = []
    current = date(2013, 1, 2)
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def _write_synthetic_daily_files(root: Path, days: list[date]) -> None:
    raw_root = root / "raw"
    adj_root = root / "adj"
    raw_root.mkdir()
    adj_root.mkdir()
    for day_index, day in enumerate(days):
        raw_lines = [",".join(RAW_COLUMNS)]
        adj_lines = [",".join(ADJ_COLUMNS)]
        for symbol_index in range(25):
            symbol = f"{symbol_index + 1:06d}"
            raw_close = 10.0 + symbol_index * 0.05 + day_index * 0.001
            raw_open = raw_close * 0.999
            raw_high = raw_close * 1.01
            raw_low = raw_open * 0.99
            raw_previous = raw_close - 0.001 if day_index else raw_close
            daily_rate = 0.0002 + symbol_index * 0.00002
            adj_close = 20.0 * (1.0 + daily_rate) ** day_index
            adj_open = adj_close * 0.999
            adj_high = adj_close * 1.01
            adj_low = adj_open * 0.99
            adj_previous = 20.0 * (1.0 + daily_rate) ** (day_index - 1) if day_index else adj_close
            raw_lines.append(
                ",".join(
                    (
                        day.isoformat(),
                        symbol,
                        f"合成股票{symbol}",
                        "合成行业",
                        f"{raw_open:.8f}",
                        f"{raw_high:.8f}",
                        f"{raw_low:.8f}",
                        f"{raw_close:.8f}",
                        f"{raw_previous:.8f}",
                        "100000",
                        str(1_000_000 + symbol_index * 10_000),
                        "1.0",
                        "否",
                        "否",
                        "100000000",
                        "80000000",
                        "1000000000",
                        "800000000",
                        "10.0",
                        "1.0",
                        "2.0",
                        "2000-01-01",
                        "-",
                        "是",
                    )
                )
            )
            adj_lines.append(
                ",".join(
                    (
                        day.isoformat(),
                        symbol,
                        f"{adj_open:.8f}",
                        f"{adj_high:.8f}",
                        f"{adj_low:.8f}",
                        f"{adj_close:.8f}",
                        f"{adj_previous:.8f}",
                    )
                )
            )
        filename = f"{day.isoformat()}_金玥数据.csv"
        (raw_root / filename).write_text("\n".join(raw_lines) + "\n", encoding="utf-8")
        (adj_root / filename).write_text("\n".join(adj_lines) + "\n", encoding="utf-8")


def _write_config(root: Path, analysis_end: date) -> Path:
    config_path = root / "research_protocol.yaml"
    config_path.write_text(
        f"""paths:
  raw_unadjusted: {root / "raw"}
  raw_backward_adjusted: {root / "adj"}
  processed: {root / "processed"}
  artifacts: {root / "artifacts"}

periods:
  research: [2005-01-01, 2016-12-31]
  validation: [2017-01-01, 2021-12-31]
  test: [2022-01-01, 2025-12-31]
  smoke_data: [2012-01-01, {analysis_end.isoformat()}]
  smoke_analysis: [2014-01-01, {analysis_end.isoformat()}]

mvp:
  universe_size: 25
  momentum_lookback: 60
  forward_horizon: 20
  portfolio_size: 20
  transaction_cost_bps: 10.0
  initial_cash: 1000000.0
""",
        encoding="utf-8",
    )
    return config_path


@pytest.fixture
def completed_mvp(tmp_path: Path) -> tuple[Path, Path, list[date]]:
    days = _trading_dates(320)
    _write_synthetic_daily_files(tmp_path, days)
    config_path = _write_config(tmp_path, days[-1])
    run_mvp(config_path)
    return tmp_path, config_path, days


def test_report_derives_windows_and_warmup_years_from_config(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, date(2014, 1, 31)))
    config = replace(
        config,
        smoke_data=Period(date(2013, 1, 1), config.smoke_data.end),
        mvp=replace(config.mvp, momentum_lookback=42, forward_horizon=7),
    )
    rank_ic = pl.DataFrame({"date": [date(2014, 1, 31)], "rank_ic": [0.1]})
    nav = pl.DataFrame(
        {
            "date": [date(2014, 1, 2), date(2014, 1, 3)],
            "nav": [1_000_000.0, 1_001_000.0],
            "drawdown": [0.0, 0.0],
        }
    )
    net_summary = {
        "total_return": 0.001,
        "max_drawdown": 0.0,
        "average_turnover": 1.0,
        "total_cost": 100.0,
        "blocked_order_count": 2.0,
        "stale_valuation_count": 3.0,
    }
    gross_summary = {**net_summary, "total_return": 0.0011, "total_cost": 0.0}

    write_mvp_report(
        config,
        rank_ic,
        nav,
        nav,
        net_summary,
        gross_summary,
        tmp_path / "report",
    )

    report = (tmp_path / "report/report.md").read_text(encoding="utf-8")
    assert "2013预热期" in report
    assert "2012–2013预热期" not in report
    assert "42日动量" in report
    assert "momentum_42 = close_adj[t] / close_adj[t-42] - 1" in report
    assert "7个交易观测后" in report
    assert "60日动量" not in report
    assert "20个交易观测后" not in report
    assert "拒单行数：2" in report
    assert "陈旧估值证券日数：3" in report
    assert "最近已知收盘价估值" in report
    assert "缺少开盘价" in report
    assert "不追单" in report
    assert "最小缺价处理" in report
    assert "统一最大可行资金基数" in report
    assert "完整模拟停牌" not in report


def test_all_pipeline_writes_auditable_outputs_and_stages_resume(
    completed_mvp: tuple[Path, Path, list[date]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path, config_path, days = completed_mvp

    artifact_root = tmp_path / "artifacts/mvp"
    processed_root = tmp_path / "processed/mvp"
    assert EXPECTED_ARTIFACTS <= {path.name for path in artifact_root.iterdir()}
    assert (processed_root / "signals.parquet").exists()
    assert (processed_root / "monthly_signals.parquet").exists()
    lineage = json.loads((processed_root / "lineage.json").read_text(encoding="utf-8"))
    assert lineage["schema_version"] == "1.0.0"
    assert len(lineage["fingerprint"]) == 64
    assert {
        "signals.parquet",
        "monthly_signals.parquet",
        "rank_ic.parquet",
        "target_weights.parquet",
        "net_nav.parquet",
        "gross_nav.parquet",
        "blocked_orders.parquet",
        "net_summary.json",
        "gross_summary.json",
    } <= set(lineage["artifacts"])
    for artifact in lineage["artifacts"].values():
        assert len(artifact["sha256"]) == 64
        assert artifact["size_bytes"] > 0

    signals = pl.read_parquet(processed_root / "signals.parquet")
    targets = pl.read_parquet(artifact_root / "target_weights.parquet")
    trades = pl.read_csv(artifact_root / "trades.csv", try_parse_dates=True)
    holdings = pl.read_parquet(artifact_root / "holdings.parquet")
    blocked_orders = pl.read_csv(artifact_root / "blocked_orders.csv", try_parse_dates=True)
    nav = pl.read_csv(artifact_root / "nav.csv", try_parse_dates=True)
    gross_nav = pl.read_csv(artifact_root / "gross_nav.csv", try_parse_dates=True)
    rank_ic = pl.read_csv(artifact_root / "rank_ic.csv", try_parse_dates=True)
    assert signals.height > 0
    assert targets.height > 0
    assert trades.height > 0
    assert holdings.height > 0
    assert blocked_orders.columns == [
        "signal_date",
        "execution_date",
        "symbol",
        "side",
        "reason",
        "target_weight",
        "units_held",
    ]
    assert nav.height > 0
    assert rank_ic.filter(pl.col("rank_ic").is_not_null()).height > 0

    for frame, columns in (
        (signals, ("date",)),
        (targets, ("signal_date", "execution_date")),
        (trades, ("signal_date", "execution_date")),
        (holdings, ("date",)),
        (nav, ("date",)),
        (gross_nav, ("date",)),
        (rank_ic, ("date",)),
    ):
        for column in columns:
            assert frame.get_column(column).max() <= days[-1]
    assert nav.get_column("date").min() >= date(2014, 1, 1)

    summary = json.loads((artifact_root / "summary.json").read_text(encoding="utf-8"))
    report = (artifact_root / "report.md").read_text(encoding="utf-8")
    assert summary["analysis"]["end"] == days[-1].isoformat()
    assert summary["rank_ic"]["valid_months"] > 0
    assert summary["net"]["total_cost"] > 0.0
    assert summary["net"]["blocked_order_count"] == blocked_orders.height
    assert (
        summary["net"]["stale_valuation_count"] == holdings.filter(pl.col("price_is_stale")).height
    )
    assert summary["gross"]["total_cost"] == 0.0
    assert f"{summary['gross']['total_return']:.6%}" in report
    assert f"{summary['net']['total_return']:.6%}" in report
    for phrase in (
        "Rank IC",
        "2012–2013",
        "10bp",
        "分数持仓",
        "涨跌停",
        "停牌",
        "历史费率",
        "最近已知收盘价估值",
        "缺少开盘价",
        "拒单",
        "不追单",
        "陈旧估值证券日数",
        "管道验证",
    ):
        assert phrase in report

    manifest = json.loads((artifact_root / "manifest.json").read_text(encoding="utf-8"))
    operation_names = [operation["name"] for operation in manifest["operations"]]
    assert operation_names == [
        "load_config",
        "build_parquet_dataset",
        "build_universe",
        "add_momentum_and_forward_return",
        "monthly_signal_panel",
        "rank_ic_by_date",
        "build_target_weights",
        "run_backtest",
        "run_backtest_gross",
        "write_mvp_report",
    ]
    for operation in manifest["operations"]:
        assert set(operation) == {"name", "input", "output"}
        for boundary in (operation["input"], operation["output"]):
            assert set(boundary) == {"rows", "min_date", "max_date"}
            if boundary["max_date"] is not None:
                assert date.fromisoformat(boundary["max_date"]) <= days[-1]

    data_manifest_path = tmp_path / "processed/daily_panel/manifest.json"
    data_manifest = json.loads(data_manifest_path.read_text(encoding="utf-8"))
    future_manifest = {**data_manifest, "max_date": "2022-01-04"}
    data_manifest_path.write_text(json.dumps(future_manifest), encoding="utf-8")
    try:
        with monkeypatch.context() as context:
            context.setattr(
                pl,
                "scan_parquet",
                lambda *args, **kwargs: pytest.fail("future manifest must fail before scan"),
            )
            with pytest.raises(ValueError, match="exceeds configured smoke_data end"):
                run_mvp(config_path, stage="signals")
    finally:
        data_manifest_path.write_text(json.dumps(data_manifest), encoding="utf-8")

    past_manifest = {**data_manifest, "min_date": "2011-12-30"}
    data_manifest_path.write_text(json.dumps(past_manifest), encoding="utf-8")
    try:
        with monkeypatch.context() as context:
            context.setattr(
                pl,
                "scan_parquet",
                lambda *args, **kwargs: pytest.fail("past manifest must fail before scan"),
            )
            with pytest.raises(ValueError, match="predates configured smoke_data start"):
                run_mvp(config_path, stage="signals")
    finally:
        data_manifest_path.write_text(json.dumps(data_manifest), encoding="utf-8")

    original_config = config_path.read_text(encoding="utf-8")
    changed_config = original_config.replace(
        "transaction_cost_bps: 10.0", "transaction_cost_bps: 20.0"
    )
    pipeline_manifest_path = artifact_root / "manifest.json"
    pipeline_manifest = pipeline_manifest_path.read_text(encoding="utf-8")
    pipeline_manifest_path.unlink()
    config_path.write_text(changed_config, encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="intermediate lineage does not match"):
            run_mvp(config_path, stage="report")
    finally:
        config_path.write_text(original_config, encoding="utf-8")
        pipeline_manifest_path.write_text(pipeline_manifest, encoding="utf-8")

    shutil.rmtree(tmp_path / "raw")
    shutil.rmtree(tmp_path / "adj")
    for stage in ("signals", "targets", "backtest", "report"):
        run_mvp(config_path, stage=stage)


def test_backtest_rejects_replaced_target_parquet(
    completed_mvp: tuple[Path, Path, list[date]],
) -> None:
    tmp_path, config_path, _ = completed_mvp
    target_path = tmp_path / "processed/mvp/target_weights.parquet"
    pl.read_parquet(target_path).reverse().write_parquet(target_path)

    with pytest.raises(ValueError, match="artifact digest mismatch"):
        run_mvp(config_path, stage="backtest")


def test_signals_rejects_replaced_or_missing_daily_panel_partition(
    completed_mvp: tuple[Path, Path, list[date]],
) -> None:
    tmp_path, config_path, _ = completed_mvp
    partition_path = tmp_path / "processed/daily_panel/year=2013/part-000.parquet"
    original = partition_path.read_bytes()
    altered = pl.read_parquet(partition_path).with_columns(
        (pl.col("close_adj") + 1.0).alias("close_adj")
    )
    altered.write_parquet(partition_path)

    with pytest.raises(ValueError, match="daily panel partition digest mismatch"):
        run_mvp(config_path, stage="signals")

    partition_path.write_bytes(original)
    partition_path.unlink()
    with pytest.raises(ValueError, match="daily panel partition file list mismatch"):
        run_mvp(config_path, stage="signals")


def test_report_rejects_replaced_summary_json(
    completed_mvp: tuple[Path, Path, list[date]],
) -> None:
    tmp_path, config_path, _ = completed_mvp
    summary_path = tmp_path / "processed/mvp/net_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["total_return"] += 0.01
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    with pytest.raises(ValueError, match="artifact digest mismatch"):
        run_mvp(config_path, stage="report")


def test_report_rejects_replaced_blocked_orders_parquet(
    completed_mvp: tuple[Path, Path, list[date]],
) -> None:
    tmp_path, config_path, days = completed_mvp
    blocked_path = tmp_path / "processed/mvp/blocked_orders.parquet"
    pl.DataFrame(
        {
            "signal_date": [days[-2]],
            "execution_date": [days[-1]],
            "symbol": ["000001"],
            "side": ["buy"],
            "reason": ["missing_open_price"],
            "target_weight": [0.05],
            "units_held": [0.0],
        }
    ).write_parquet(blocked_path)

    with pytest.raises(ValueError, match="artifact digest mismatch"):
        run_mvp(config_path, stage="report")


def test_rerunning_targets_invalidates_backtest_and_report(
    completed_mvp: tuple[Path, Path, list[date]],
) -> None:
    tmp_path, config_path, _ = completed_mvp

    run_mvp(config_path, stage="targets")

    for relative_path in (
        "processed/mvp/net_nav.parquet",
        "processed/mvp/gross_nav.parquet",
        "processed/mvp/trades.parquet",
        "processed/mvp/holdings.parquet",
        "processed/mvp/blocked_orders.parquet",
        "processed/mvp/net_summary.json",
        "processed/mvp/gross_summary.json",
        "artifacts/mvp/nav.csv",
        "artifacts/mvp/gross_nav.csv",
        "artifacts/mvp/trades.csv",
        "artifacts/mvp/holdings.parquet",
        "artifacts/mvp/blocked_orders.csv",
        "artifacts/mvp/nav.png",
        "artifacts/mvp/drawdown.png",
        "artifacts/mvp/summary.json",
        "artifacts/mvp/report.md",
    ):
        assert not (tmp_path / relative_path).exists()

    with pytest.raises(ValueError, match="missing required artifact"):
        run_mvp(config_path, stage="report")


def test_failed_signals_rerun_removes_all_stale_downstream_outputs(
    completed_mvp: tuple[Path, Path, list[date]],
) -> None:
    tmp_path, config_path, _ = completed_mvp
    partition_path = tmp_path / "processed/daily_panel/year=2013/part-000.parquet"
    partition_path.unlink()

    with pytest.raises(ValueError, match="daily panel partition file list mismatch"):
        run_mvp(config_path, stage="signals")

    assert not list((tmp_path / "processed/mvp").glob("*.parquet"))
    assert not list((tmp_path / "processed/mvp").glob("*_summary.json"))
    assert not (tmp_path / "artifacts/mvp/rank_ic.csv").exists()
    assert not (tmp_path / "artifacts/mvp/target_weights.parquet").exists()
    for name in (
        "nav.csv",
        "gross_nav.csv",
        "trades.csv",
        "holdings.parquet",
        "blocked_orders.csv",
        "nav.png",
        "drawdown.png",
        "summary.json",
        "report.md",
    ):
        assert not (tmp_path / "artifacts/mvp" / name).exists()


@pytest.mark.parametrize("stage", ["build", "all"])
def test_failed_build_start_removes_all_stale_mvp_outputs(
    completed_mvp: tuple[Path, Path, list[date]],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    tmp_path, config_path, _ = completed_mvp

    def fail_build(*args: object, **kwargs: object) -> object:
        raise OSError("simulated build failure")

    monkeypatch.setattr(mvp_module, "build_parquet_dataset", fail_build)

    with pytest.raises(OSError, match="simulated build failure"):
        run_mvp(config_path, stage=stage)

    assert not list((tmp_path / "processed/mvp").glob("*.parquet"))
    assert not list((tmp_path / "processed/mvp").glob("*_summary.json"))
    for name in EXPECTED_ARTIFACTS - {"manifest.json", "data_quality.json"}:
        assert not (tmp_path / "artifacts/mvp" / name).exists()


def test_rejected_stage_start_still_removes_stale_downstream_outputs(
    completed_mvp: tuple[Path, Path, list[date]],
) -> None:
    tmp_path, config_path, _ = completed_mvp
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "transaction_cost_bps: 10.0", "transaction_cost_bps: 20.0"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="manifest does not match current configuration"):
        run_mvp(config_path, stage="targets")

    assert not (tmp_path / "processed/mvp/target_weights.parquet").exists()
    assert not (tmp_path / "processed/mvp/net_nav.parquet").exists()
    assert not (tmp_path / "artifacts/mvp/target_weights.parquet").exists()
    assert not (tmp_path / "artifacts/mvp/report.md").exists()


def test_failed_target_publish_keeps_downstream_invalid(
    completed_mvp: tuple[Path, Path, list[date]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path, config_path, _ = completed_mvp
    real_write_frame = mvp_module._write_frame

    def fail_target_write(frame: pl.DataFrame, path: Path) -> None:
        if path == tmp_path / "artifacts/mvp/target_weights.parquet":
            raise OSError("simulated target publish failure")
        real_write_frame(frame, path)

    monkeypatch.setattr(mvp_module, "_write_frame", fail_target_write)
    with pytest.raises(OSError, match="simulated target publish failure"):
        run_mvp(config_path, stage="targets")
    monkeypatch.setattr(mvp_module, "_write_frame", real_write_frame)

    with pytest.raises(ValueError, match="missing required artifact"):
        run_mvp(config_path, stage="report")


def test_cli_rejects_an_unknown_stage(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--config", str(tmp_path / "missing.yaml"), "--stage", "invalid"])

    assert error.value.code != 0
