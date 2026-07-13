"""Thin stage orchestration and CLI for the auditable momentum MVP."""

from __future__ import annotations

import argparse
import logging
from dataclasses import replace
from pathlib import Path
from typing import Mapping

import polars as pl

from ashare_multifactor.config import ResearchConfig, load_config
from ashare_multifactor.data.build import build_parquet_dataset
from ashare_multifactor.research.backtest import BacktestResult, run_backtest
from ashare_multifactor.research.evaluation import monthly_signal_panel, rank_ic_by_date
from ashare_multifactor.research.lineage import (
    DailyPanelSource,
    discard_stage_outputs,
    invalidate_build,
    invalidate_stage,
    record_stage,
    require_lineage,
    validate_daily_panel_source,
)
from ashare_multifactor.research.momentum import add_momentum_and_forward_return
from ashare_multifactor.research.pipeline_audit import PipelineRecorder, publish_data_audit
from ashare_multifactor.research.pipeline_io import (
    date_range as _date_range,
    frame_stats as _stats,
    manual_stats as _manual_stats,
    read_daily_panel,
    read_intermediate as _read_intermediate,
    read_json as _read_json,
    write_frame as _write_frame,
    write_json as _write_json,
)
from ashare_multifactor.research.portfolio import build_target_weights
from ashare_multifactor.research.report import write_mvp_report
from ashare_multifactor.research.universe import build_universe


STAGES = ("build", "signals", "targets", "backtest", "report", "all")
LOGGER = logging.getLogger(__name__)


def _panel(
    config: ResearchConfig,
    source: DailyPanelSource,
    columns: list[str] | None = None,
) -> pl.DataFrame:
    return read_daily_panel(config, list(source.files), columns)


def _invalidate(
    config: ResearchConfig,
    recorder: PipelineRecorder,
    stage: str,
    source: DailyPanelSource,
) -> None:
    invalidate_stage(config, stage, source.manifest)
    recorder.invalidate_stage(stage)


def _run_build(config: ResearchConfig, recorder: PipelineRecorder) -> DailyPanelSource:
    name = "build_parquet_dataset"
    unknown_input = _manual_stats(None, config.smoke_data.start, config.smoke_data.end)
    recorder.start(name, unknown_input)
    manifest = build_parquet_dataset(config, config.smoke_data.start, config.smoke_data.end)
    invalidate_build(config)
    stats = _manual_stats(manifest.rows, manifest.min_date, manifest.max_date)
    recorder.finish(name, unknown_input, stats)
    panel_root = config.paths.processed / "daily_panel"
    source = DailyPanelSource(
        manifest=manifest.to_dict(),
        files=tuple(panel_root / partition.relative_path for partition in manifest.partitions),
    )
    publish_data_audit(config, recorder, source.manifest)
    return source


def _run_signals(
    config: ResearchConfig, recorder: PipelineRecorder, source: DailyPanelSource
) -> None:
    processed_root = config.paths.processed / "mvp"
    artifact_root = config.paths.artifacts / "mvp"
    _invalidate(config, recorder, "signals", source)
    panel = _panel(
        config,
        source,
        [
            "date",
            "symbol",
            "amount",
            "is_st",
            "open_raw",
            "high_raw",
            "low_raw",
            "close_raw",
            "close_adj",
        ],
    )
    name = "build_universe"
    recorder.start(name, _stats(panel))
    universe = build_universe(panel, config)
    recorder.finish(name, _stats(panel), _stats(universe))

    name = "add_momentum_and_forward_return"
    recorder.start(name, _stats(universe))
    signals = add_momentum_and_forward_return(universe, config)
    recorder.finish(name, _stats(universe), _stats(signals))

    name = "monthly_signal_panel"
    recorder.start(name, _stats(signals))
    monthly = monthly_signal_panel(signals)
    recorder.finish(name, _stats(signals), _stats(monthly))

    name = "rank_ic_by_date"
    recorder.start(name, _stats(monthly))
    rank_ic = rank_ic_by_date(monthly)
    recorder.finish(name, _stats(monthly), _stats(rank_ic))

    _write_frame(signals, processed_root / "signals.parquet")
    _write_frame(monthly, processed_root / "monthly_signals.parquet")
    _write_frame(rank_ic, processed_root / "rank_ic.parquet")
    _write_frame(rank_ic, artifact_root / "rank_ic.csv")
    record_stage(config, "signals", source.manifest)


