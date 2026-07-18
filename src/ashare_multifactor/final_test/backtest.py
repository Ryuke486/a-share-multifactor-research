from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path

import polars as pl

from ashare_multifactor.audit.publication import resolve_current
from ashare_multifactor.config import FormalBacktestSettings
from ashare_multifactor.data.security import assert_supported_markets, market_for_symbol
from ashare_multifactor.execution.broker import BacktestSettings, run_backtest
from ashare_multifactor.execution.fees import FeeSchedule, load_market_rules
from ashare_multifactor.execution.market_state import build_execution_panel
from ashare_multifactor.execution.shadow_nav import shadow_nav_audit
from ashare_multifactor.execution.stale_audit import audit_stale_positions
from ashare_multifactor.final_test.data_extension import (
    _verify_authorization as _verify_data_authorization,
)
from ashare_multifactor.final_test.action_source_contract import (
    verify_execution_input_manifest,
)
from ashare_multifactor.final_test.execution_contracts import (
    normalize_corporate_action_rows,
    normalize_security_event_rows,
)
from ashare_multifactor.final_test.gate import (
    FINAL_TEST_END,
    FINAL_TEST_START,
    FinalTestAuthorization,
)
from ashare_multifactor.final_test.signals import (
    MAIN_CANDIDATE,
    FinalTestSignals,
    _load_frozen_config,
    _resolve_authorized_data,
)
from ashare_multifactor.final_test.signal_inputs import (
    _release_file,
    resolve_final_test_signal_inputs,
)


_RECONCILIATION_THRESHOLD = 0.01
_EXECUTION_INPUT_MANIFEST = "execution_inputs/manifest.json"


@dataclass(frozen=True)
class FinalTestBacktestResult:
    outputs: dict[str, pl.DataFrame]
    audits: dict[str, object]
    preflight: dict[str, object]
    publishable: bool
    gate_failures: tuple[str, ...]


@dataclass(frozen=True)
class FinalTestBacktestInputs:
    execution_panel: pl.DataFrame
    target_weights: pl.DataFrame
    corporate_actions: pl.DataFrame
    security_events: pl.DataFrame
    stale_evidence: pl.DataFrame


@dataclass(frozen=True)
class _PretestExecutionInputs:
    execution_panel: pl.DataFrame
    target_weights: pl.DataFrame
    corporate_actions: pl.DataFrame
    security_events: pl.DataFrame
    warmup_daily: pl.DataFrame
    adv_lookback: int


def run_final_test_backtest(
    authorization: FinalTestAuthorization,
    signals: FinalTestSignals,
    *,
    code_root: Path,
    final_root: Path,
) -> FinalTestBacktestResult:
    """Fail closed until the sealed fee and execution-input contracts are complete."""
    if not isinstance(authorization, FinalTestAuthorization):
        raise TypeError("authorization must be a FinalTestAuthorization")
    if authorization.test_period != (FINAL_TEST_START, FINAL_TEST_END):
        raise ValueError("authorization period differs from the sealed final-test period")
    if not isinstance(signals, FinalTestSignals):
        raise TypeError("signals must be FinalTestSignals from Task 3")
    code_root = code_root.resolve()
    final_root = final_root.resolve()
    config = _load_frozen_config(code_root, final_root.parent.parent)
    _validate_final_targets(
        signals.target_weights, supported_markets=config.supported_markets
    )
    if final_root != config.paths.processed / "final_test":
        raise ValueError("final-test root is not canonical")
    _verify_data_authorization(config, authorization, code_root)

    failures = _fee_coverage_failures(code_root / "configs/market_rules.yaml")
    failures.extend(_execution_input_failures(final_root, authorization))
    if failures:
        return _blocked_preflight(failures)

    # Resolve test-period data only after every frozen fee/action preflight passes.
    source = _resolve_authorized_data(final_root, authorization, config.test)
    try:
        inputs = _resolve_verified_inputs(
            source,
            signals,
            authorization=authorization,
            code_root=code_root,
            final_root=final_root,
            supported_markets=config.supported_markets,
        )
    except (FileNotFoundError, ValueError) as error:
        return _blocked_preflight([str(error)])
    fees = load_market_rules(code_root / "configs/market_rules.yaml")
    if config.formal_backtest is None:
        return _blocked_preflight(["frozen formal-backtest settings are missing"])
    return _execute_verified_inputs(inputs, fees=fees, formal=config.formal_backtest)


