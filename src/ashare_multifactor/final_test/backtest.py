from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from ashare_multifactor.config import FormalBacktestSettings
from ashare_multifactor.execution.broker import BacktestSettings, run_backtest
from ashare_multifactor.execution.fees import FeeSchedule, load_market_rules
from ashare_multifactor.execution.shadow_nav import shadow_nav_audit
from ashare_multifactor.execution.stale_audit import audit_stale_positions
from ashare_multifactor.final_test.data_extension import (
    _verify_authorization as _verify_data_authorization,
)
from ashare_multifactor.final_test.action_source_contract import (
    verify_execution_input_manifest,
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


_RECONCILIATION_THRESHOLD = 0.01
_EXECUTION_INPUT_MANIFEST = "execution_inputs/manifest.json"


@dataclass(frozen=True)
class FinalTestBacktestResult:
    outputs: dict[str, pl.DataFrame]
    audits: dict[str, object]
    preflight: dict[str, object]
    publishable: bool
    gate_failures: tuple[str, ...]


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
    _validate_final_targets(signals.target_weights)

    code_root = code_root.resolve()
    final_root = final_root.resolve()
    config = _load_frozen_config(code_root, final_root.parent.parent)
    if final_root != config.paths.processed / "final_test":
        raise ValueError("final-test root is not canonical")
    _verify_data_authorization(config, authorization, code_root)

    failures = _fee_coverage_failures(code_root / "configs/market_rules.yaml")
    failures.extend(_execution_input_failures(final_root, authorization))
    if failures:
        return _blocked_preflight(failures)

    # This point is deliberately unreachable under the current Stage-8 seal.
    # Resolution happens only after all preflight checks so no 2022 file is read
    # when the frozen fee/action contract is incomplete.
    source = _resolve_authorized_data(final_root, authorization, config.test)
    try:
        inputs = _resolve_verified_inputs(
            source,
            signals,
            authorization=authorization,
            code_root=code_root,
            final_root=final_root,
        )
    except (FileNotFoundError, ValueError) as error:
        return _blocked_preflight([str(error)])
    fees = load_market_rules(code_root / "configs/market_rules.yaml")
    if config.formal_backtest is None:
        return _blocked_preflight(["frozen formal-backtest settings are missing"])
    return _execute_verified_inputs(inputs, fees=fees, formal=config.formal_backtest)


def _validate_final_targets(targets: pl.DataFrame) -> None:
    required = {"date", "candidate", "symbol", "target_weight"}
    if not required.issubset(targets.columns) or targets.is_empty():
        raise ValueError("Task 3 final targets are empty or have an invalid schema")
    if set(targets.get_column("candidate")) != {MAIN_CANDIDATE}:
        raise ValueError("Task 3 targets substitute the sealed main candidate")
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
    manifest_path = final_root / _EXECUTION_INPUT_MANIFEST
    if not manifest_path.is_file():
        return [
            "authoritative 2022-2025 corporate-action inputs are missing",
            "authoritative 2022-2025 security-event inputs are missing",
        ]
    try:
        verify_execution_input_manifest(manifest_path, authorization)
    except (FileNotFoundError, TypeError, ValueError) as error:
        return [str(error)]
    return []


def _resolve_verified_inputs(
    source: object,
    signals: FinalTestSignals,
    *,
    authorization: FinalTestAuthorization,
    code_root: Path,
    final_root: Path,
) -> Any:
    del source, signals, authorization, code_root, final_root
    raise ValueError(
        "Stage-8 seal must be invalidated and reissued before final execution inputs can be resolved"
    )


def _execute_verified_inputs(
    inputs: Any,
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
    reconciliation = max(
        float(outputs["reconciliation"]["difference"].abs().max() or 0.0),
        float(
            outputs["scenario_reconciliation"]["difference"].abs().max() or 0.0
        ),
    )
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
    if reconciliation > _RECONCILIATION_THRESHOLD:
        failures.append("reconciliation gate failed")
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
