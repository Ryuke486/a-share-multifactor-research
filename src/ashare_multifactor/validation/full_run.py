from __future__ import annotations

from dataclasses import replace
import gc
import json
from pathlib import Path

import polars as pl

from ashare_multifactor.audit.publication import resolve_current
from ashare_multifactor.config import load_config
from ashare_multifactor.data.field_audit import FieldReadiness
from ashare_multifactor.execution.broker import BacktestSettings
from ashare_multifactor.execution.fees import load_market_rules
from ashare_multifactor.execution.shadow_nav import shadow_nav_audit
from ashare_multifactor.execution.stale_audit import audit_stale_positions
from ashare_multifactor.factors.definitions import FACTOR_DEFINITIONS
from ashare_multifactor.research.combination_evaluation import evaluate_combinations
from ashare_multifactor.research.factor_evaluation import evaluate_factors
from ashare_multifactor.validation.backtest_extension import run_validation_backtest
from ashare_multifactor.validation.backtest_inputs import (
    build_continuous_targets,
    extend_execution_panel,
)
from ashare_multifactor.validation.combination_extension import (
    build_validation_combinations,
)
from ashare_multifactor.validation.corporate_action_source import (
    load_official_action_corrections,
    load_official_payment_date_overrides,
    load_official_security_events,
)
from ashare_multifactor.validation.corporate_actions import (
    load_validation_corporate_actions,
)
from ashare_multifactor.validation.factor_extension import (
    build_validation_factor_extension,
)
from ashare_multifactor.validation.metrics import validation_portfolio_metrics
from ashare_multifactor.validation.portfolio_extension import build_validation_targets
from ashare_multifactor.validation.report import render_validation_report
from ashare_multifactor.validation.selection import select_validation_candidate


_CANDIDATE_SPEC = {
    "family_equal_size_stratified_buffered": (
        "family_equal",
        "size_stratified_buffered",
    ),
    "rolling_ic_family_size_stratified_buffered": (
        "rolling_ic_family",
        "size_stratified_buffered",
    ),
    "family_equal_top100_equal": ("family_equal", "top100_equal"),
}


