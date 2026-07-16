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


def test_verified_inputs_extend_stage7_ledger_with_final_manifest_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ashare_multifactor.execution.corporate_actions import (
        normalize_corporate_actions,
    )
    from ashare_multifactor.final_test.backtest import _resolve_verified_inputs

    final_root = tmp_path / "processed/final_test"
    final_panel_path = final_root / "daily_panel/year=2022/part-000.parquet"
    final_panel_path.parent.mkdir(parents=True)
    pl.DataFrame(
        {
            "date": [date(2022, 1, 3), date(2022, 1, 4)],
            "symbol": ["000001", "000001"],
            "open_raw": [10.0, 10.1],
            "close_raw": [10.0, 10.1],
            "prev_close_raw": [10.0, 10.0],
            "close_adj": [10.0, 10.1],
            "prev_close_adj": [10.0, 10.0],
            "amount": [300.0, 400.0],
            "is_st": [False, False],
        }
    ).write_parquet(final_panel_path)

    execution_inputs = final_root / "execution_inputs"
    execution_inputs.mkdir()
    final_actions_path = execution_inputs / "corporate_actions.parquet"
    final_events_path = execution_inputs / "security_events.parquet"
    pl.DataFrame(
        {
            "symbol": ["000001"],
            "ex_date": [date(2022, 1, 4)],
            "effective_date": [date(2022, 1, 4)],
            "cash_per_share": [0.1],
            "share_ratio": [0.0],
            "source": ["synthetic"],
        }
    ).write_parquet(final_actions_path)
    pl.DataFrame(
        schema={
            "effective_date": pl.Date,
            "source_symbol": pl.String,
            "event_type": pl.String,
            "target_symbol": pl.String,
            "ratio": pl.Float64,
            "cash_per_share": pl.Float64,
            "source": pl.String,
        }
    ).write_parquet(final_events_path)
    build_execution_input_manifest(
        execution_inputs / "manifest.json",
        authorization=_authorization(),
        files={
            "corporate_actions.parquet": final_actions_path,
            "security_events.parquet": final_events_path,
        },
    )

    historical_actions = normalize_corporate_actions(
        pl.DataFrame(
            schema={
                "symbol": pl.String,
                "effective_date": pl.Date,
                "cash_per_share": pl.Float64,
                "share_ratio": pl.Float64,
                "source": pl.String,
            }
        )
    )
    historical_events = pl.DataFrame(schema=pl.read_parquet(final_events_path).schema)
    pretest = SimpleNamespace(
        execution_panel=pl.DataFrame(
            {
                "date": [date(2021, 12, 30), date(2021, 12, 31)],
                "symbol": ["000001", "000001"],
                "open_raw": [9.8, 9.9],
                "close_raw": [9.8, 9.9],
                "prev_close_raw": [9.7, 9.8],
                "close_adj": [9.8, 9.9],
                "prev_close_adj": [9.7, 9.8],
                "adv20": [90.0, 100.0],
                "limit_rate": [0.1, 0.1],
                "is_suspended_proxy": [False, False],
            }
        ),
        target_weights=pl.DataFrame(
            {
                "date": [date(2021, 12, 31)],
                "symbol": ["000001"],
                "target_weight": [1.0],
            }
        ),
        corporate_actions=historical_actions,
        security_events=historical_events,
        adv_lookback=20,
        warmup_daily=pl.DataFrame(
            {
                "date": [date(2021, 12, 30), date(2021, 12, 31)],
                "symbol": ["000001", "000001"],
                "open_raw": [9.8, 9.9],
                "close_raw": [9.8, 9.9],
                "prev_close_raw": [9.7, 9.8],
                "close_adj": [9.8, 9.9],
                "prev_close_adj": [9.7, 9.8],
                "amount": [100.0, 200.0],
                "is_st": [False, False],
            }
        ),
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.backtest._resolve_pretest_execution_inputs",
        lambda *_args, **_kwargs: pretest,
        raising=False,
    )

    result = _resolve_verified_inputs(
        SimpleNamespace(files=(final_panel_path,)),
        _signals(),
        authorization=_authorization(),
        code_root=tmp_path,
        final_root=final_root,
    )

    assert result.execution_panel.get_column("date").max() == date(2022, 1, 4)
    assert result.execution_panel.filter(pl.col("date") == date(2022, 1, 3)).item(
        0, "adv20"
    ) == pytest.approx(150.0)
    assert result.target_weights.get_column("date").to_list() == [
        date(2021, 12, 31),
        date(2022, 1, 31),
    ]
    assert result.corporate_actions.get_column("effective_date").max() == date(
        2022, 1, 4
    )
    assert result.stale_evidence.schema == {"symbol": pl.String}