def _validate_final_targets(
    targets: pl.DataFrame, *, supported_markets: tuple[str, ...]
) -> None:
    required = {"date", "candidate", "symbol", "target_weight"}
    if not required.issubset(targets.columns) or targets.is_empty():
        raise ValueError("Task 3 final targets are empty or have an invalid schema")
    if set(targets.get_column("candidate")) != {MAIN_CANDIDATE}:
        raise ValueError("Task 3 targets substitute the sealed main candidate")
    if targets.filter(
        ~pl.col("symbol")
        .cast(pl.String)
        .str.zfill(6)
        .map_elements(market_for_symbol, return_dtype=pl.String)
        .is_in(supported_markets)
    ).height:
        raise ValueError("Task 3 final targets escape supported market scope")
    if targets.filter(
        ~pl.col("date").is_between(FINAL_TEST_START, FINAL_TEST_END)
    ).height:
        raise ValueError("Task 3 targets cross the exact final-test date bounds")
    if targets.select(pl.struct("date", "symbol").is_duplicated().any()).item():
        raise ValueError("Task 3 final targets contain duplicate keys")
    totals = targets.group_by("date").agg(
        pl.col("target_weight").sum().alias("weight")
    )
    if (
        targets.filter(pl.col("target_weight") < 0).height
        or totals.filter((pl.col("weight") - 1.0).abs() > 1e-12).height
    ):
        raise ValueError("Task 3 final targets fail the frozen weight contract")


def _fee_coverage_failures(rules_path: Path) -> list[str]:
    try:
        fees = load_market_rules(rules_path)
        for boundary in (FINAL_TEST_START, FINAL_TEST_END):
            fees.stamp_duty_rate(boundary, "buy")
            fees.stamp_duty_rate(boundary, "sell")
            fees.transfer_fee(boundary, "sh", 10_000.0, 1_000)
            fees.transfer_fee(boundary, "sz", 10_000.0, 1_000)
    except (FileNotFoundError, KeyError, TypeError, ValueError) as error:
        return [f"frozen fee coverage does not span 2022-2025: {error}"]
    return []


def _execution_input_failures(
    final_root: Path,
    authorization: FinalTestAuthorization,
) -> list[str]:
    manifest_path = _execution_manifest_path(final_root, authorization)
    if not manifest_path.is_file():
        return [
            "authoritative 2022-2025 corporate-action inputs are missing",
            "authoritative 2022-2025 security-event inputs are missing",
        ]
    try:
        # Attempt inputs are written directly from the already-validated coverage
        # snapshot. Their own manifest, rather than the mutable audit archive,
        # is the execution-time authority.
        verify_execution_input_manifest(manifest_path, authorization)
    except (FileNotFoundError, TypeError, ValueError) as error:
        return [str(error)]
    return []


def _execution_manifest_path(
    final_root: Path, authorization: FinalTestAuthorization
) -> Path:
    attempt = final_root / "attempt_inputs" / authorization.attempt_id / "manifest.json"
    if attempt.is_file():
        return attempt
    return final_root / _EXECUTION_INPUT_MANIFEST


def _resolve_verified_inputs(
    source: object,
    signals: FinalTestSignals,
    *,
    authorization: FinalTestAuthorization,
    code_root: Path,
    final_root: Path,
    supported_markets: tuple[str, ...],
) -> FinalTestBacktestInputs:
    verified = verify_execution_input_manifest(
        _execution_manifest_path(final_root, authorization),
        authorization,
    )
    pretest = _resolve_pretest_execution_inputs(
        authorization,
        code_root=code_root,
        data_root=final_root.parent.parent,
    )
    assert_supported_markets(
        pretest.execution_panel,
        supported_markets,
        label="final backtest handoff Stage-7 execution panel",
    )
    assert_supported_markets(
        pretest.target_weights,
        supported_markets,
        label="final backtest handoff Stage-7 targets",
    )
    assert_supported_markets(
        pretest.corporate_actions,
        supported_markets,
        label="final backtest handoff Stage-7 corporate actions",
    )
    assert_supported_markets(
        pretest.security_events,
        supported_markets,
        label="final backtest handoff Stage-7 security events",
        symbol_columns=("source_symbol", "target_symbol"),
    )
    adv_lookback = pretest.adv_lookback
    if adv_lookback <= 0:
        raise ValueError("frozen ADV lookback must be positive")

    final_actions = _load_final_actions(verified["corporate_actions.parquet"])
    final_events = _load_final_security_events(verified["security_events.parquet"])
    targets = _continuous_targets(pretest.target_weights, signals.target_weights)
    symbols = _execution_symbols(targets, pretest.security_events, final_events)
    final_calendar = _load_final_trading_calendar(source)
    final_daily = _load_final_execution_daily(source, symbols)
    _validate_final_coverage(
        source,
        final_calendar,
        final_daily,
        signals.target_weights,
    )
    final_execution = _extend_execution_panel(
        pretest.execution_panel,
        pretest.warmup_daily,
        final_daily,
        symbols=symbols,
        adv_lookback=adv_lookback,
    )
    actions = pl.concat(
        (pretest.corporate_actions, final_actions), how="vertical_relaxed"
    ).sort("effective_date", "symbol", "action_id")
    if actions.select(pl.col("action_id").is_duplicated().any()).item():
        raise ValueError("continuous corporate actions contain duplicate identities")
    events = pl.concat(
        (pretest.security_events, final_events), how="vertical_relaxed"
    ).sort("effective_date", "source_symbol")
    if events.select(
        pl.struct("effective_date", "source_symbol").is_duplicated().any()
    ).item():
        raise ValueError("continuous security events contain duplicate keys")
    return FinalTestBacktestInputs(
        execution_panel=final_execution,
        target_weights=targets,
        corporate_actions=actions,
        security_events=events,
        stale_evidence=pl.DataFrame(schema={"symbol": pl.String}),
    )