def execute_validation_stages(code_root: Path, data_root: Path, run_root: Path) -> None:
    """Recompute Stage 7 from frozen daily-panel and immutable release inputs."""
    config = load_config(code_root / "configs/research_protocol.yaml")
    factor = config.factor_research
    combination = config.factor_combination
    portfolio = config.portfolio_construction
    formal = config.formal_backtest
    validation = config.validation_evaluation
    if any(item is None for item in (factor, combination, portfolio, formal, validation)):
        raise ValueError("stage seven requires factor, combination, portfolio, and backtest settings")
    datasets = run_root / "datasets"
    artifacts = run_root / "artifacts"
    datasets.mkdir(parents=True)
    artifacts.mkdir()

    daily = _load_factor_daily_panel(data_root)
    readiness = _load_readiness(data_root / "processed/factor_research/data_readiness.json")
    factor_settings = replace(
        factor,
        analysis_start=validation.analysis_start,
        analysis_end=validation.analysis_end,
    )
    extension = build_validation_factor_extension(daily, readiness, factor_settings)
    extension.features.write_parquet(datasets / "factor_features.parquet")
    extension.forward_returns.write_parquet(datasets / "forward_returns.parquet")
    extension.panel.write_parquet(datasets / "factor_panel.parquet")
    extension.audit.write_parquet(artifacts / "factor_audit.parquet")
    factor_evaluation = evaluate_factors(
        extension.panel,
        FACTOR_DEFINITIONS,
        factor_settings,
    )
    factor_evaluation.rank_ic.write_parquet(artifacts / "factor_rank_ic.parquet")
    factor_evaluation.quantile_returns.write_parquet(
        artifacts / "factor_quantile_returns.parquet"
    )
    factor_evaluation.factor_summary.write_parquet(artifacts / "factor_summary.parquet")
    factor_evaluation.factor_turnover.write_parquet(
        artifacts / "factor_turnover.parquet"
    )

    research = data_root / "processed/factor_research"
    historical_returns = pl.read_parquet(research / "forward_returns.parquet")
    all_returns = pl.concat(
        (historical_returns, extension.forward_returns), how="vertical_relaxed"
    )
    combinations = build_validation_combinations(
        extension.panel,
        pl.read_parquet(research / "factor_classifications.parquet"),
        pl.read_parquet(research / "rank_ic.parquet"),
        factor_evaluation.rank_ic,
        _realization_dates(all_returns),
        minimum_families=combination.minimum_families,
        window_months=combination.ic_window_months,
        minimum_months=combination.ic_minimum_months,
        shrinkage=combination.ic_shrinkage,
    )
    combinations.scores.write_parquet(datasets / "composite_scores.parquet")
    combinations.weights.write_parquet(datasets / "composite_weights.parquet")
    combinations.audit.write_parquet(artifacts / "combination_audit.parquet")
    composite_evaluation = evaluate_combinations(
        combinations.scores,
        extension.forward_returns,
        extension.panel,
        subperiod_boundaries=(("2017-2021", 2017, 2021),),
    )
    composite_evaluation.ic.write_parquet(artifacts / "composite_rank_ic.parquet")
    composite_evaluation.quantiles.write_parquet(
        artifacts / "composite_quantile_returns.parquet"
    )
    composite_evaluation.subperiods.write_parquet(
        artifacts / "composite_subperiods.parquet"
    )
    composite_evaluation.summary.write_parquet(artifacts / "composite_summary.parquet")
    composite_summary = composite_evaluation.summary

    stage_five = resolve_current(data_root / "processed/factor_combination")
    research_targets = pl.read_parquet(stage_five.datasets / "target_weights.parquet")
    previous_targets = research_targets.filter(
        (pl.col("portfolio_name") == "size_stratified_buffered")
        & (pl.col("date") == pl.col("date").max())
    )
    log_size = extension.features.filter(
        pl.col("factor_name") == "log_market_cap"
    ).select(
        "date",
        "symbol",
        pl.col("raw_value").alias("log_market_cap"),
    )
    target_frames: list[pl.DataFrame] = []
    diagnostic_frames: list[pl.DataFrame] = []
    for candidate in validation.candidates:
        method, portfolio_name = _CANDIDATE_SPEC[candidate]
        built = build_validation_targets(
            combinations.scores,
            log_size,
            method=method,
            portfolio_name=portfolio_name,
            previous_targets=previous_targets,
            portfolio_size=portfolio.portfolio_size,
            size_groups=portfolio.size_groups,
            per_group=portfolio.per_group,
            buffer_rank=portfolio.buffer_rank,
        )
        targets = built.targets.with_columns(pl.lit(candidate).alias("candidate"))
        diagnostics = built.diagnostics.with_columns(
            pl.lit(candidate).alias("candidate")
        )
        targets.write_parquet(datasets / f"target_weights_{candidate}.parquet")
        diagnostics.write_parquet(
            artifacts / f"target_diagnostics_{candidate}.parquet"
        )
        target_frames.append(targets)
        diagnostic_frames.append(diagnostics)
    validation_targets = pl.concat(target_frames).sort("candidate", "date", "symbol")
    target_diagnostics = pl.concat(diagnostic_frames).sort("candidate", "date")
    validation_targets.write_parquet(datasets / "target_weights.parquet")
    target_diagnostics.write_parquet(artifacts / "target_diagnostics.parquet")

    stage_six = resolve_current(data_root / "processed/formal_backtest")
    research_execution = pl.read_parquet(stage_six.datasets / "execution_panel.parquet")
    research_actions = pl.read_parquet(stage_six.datasets / "corporate_actions.parquet")
    research_events = pl.read_parquet(stage_six.datasets / "security_events.parquet")
    continuous: dict[str, pl.DataFrame] = {}
    for candidate in validation.candidates:
        frame = build_continuous_targets(
            research_targets,
            validation_targets,
            candidate=candidate,
        )
        frame.write_parquet(datasets / f"continuous_targets_{candidate}.parquet")
        continuous[candidate] = frame
    source_cache = data_root / "artifacts/validation_evaluation/source_cache"
    official_cache = source_cache / "cninfo_official_pdfs"
    validation_events = load_official_security_events(
        code_root / "configs/validation_security_events.csv",
        official_cache,
    )
    events = pl.concat((research_events, validation_events), how="vertical_relaxed").sort(
        "effective_date", "source_symbol"
    )
    event_symbols = set(events.get_column("source_symbol").drop_nulls()) | set(
        events.get_column("target_symbol").drop_nulls()
    )
    symbols = sorted(
        event_symbols
        | set().union(
            *(set(frame.get_column("symbol")) for frame in continuous.values())
        )
    )
    raw_extension = daily.filter(
        (pl.col("date") >= pl.date(2016, 11, 1)) & pl.col("symbol").is_in(symbols)
    ).select(
        "date",
        "symbol",
        "open_raw",
        "close_raw",
        "prev_close_raw",
        "close_adj",
        "prev_close_adj",
        "amount",
        "is_st",
    )
    execution = extend_execution_panel(
        research_execution,
        raw_extension,
        adv_lookback=formal.adv_lookback,
    )
    execution.write_parquet(datasets / "execution_panel.parquet")

    payments = load_official_payment_date_overrides(
        code_root / "configs/validation_corporate_action_evidence.csv",
        official_cache,
    )
    corrections = load_official_action_corrections(
        code_root / "configs/validation_corporate_action_corrections.csv",
        code_root / "configs/evidence/validation_action_corrections",
    )
    actions = load_validation_corporate_actions(
        research_actions,
        source_cache
        / "baostock_dividend/baostock_dividends_execution_union.parquet",
        symbols=symbols,
        payment_date_overrides=payments,
        action_corrections=corrections,
    )
    actions.write_parquet(datasets / "corporate_actions.parquet")
    events.write_parquet(datasets / "security_events.parquet")

    del (
        all_returns,
        combinations,
        composite_evaluation,
        daily,
        extension,
        factor_evaluation,
        historical_returns,
        raw_extension,
        validation_targets,
    )
    gc.collect()

    fees = load_market_rules(code_root / "configs/market_rules.yaml")
    backtest_settings = BacktestSettings(
        initial_cash=formal.initial_cash,
        maximum_participation=formal.maximum_participation,
        fixed_slippage_bps=formal.fixed_slippage_bps,
        reference_impact_bps=formal.reference_impact_bps,
        reference_participation=formal.reference_participation,
        maximum_impact_bps=formal.maximum_impact_bps,
        buy_lot_size=formal.buy_lot_size,
        analysis_end=validation.analysis_end,
    )
    metric_rows: list[dict[str, object]] = []
    for candidate in validation.candidates:
        result = run_validation_backtest(
            execution,
            continuous[candidate],
            actions,
            fees,
            backtest_settings,
            events,
        )
        candidate_root = run_root / "backtests" / candidate
        candidate_datasets = candidate_root / "datasets"
        candidate_artifacts = candidate_root / "artifacts"
        candidate_datasets.mkdir(parents=True)
        candidate_artifacts.mkdir()
        for name, frame in result.items():
            frame.write_parquet(candidate_datasets / f"{name}.parquet")
        shadow, shadow_daily = shadow_nav_audit(
            execution,
            actions,
            result["positions"],
            result["nav"],
            threshold=formal.shadow_divergence_threshold,
        )
        stale, stale_intervals = audit_stale_positions(
            result["positions"],
            result["nav"],
            execution,
            events,
            pl.DataFrame(schema={"symbol": pl.String}),
            review_days=formal.stale_review_days,
        )
        if shadow["status"] != "ready" or stale["status"] != "ready":
            raise ValueError(f"validation audit failed: {candidate}")
        _write_json(candidate_artifacts / "shadow_nav_audit.json", shadow)
        shadow_daily.write_parquet(candidate_artifacts / "shadow_nav_daily.parquet")
        _write_json(candidate_artifacts / "stale_audit.json", stale)
        stale_intervals.write_parquet(candidate_artifacts / "stale_intervals.parquet")
        diagnostics = target_diagnostics.filter(pl.col("candidate") == candidate)
        metric_rows.append(
            validation_portfolio_metrics(candidate, result, diagnostics)
        )
        del result, shadow_daily, stale_intervals
        gc.collect()

    metrics = _attach_composite_metrics(
        pl.DataFrame(metric_rows), composite_summary
    )
    metrics.write_parquet(artifacts / "validation_metrics.parquet")
    decision = select_validation_candidate(metrics, validation)
    _write_json(artifacts / "selection_decision.json", decision)
    (artifacts / "report.md").write_text(
        render_validation_report(metrics, decision), encoding="utf-8"
    )