def test_continuous_targets_require_exact_stage7_handoff_date() -> None:
    from ashare_multifactor.final_test.backtest import _continuous_targets

    historical = pl.DataFrame(
        {
            "date": [date(2021, 12, 30)],
            "symbol": ["000001"],
            "target_weight": [1.0],
        }
    )

    with pytest.raises(ValueError, match="2021-12-31"):
        _continuous_targets(historical, _signals().target_weights)


def test_final_action_dates_must_be_non_null_and_inside_sealed_period(
    tmp_path: Path,
) -> None:
    from ashare_multifactor.final_test.backtest import _load_final_actions

    path = tmp_path / "corporate_actions.parquet"
    pl.DataFrame(
        {
            "symbol": ["000001"],
            "ex_date": [None],
            "effective_date": [date(2022, 1, 4)],
            "cash_per_share": [0.1],
            "share_ratio": [0.0],
            "source": ["synthetic"],
        },
        schema_overrides={"ex_date": pl.Date},
    ).write_parquet(path)

    with pytest.raises(ValueError, match="sealed date bounds"):
        _load_final_actions(path)


def test_verified_inputs_run_the_unchanged_three_scenario_stage6_engine() -> None:
    from ashare_multifactor.execution.corporate_actions import (
        normalize_corporate_actions,
    )
    from ashare_multifactor.execution.fees import load_market_rules
    from ashare_multifactor.final_test.backtest import (
        FinalTestBacktestInputs,
        _execute_verified_inputs,
    )

    config = load_config(Path("configs/research_protocol.yaml"))
    inputs = FinalTestBacktestInputs(
        execution_panel=pl.DataFrame(
            {
                "date": [date(2022, 1, 3), date(2022, 1, 4), date(2022, 1, 5)],
                "symbol": ["000001"] * 3,
                "open_raw": [10.0, 10.0, 10.1],
                "close_raw": [10.0, 10.1, 10.2],
                "prev_close_raw": [10.0, 10.0, 10.1],
                "close_adj": [10.0, 10.1, 10.2],
                "prev_close_adj": [10.0, 10.0, 10.1],
                "adv20": [1_000_000_000.0] * 3,
                "limit_rate": [0.1] * 3,
                "is_suspended_proxy": [False] * 3,
            }
        ),
        target_weights=pl.DataFrame(
            {
                "date": [date(2022, 1, 3)],
                "symbol": ["000001"],
                "target_weight": [0.01],
            }
        ),
        corporate_actions=normalize_corporate_actions(
            pl.DataFrame(
                schema={
                    "symbol": pl.String,
                    "effective_date": pl.Date,
                    "cash_per_share": pl.Float64,
                    "share_ratio": pl.Float64,
                    "source": pl.String,
                }
            )
        ),
        security_events=pl.DataFrame(
            schema={
                "effective_date": pl.Date,
                "source_symbol": pl.String,
                "event_type": pl.String,
                "target_symbol": pl.String,
                "ratio": pl.Float64,
                "cash_per_share": pl.Float64,
                "source": pl.String,
            }
        ),
        stale_evidence=pl.DataFrame(schema={"symbol": pl.String}),
    )

    result = _execute_verified_inputs(
        inputs,
        fees=load_market_rules(Path("configs/market_rules.yaml")),
        formal=config.formal_backtest,
    )

    assert result.publishable is True
    assert result.outputs["trades"].item(0, "date") == date(2022, 1, 4)
    assert result.outputs["trades"].item(0, "quantity") % 100 == 0
    assert set(result.outputs["scenario_nav"].get_column("scenario")) == {
        "full_cost",
        "explicit_fee_only",
        "zero_cost",
    }
    assert result.outputs["scenario_reconciliation"]["difference"].abs().max() < 0.01
