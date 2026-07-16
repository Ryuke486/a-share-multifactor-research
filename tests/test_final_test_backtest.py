from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from ashare_multifactor.config import load_config
from ashare_multifactor.final_test.backtest import run_final_test_backtest
from ashare_multifactor.final_test.action_source_contract import (
    build_execution_input_manifest,
)
from ashare_multifactor.final_test.gate import FinalTestAuthorization
from ashare_multifactor.final_test.signals import FinalTestSignals


FINAL_START = date(2022, 1, 1)
FINAL_END = date(2025, 12, 31)


def _authorization() -> FinalTestAuthorization:
    return FinalTestAuthorization(
        attempt_id="attempt-001",
        approval_id="approved-stage9",
        registered_at="2026-07-16T00:00:00+00:00",
        git_commit="a" * 40,
        git_tree="b" * 40,
        sealed_protocol_sha256="c" * 64,
        robustness_release="stage8-release",
        test_period=(FINAL_START, FINAL_END),
    )


def _signals() -> FinalTestSignals:
    targets = pl.DataFrame(
        {
            "date": [date(2022, 1, 31)],
            "method": ["rolling_ic_family"],
            "portfolio_name": ["size_stratified_buffered"],
            "candidate": ["rolling_ic_family_size_stratified_buffered"],
            "symbol": ["000001"],
            "target_weight": [1.0],
        }
    )
    empty = pl.DataFrame()
    return FinalTestSignals(empty, empty, empty, targets)


def _config(tmp_path: Path):
    frozen = load_config(Path("configs/research_protocol.yaml"))
    return replace(
        frozen,
        paths=replace(frozen.paths, processed=tmp_path / "processed"),
    )


def test_backtest_requires_authorization_before_any_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def forbidden(*_args: object, **_kwargs: object) -> None:
        nonlocal called
        called = True
        raise AssertionError("authorization must be checked first")

    monkeypatch.setattr(
        "ashare_multifactor.final_test.backtest._load_frozen_config", forbidden
    )

    with pytest.raises(TypeError, match="FinalTestAuthorization"):
        run_final_test_backtest(
            None,
            _signals(),
            code_root=tmp_path / "code",
            final_root=tmp_path / "processed/final_test",
        )

    assert called is False


def test_frozen_fee_gap_blocks_before_final_data_or_stage6_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    resolved_data = False
    executed = False
    monkeypatch.setattr(
        "ashare_multifactor.final_test.backtest._load_frozen_config",
        lambda *_args: config,
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.backtest._verify_data_authorization",
        lambda *_args: None,
    )

    def forbidden_data(*_args: object, **_kwargs: object) -> None:
        nonlocal resolved_data
        resolved_data = True
        raise AssertionError("2022 data must remain unread")

    def forbidden_execution(*_args: object, **_kwargs: object) -> None:
        nonlocal executed
        executed = True
        raise AssertionError("Stage6 execution must not start")

    monkeypatch.setattr(
        "ashare_multifactor.final_test.backtest._resolve_authorized_data",
        forbidden_data,
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.backtest.run_backtest", forbidden_execution
    )

    result = run_final_test_backtest(
        _authorization(),
        _signals(),
        code_root=tmp_path / "code",
        final_root=tmp_path / "processed/final_test",
    )

    assert result.publishable is False
    assert result.outputs == {}
    assert any("fee coverage" in item for item in result.gate_failures)
    assert result.preflight["status"] == "blocked"
    assert resolved_data is False
    assert executed is False