def _run_targets(
    config: ResearchConfig, recorder: PipelineRecorder, source: DailyPanelSource
) -> None:
    processed_root = config.paths.processed / "mvp"
    artifact_root = config.paths.artifacts / "mvp"
    _invalidate(config, recorder, "targets", source)
    require_lineage(config, ("monthly_signals.parquet",), source.manifest)
    monthly = _read_intermediate(
        processed_root / "monthly_signals.parquet",
        config.smoke_analysis.start,
        config.smoke_analysis.end,
    )
    trading_dates = _panel(config, source, ["date", "symbol"]).select("date").unique()
    input_stats = _manual_stats(monthly.height + trading_dates.height)
    input_stats["min_date"] = min(
        monthly.get_column("date").min(), trading_dates.get_column("date").min()
    ).isoformat()
    input_stats["max_date"] = max(
        monthly.get_column("date").max(), trading_dates.get_column("date").max()
    ).isoformat()
    name = "build_target_weights"
    recorder.start(name, input_stats)
    targets = build_target_weights(monthly, trading_dates, config)
    recorder.finish(name, input_stats, _stats(targets))
    _write_frame(targets, processed_root / "target_weights.parquet")
    _write_frame(targets, artifact_root / "target_weights.parquet")
    record_stage(config, "targets", source.manifest)


def _save_backtest_result(
    result: BacktestResult,
    processed_root: Path,
    artifact_root: Path,
) -> None:
    _write_frame(result.nav, processed_root / "net_nav.parquet")
    _write_frame(result.trades, processed_root / "trades.parquet")
    _write_frame(result.holdings, processed_root / "holdings.parquet")
    _write_frame(result.blocked_orders, processed_root / "blocked_orders.parquet")
    _write_json(processed_root / "net_summary.json", result.summary)
    _write_frame(result.nav, artifact_root / "nav.csv")
    _write_frame(result.trades, artifact_root / "trades.csv")
    _write_frame(result.holdings, artifact_root / "holdings.parquet")
    _write_frame(result.blocked_orders, artifact_root / "blocked_orders.csv")


def _run_backtests(
    config: ResearchConfig, recorder: PipelineRecorder, source: DailyPanelSource
) -> None:
    processed_root = config.paths.processed / "mvp"
    artifact_root = config.paths.artifacts / "mvp"
    _invalidate(config, recorder, "backtest", source)
    require_lineage(config, ("target_weights.parquet",), source.manifest)
    prices = _panel(config, source, ["date", "symbol", "open_adj", "close_adj"])
    targets = _read_intermediate(
        processed_root / "target_weights.parquet",
        config.smoke_analysis.start,
        config.smoke_analysis.end,
    )
    input_stats = _manual_stats(prices.height + targets.height)
    minimum, maximum = _date_range(
        pl.concat(
            [
                prices.select("date"),
                targets.select("execution_date").rename({"execution_date": "date"}),
            ]
        )
    )
    input_stats["min_date"] = minimum
    input_stats["max_date"] = maximum
    name = "run_backtest"
    recorder.start(name, input_stats)
    net = run_backtest(prices, targets, config)
    recorder.finish(name, input_stats, _stats(net.nav))

    gross_config = replace(config, mvp=replace(config.mvp, transaction_cost_bps=0.0))
    name = "run_backtest_gross"
    recorder.start(name, input_stats)
    gross = run_backtest(prices, targets, gross_config)
    recorder.finish(name, input_stats, _stats(gross.nav))

    _save_backtest_result(net, processed_root, artifact_root)
    _write_frame(gross.nav, processed_root / "gross_nav.parquet")
    _write_json(processed_root / "gross_summary.json", gross.summary)
    _write_frame(gross.nav, artifact_root / "gross_nav.csv")
    record_stage(config, "backtest", source.manifest)