def _resolve_pretest_execution_inputs(
    authorization: FinalTestAuthorization,
    *,
    code_root: Path,
    data_root: Path,
) -> _PretestExecutionInputs:
    config = _load_frozen_config(code_root, data_root)
    formal = config.formal_backtest
    if formal is None:
        raise ValueError("frozen formal-backtest settings are missing")
    signal_inputs = resolve_final_test_signal_inputs(
        config,
        authorization,
        data_root=data_root,
    )
    robustness = resolve_current(data_root / "processed/robustness")
    if robustness.run_id != authorization.robustness_release:
        raise ValueError("robustness release changed during final input resolution")
    sealed = json.loads(
        (robustness.artifacts / "sealed_test_protocol.json").read_text(
            encoding="utf-8"
        )
    )
    if sealed.get("sealed_protocol_sha256") != authorization.sealed_protocol_sha256:
        raise ValueError("sealed protocol changed during final input resolution")
    expected_validation = sealed.get("upstream_validation")
    if not isinstance(expected_validation, dict):
        raise ValueError("authorized seal lacks validation release identity")
    validation = resolve_current(data_root / "processed/validation_evaluation")
    if (
        validation.run_id != expected_validation.get("run_id")
        or validation.manifest_sha256
        != expected_validation.get("manifest_sha256")
    ):
        raise ValueError("validation release differs from authorized seal")
    paths = {
        name: _release_file(validation, f"datasets/inputs/{name}.parquet")
        for name in (
            "execution_panel",
            f"continuous_targets_{MAIN_CANDIDATE}",
            "corporate_actions",
            "security_events",
        )
    }
    return _PretestExecutionInputs(
        execution_panel=pl.read_parquet(paths["execution_panel"]),
        target_weights=pl.read_parquet(
            paths[f"continuous_targets_{MAIN_CANDIDATE}"]
        ),
        corporate_actions=pl.read_parquet(paths["corporate_actions"]),
        security_events=pl.read_parquet(paths["security_events"]),
        warmup_daily=signal_inputs.historical_daily,
        adv_lookback=formal.adv_lookback,
    )


def _load_final_actions(path: Path) -> pl.DataFrame:
    frame = pl.read_parquet(path)
    return normalize_corporate_action_rows(frame, maximum_date=FINAL_TEST_END)


def _load_final_security_events(path: Path) -> pl.DataFrame:
    frame = pl.read_parquet(path)
    return normalize_security_event_rows(frame).drop("evidence_id")


def _continuous_targets(
    historical: pl.DataFrame,
    final: pl.DataFrame,
) -> pl.DataFrame:
    required = {"date", "symbol", "target_weight"}
    if not required.issubset(historical.columns):
        raise ValueError("Stage-7 continuous targets have an invalid schema")
    historical = historical.select(*sorted(required))
    if historical.is_empty() or historical.filter(
        pl.col("date") >= FINAL_TEST_START
    ).height:
        raise ValueError("Stage-7 continuous targets cross the final-test boundary")
    if historical.get_column("date").max() != date(2021, 12, 31):
        raise ValueError("Stage-7 continuous targets must end on 2021-12-31")
    historical_totals = historical.group_by("date").agg(
        pl.col("target_weight").sum().alias("weight")
    )
    if historical.filter(pl.col("target_weight") < 0).height or historical_totals.filter(
        (pl.col("weight") - 1.0).abs() > 1e-12
    ).height:
        raise ValueError("Stage-7 continuous targets fail the weight contract")
    combined = pl.concat(
        (historical, final.select(*sorted(required))), how="vertical_relaxed"
    ).sort("date", "symbol")
    if combined.select(pl.struct("date", "symbol").is_duplicated().any()).item():
        raise ValueError("continuous final targets contain duplicate keys")
    return combined