def test_missing_authoritative_action_inputs_block_before_final_data_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    resolved_data = False
    monkeypatch.setattr(
        "ashare_multifactor.final_test.backtest._load_frozen_config",
        lambda *_args: config,
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.backtest._verify_data_authorization",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.backtest._fee_coverage_failures",
        lambda *_args: [],
    )

    def forbidden_data(*_args: object, **_kwargs: object) -> None:
        nonlocal resolved_data
        resolved_data = True
        raise AssertionError("2022 data must remain unread")

    monkeypatch.setattr(
        "ashare_multifactor.final_test.backtest._resolve_authorized_data",
        forbidden_data,
    )

    result = run_final_test_backtest(
        _authorization(),
        _signals(),
        code_root=tmp_path / "code",
        final_root=tmp_path / "processed/final_test",
    )

    assert result.publishable is False
    assert any("corporate-action" in item for item in result.gate_failures)
    assert any("security-event" in item for item in result.gate_failures)
    assert result.preflight["execution_started"] is False
    assert resolved_data is False


def test_tampered_execution_input_is_blocked_before_final_data_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(
        "ashare_multifactor.final_test.backtest._load_frozen_config",
        lambda *_args: config,
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.backtest._verify_data_authorization",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.backtest._fee_coverage_failures",
        lambda *_args: [],
    )
    execution_inputs = tmp_path / "processed/final_test/execution_inputs"
    execution_inputs.mkdir(parents=True)
    actions = execution_inputs / "corporate_actions.parquet"
    events = execution_inputs / "security_events.parquet"
    pl.DataFrame(schema={"effective_date": pl.Date}).write_parquet(actions)
    pl.DataFrame(schema={"effective_date": pl.Date}).write_parquet(events)
    build_execution_input_manifest(
        execution_inputs / "manifest.json",
        authorization=_authorization(),
        files={
            "corporate_actions.parquet": actions,
            "security_events.parquet": events,
        },
    )
    actions.write_bytes(b"tampered")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("tampered inputs must block before final data read")

    monkeypatch.setattr(
        "ashare_multifactor.final_test.backtest._resolve_authorized_data",
        forbidden,
    )

    result = run_final_test_backtest(
        _authorization(),
        _signals(),
        code_root=tmp_path / "code",
        final_root=tmp_path / "processed/final_test",
    )

    assert result.publishable is False
    assert result.preflight["execution_started"] is False
    assert "digest mismatch" in " ".join(result.gate_failures)


def test_runtime_audit_failure_retains_stage6_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    outputs = {
        "positions": pl.DataFrame(
            schema={
                "date": pl.Date,
                "symbol": pl.String,
                "market_value": pl.Float64,
                "is_stale": pl.Boolean,
                "stale_days": pl.Int64,
            }
        ),
        "nav": pl.DataFrame(
            {
                "date": [date(2022, 1, 4)],
                "nav": [100_000_000.0],
            }
        ),
        "reconciliation": pl.DataFrame({"difference": [0.02]}),
        "scenario_reconciliation": pl.DataFrame({"difference": [0.03]}),
    }
    inputs = SimpleNamespace(
        execution_panel=pl.DataFrame(),
        target_weights=pl.DataFrame(),
        corporate_actions=pl.DataFrame(),
        security_events=pl.DataFrame(),
        stale_evidence=pl.DataFrame(schema={"symbol": pl.String}),
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.backtest.run_backtest",
        lambda *_args, **_kwargs: outputs,
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.backtest.shadow_nav_audit",
        lambda *_args, **_kwargs: (
            {"status": "blocked", "breach_days": 1},
            pl.DataFrame(),
        ),
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.backtest.audit_stale_positions",
        lambda *_args, **_kwargs: (
            {"status": "blocked", "unexplained_symbols": 1},
            pl.DataFrame(),
        ),
    )

    from ashare_multifactor.final_test.backtest import _execute_verified_inputs

    result = _execute_verified_inputs(
        inputs,
        fees=object(),
        formal=config.formal_backtest,
    )

    assert result.outputs is outputs
    assert result.publishable is False
    assert set(result.gate_failures) == {
        "reconciliation gate failed",
        "shadow NAV gate failed",
        "stale valuation gate failed",
    }
    assert result.audits["shadow_nav"]["status"] == "blocked"
    assert result.audits["stale_valuation"]["status"] == "blocked"