def _as_float_mapping(payload: Mapping[str, object]) -> dict[str, float]:
    return {key: float(value) for key, value in payload.items()}


def _run_report(
    config: ResearchConfig, recorder: PipelineRecorder, source: DailyPanelSource
) -> None:
    processed_root = config.paths.processed / "mvp"
    artifact_root = config.paths.artifacts / "mvp"
    _invalidate(config, recorder, "report", source)
    require_lineage(
        config,
        (
            "rank_ic.parquet",
            "net_nav.parquet",
            "gross_nav.parquet",
            "blocked_orders.parquet",
            "net_summary.json",
            "gross_summary.json",
        ),
        source.manifest,
    )
    rank_ic = _read_intermediate(
        processed_root / "rank_ic.parquet",
        config.smoke_analysis.start,
        config.smoke_analysis.end,
    )
    net_nav = _read_intermediate(
        processed_root / "net_nav.parquet",
        config.smoke_analysis.start,
        config.smoke_analysis.end,
    )
    gross_nav = _read_intermediate(
        processed_root / "gross_nav.parquet",
        config.smoke_analysis.start,
        config.smoke_analysis.end,
    )
    net_summary = _as_float_mapping(_read_json(processed_root / "net_summary.json"))
    gross_summary = _as_float_mapping(_read_json(processed_root / "gross_summary.json"))
    input_stats = _manual_stats(rank_ic.height + net_nav.height + gross_nav.height)
    minimum, maximum = _date_range(pl.concat([net_nav, gross_nav], how="vertical"))
    input_stats["min_date"] = minimum
    input_stats["max_date"] = maximum
    name = "write_mvp_report"
    recorder.start(name, input_stats)
    write_mvp_report(
        config,
        rank_ic,
        net_nav,
        gross_nav,
        net_summary,
        gross_summary,
        artifact_root,
    )
    recorder.finish(
        name,
        input_stats,
        _manual_stats(4, net_nav.get_column("date").min(), net_nav.get_column("date").max()),
    )
    record_stage(config, "report", source.manifest)


def run_mvp(config_path: Path, stage: str = "all") -> None:
    """Run one resumable stage, or the complete dependency chain with ``all``."""
    if stage not in STAGES:
        raise ValueError(f"invalid stage {stage!r}; expected one of {STAGES}")
    config_path = Path(config_path)
    LOGGER.info("load_config start input_rows=1 min_date=None max_date=None")
    config = load_config(config_path)
    artifact_root = config.paths.artifacts / "mvp"
    artifact_root.mkdir(parents=True, exist_ok=True)
    if stage in {"build", "all"}:
        discard_stage_outputs(config, "signals")
    elif stage in {"signals", "targets", "backtest", "report"}:
        discard_stage_outputs(config, stage)
    recorder = PipelineRecorder(
        artifact_root / "manifest.json",
        config_path,
        config,
        reset=stage in {"all", "build"},
    )
    config_input = _manual_stats(1)
    config_output = _manual_stats(1, config.smoke_data.start, config.smoke_data.end)
    recorder.finish("load_config", config_input, config_output)

    if stage in {"signals", "targets", "backtest", "report"}:
        recorder.invalidate_stage(stage)

    if stage in {"build", "all"}:
        source = _run_build(config, recorder)
    else:
        source = validate_daily_panel_source(config)
        publish_data_audit(config, recorder, source.manifest)
    if stage in {"signals", "all"}:
        _run_signals(config, recorder, source)
    if stage in {"targets", "all"}:
        _run_targets(config, recorder, source)
    if stage in {"backtest", "all"}:
        _run_backtests(config, recorder, source)
    if stage in {"report", "all"}:
        _run_report(config, recorder, source)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the auditable A-share momentum MVP")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--stage", choices=STAGES, default="all")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run_mvp(args.config, stage=args.stage)


if __name__ == "__main__":
    main()