def _execution_symbols(
    targets: pl.DataFrame,
    historical_events: pl.DataFrame,
    final_events: pl.DataFrame,
) -> list[str]:
    symbols = set(targets.get_column("symbol"))
    for events in (historical_events, final_events):
        if events.is_empty():
            continue
        symbols.update(events.get_column("source_symbol").drop_nulls())
        symbols.update(events.get_column("target_symbol").drop_nulls())
    return sorted(str(symbol) for symbol in symbols)


def _load_final_execution_daily(source: object, symbols: list[str]) -> pl.DataFrame:
    files = getattr(source, "files", None)
    if not isinstance(files, tuple) or not files:
        raise ValueError("verified final daily-panel source is empty")
    columns = (
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
    daily = (
        pl.scan_parquet(files)
        .filter(pl.col("symbol").is_in(symbols))
        .select(*columns)
        .collect()
        .sort("date", "symbol")
    )
    if daily.is_empty() or daily.filter(
        ~pl.col("date").is_between(FINAL_TEST_START, FINAL_TEST_END)
    ).height:
        raise ValueError("final execution daily panel crosses the sealed date bounds")
    if daily.select(pl.struct("date", "symbol").is_duplicated().any()).item():
        raise ValueError("final execution daily panel contains duplicate keys")
    return daily


def _load_final_trading_calendar(source: object) -> pl.DataFrame:
    files = getattr(source, "files", None)
    if not isinstance(files, tuple) or not files:
        raise ValueError("verified final daily-panel source is empty")
    return (
        pl.scan_parquet(files)
        .select("date")
        .unique()
        .collect()
        .sort("date")
    )


def _validate_final_coverage(
    source: object,
    trading_calendar: pl.DataFrame,
    final_execution: pl.DataFrame,
    final_targets: pl.DataFrame,
) -> None:
    manifest = getattr(source, "manifest", None)
    if not isinstance(manifest, dict):
        raise ValueError("verified final daily-panel manifest is missing")
    try:
        manifest_minimum = date.fromisoformat(str(manifest["min_date"]))
        manifest_maximum = date.fromisoformat(str(manifest["max_date"]))
    except (KeyError, ValueError) as error:
        raise ValueError("verified final daily-panel date identity is invalid") from error
    if (
        manifest.get("years") != [2022, 2023, 2024, 2025]
        or manifest_minimum.year != FINAL_TEST_START.year
        or manifest_maximum.year != FINAL_TEST_END.year
    ):
        raise ValueError("final daily-panel manifest must cover all four final-test years")
    if trading_calendar.is_empty() or (
        trading_calendar.get_column("date").min() != manifest_minimum
        or trading_calendar.get_column("date").max() != manifest_maximum
    ):
        raise ValueError("trading calendar differs from the verified final manifest")
    actual_months = set(
        trading_calendar.select(
            pl.col("date").dt.year().alias("year"),
            pl.col("date").dt.month().alias("month"),
        ).iter_rows()
    )
    expected_months = {
        (year, month)
        for year in range(FINAL_TEST_START.year, FINAL_TEST_END.year + 1)
        for month in range(1, 13)
    }
    if actual_months != expected_months:
        raise ValueError("trading calendar must cover all 48 continuous final-test months")
    if (
        final_execution.is_empty()
        or final_execution.get_column("date").max() != manifest_maximum
    ):
        raise ValueError("final execution rows do not reach the manifest last trading day")
    expected_rebalances = set(
        trading_calendar.with_columns(
            pl.col("date").dt.year().alias("_year"),
            pl.col("date").dt.month().alias("_month"),
        )
        .group_by("_year", "_month")
        .agg(pl.col("date").max())
        .get_column("date")
    )
    actual_rebalances = set(final_targets.get_column("date"))
    if actual_rebalances != expected_rebalances:
        raise ValueError("final targets differ from the frozen month-end rebalance schedule")


def _extend_execution_panel(
    historical_execution: pl.DataFrame,
    warmup_daily: pl.DataFrame,
    final_daily: pl.DataFrame,
    *,
    symbols: list[str],
    adv_lookback: int,
) -> pl.DataFrame:
    raw_columns = final_daily.columns
    warmup = (
        warmup_daily.filter(pl.col("symbol").is_in(symbols))
        .select(*raw_columns)
        .sort("symbol", "date")
        .group_by("symbol", maintain_order=True)
        .tail(adv_lookback)
        .select(*raw_columns)
    )
    raw = pl.concat((warmup, final_daily), how="vertical_relaxed").sort(
        "date", "symbol"
    )
    extension = (
        build_execution_panel(raw, adv_lookback=adv_lookback)
        .filter(pl.col("date") >= FINAL_TEST_START)
        .select(historical_execution.columns)
    )
    combined = pl.concat(
        (historical_execution, extension), how="vertical_relaxed"
    ).sort("date", "symbol")
    if combined.select(pl.struct("date", "symbol").is_duplicated().any()).item():
        raise ValueError("continuous execution panel contains duplicate keys")
    return combined


def _execute_verified_inputs(
    inputs: FinalTestBacktestInputs,
    *,
    fees: FeeSchedule,
    formal: FormalBacktestSettings | None,
) -> FinalTestBacktestResult:
    if formal is None:
        return _blocked_preflight(["frozen formal-backtest settings are missing"])
    settings = BacktestSettings(
        initial_cash=formal.initial_cash,
        maximum_participation=formal.maximum_participation,
        fixed_slippage_bps=formal.fixed_slippage_bps,
        reference_impact_bps=formal.reference_impact_bps,
        reference_participation=formal.reference_participation,
        maximum_impact_bps=formal.maximum_impact_bps,
        buy_lot_size=formal.buy_lot_size,
        analysis_end=FINAL_TEST_END,
    )
    outputs = run_backtest(
        inputs.execution_panel,
        inputs.target_weights,
        inputs.corporate_actions,
        fees,
        settings,
        inputs.security_events,
    )
    reconciliation_frames = (
        outputs["reconciliation"],
        outputs["scenario_reconciliation"],
    )
    reconciliation_is_finite = all(
        not _has_nonfinite(frame, ("difference",)) for frame in reconciliation_frames
    )
    reconciliation = (
        max(
            float(frame["difference"].abs().max() or 0.0)
            for frame in reconciliation_frames
        )
        if reconciliation_is_finite
        else float("inf")
    )
    nav_is_finite = not _has_nonfinite(outputs["nav"])
    shadow, shadow_daily = shadow_nav_audit(
        inputs.execution_panel,
        inputs.corporate_actions,
        outputs["positions"],
        outputs["nav"],
        threshold=formal.shadow_divergence_threshold,
    )
    stale, stale_intervals = audit_stale_positions(
        outputs["positions"],
        outputs["nav"],
        inputs.execution_panel,
        inputs.security_events,
        inputs.stale_evidence,
        review_days=formal.stale_review_days,
    )
    failures = []
    if not reconciliation_is_finite:
        failures.append("non-finite reconciliation gate failed")
    elif reconciliation > _RECONCILIATION_THRESHOLD:
        failures.append("reconciliation gate failed")
    if not nav_is_finite:
        failures.append("non-finite NAV gate failed")
    if shadow.get("status") != "ready":
        failures.append("shadow NAV gate failed")
    if stale.get("status") != "ready" or stale.get("unexplained_symbols", 0):
        failures.append("stale valuation gate failed")
    audits: dict[str, object] = {
        "maximum_reconciliation_difference": reconciliation,
        "shadow_nav": shadow,
        "shadow_nav_daily": shadow_daily,
        "stale_valuation": stale,
        "stale_intervals": stale_intervals,
    }
    return FinalTestBacktestResult(
        outputs=outputs,
        audits=audits,
        preflight={"status": "ready", "execution_started": True},
        publishable=not failures,
        gate_failures=tuple(failures),
    )


def _has_nonfinite(
    frame: pl.DataFrame,
    columns: tuple[str, ...] | None = None,
) -> bool:
    selected = columns or tuple(
        name for name, dtype in frame.schema.items() if dtype.is_numeric()
    )
    if (
        frame.is_empty()
        or not selected
        or any(name not in frame.columns for name in selected)
    ):
        return True
    return any(
        frame.select((pl.col(name).is_null() | ~pl.col(name).is_finite()).any()).item()
        for name in selected
    )


def _blocked_preflight(failures: list[str]) -> FinalTestBacktestResult:
    return FinalTestBacktestResult(
        outputs={},
        audits={},
        preflight={
            "status": "blocked",
            "execution_started": False,
            "gate_failures": list(failures),
        },
        publishable=False,
        gate_failures=tuple(failures),
    )