def _load_factor_daily_panel(root: Path) -> pl.DataFrame:
    paths = [
        root / f"processed/factor_research/daily_panel/year={year}/part-000.parquet"
        for year in range(2003, 2017)
    ] + [
        root
        / f"processed/validation_evaluation/daily_panel/year={year}/part-000.parquet"
        for year in range(2017, 2022)
    ]
    if any(not path.is_file() for path in paths):
        raise FileNotFoundError("validation daily-panel input is incomplete")
    return pl.scan_parquet(paths).collect()


def _load_readiness(path: Path) -> FieldReadiness:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return FieldReadiness(**payload)


def _realization_dates(returns: pl.DataFrame) -> pl.DataFrame:
    return returns.group_by("date").agg(
        (
            pl.col("date").first()
            + pl.duration(days=pl.col("forward_calendar_days_20").max())
        ).alias("realization_date")
    )


def _attach_composite_metrics(
    portfolio_metrics: pl.DataFrame,
    composite_summary: pl.DataFrame,
) -> pl.DataFrame:
    mapping = pl.DataFrame(
        {
            "candidate": list(_CANDIDATE_SPEC),
            "method": [value[0] for value in _CANDIDATE_SPEC.values()],
        }
    )
    research = composite_summary.filter(pl.col("horizon") == 20).select(
        "method",
        pl.col("mean_ic").alias("rank_ic"),
        "icir",
        "monotonicity",
    )
    return portfolio_metrics.join(mapping, on="candidate", how="left").join(
        research,
        on="method",
        how="left",
        validate="m:1",
    ).drop("method").sort("candidate")


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
