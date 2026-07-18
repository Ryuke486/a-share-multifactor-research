from dataclasses import replace
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from ashare_multifactor.config import load_config
from ashare_multifactor.final_test.backtest import (
    _execution_input_failures,
    _validate_final_targets,
    run_final_test_backtest,
)
from ashare_multifactor.final_test.action_source_contract import (
    build_execution_input_manifest,
)
from ashare_multifactor.final_test.gate import FinalTestAuthorization
from ashare_multifactor.final_test.registry import (
    append_attempt_state,
    bind_execution_identity,
    bind_execution_input_manifest_hash,
    register_attempt,
)
from ashare_multifactor.final_test.signals import FinalTestSignals
from ashare_multifactor.audit.records import sha256_file


FINAL_START = date(2022, 1, 1)
FINAL_END = date(2025, 12, 31)


def test_final_targets_reject_bj_leakage() -> None:
    targets = pl.DataFrame(
        {
            "date": [FINAL_START],
            "candidate": ["rolling_ic_family_size_stratified_buffered"],
            "symbol": ["920001"],
            "target_weight": [1.0],
        }
    )
    with pytest.raises(ValueError, match="supported market"):
        _validate_final_targets(targets, supported_markets=("sh", "sz"))


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
    assert any("attempt" in item or "binding" in item for item in result.gate_failures)
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
    assert "attempt" in " ".join(result.gate_failures)


def test_unregistered_attempt_inputs_are_rejected_even_if_self_consistent(
    tmp_path: Path,
) -> None:
    final_root = tmp_path / "processed/final_test"
    execution_inputs = final_root / "attempt_inputs/attempt-001"
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

    failures = _execution_input_failures(final_root, _authorization())
    assert any("attempt" in item or "binding" in item for item in failures)


def test_self_consistent_input_replacement_is_rejected_by_registry_binding(
    tmp_path: Path,
) -> None:
    authorization = _authorization()
    final_root = tmp_path / "processed/final_test"
    registry = final_root / "attempts"
    register_attempt(
        registry,
        attempt_id=authorization.attempt_id,
        git_commit=authorization.git_commit,
        git_tree=authorization.git_tree,
        token_sha256="d" * 64,
        sealed_protocol_sha256=authorization.sealed_protocol_sha256,
        robustness_release=authorization.robustness_release,
        robustness_manifest_sha256="e" * 64,
        robustness_lineage_sha256="f" * 64,
        approval_id=authorization.approval_id,
    )
    prepare_sha256 = "1" * 64
    security_sha256 = "2" * 64
    corporate_sha256 = "3" * 64
    append_attempt_state(
        registry,
        attempt_id=authorization.attempt_id,
        state="preparing",
    )
    append_attempt_state(
        registry,
        attempt_id=authorization.attempt_id,
        state="awaiting_official_evidence",
        identities={"prepare_manifest_sha256": prepare_sha256},
    )
    append_attempt_state(
        registry,
        attempt_id=authorization.attempt_id,
        state="executing",
        identities={
            "prepare_manifest_sha256": prepare_sha256,
            "security_event_coverage_sha256": security_sha256,
            "corporate_action_coverage_sha256": corporate_sha256,
        },
    )
    snapshot_manifest = (
        final_root
        / "execution_coverage_snapshots"
        / authorization.attempt_id
        / "snapshot_manifest.json"
    )
    snapshot_manifest.parent.mkdir(parents=True)
    snapshot_manifest.write_text(
        json.dumps(
            {
                "attempt_id": authorization.attempt_id,
                "prepare_manifest_sha256": prepare_sha256,
                "security_event_coverage_sha256": security_sha256,
                "corporate_action_coverage_sha256": corporate_sha256,
            }
        ),
        encoding="utf-8",
    )
    identity = {
        "attempt_id": authorization.attempt_id,
        "execution_id": "execution-001",
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "prepare_manifest_sha256": prepare_sha256,
        "security_event_coverage_sha256": security_sha256,
        "corporate_action_coverage_sha256": corporate_sha256,
        "coverage_snapshot_manifest_sha256": sha256_file(snapshot_manifest),
    }
    attempt_root = final_root / "attempt_runs" / authorization.attempt_id
    attempt_root.mkdir(parents=True)
    (attempt_root / "execution_identity.json").write_text(
        json.dumps(identity), encoding="utf-8"
    )
    bind_execution_identity(registry, identity=identity)
    execution_inputs = final_root / "attempt_inputs" / authorization.attempt_id
    execution_inputs.mkdir(parents=True)
    actions = execution_inputs / "corporate_actions.parquet"
    events = execution_inputs / "security_events.parquet"
    pl.DataFrame({"version": [1]}).write_parquet(actions)
    pl.DataFrame({"version": [1]}).write_parquet(events)
    manifest_path = execution_inputs / "manifest.json"
    build_execution_input_manifest(
        manifest_path,
        authorization=authorization,
        files={
            "corporate_actions.parquet": actions,
            "security_events.parquet": events,
        },
        execution_identity=identity,
    )
    bind_execution_input_manifest_hash(
        registry,
        identity=identity,
        manifest_sha256=sha256_file(manifest_path),
    )

    pl.DataFrame({"version": [2]}).write_parquet(actions)
    pl.DataFrame({"version": [2]}).write_parquet(events)
    build_execution_input_manifest(
        manifest_path,
        authorization=authorization,
        files={
            "corporate_actions.parquet": actions,
            "security_events.parquet": events,
        },
        execution_identity=identity,
    )

    failures = _execution_input_failures(final_root, authorization)
    assert any("registry" in item or "binding" in item for item in failures)


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
            "evidence_id": pl.String,
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
    historical_events = pl.DataFrame(
        schema={
            "effective_date": pl.Date,
            "source_symbol": pl.String,
            "event_type": pl.String,
            "target_symbol": pl.String,
            "ratio": pl.Float64,
            "cash_per_share": pl.Float64,
            "source": pl.String,
        }
    )
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
    monkeypatch.setattr(
        "ashare_multifactor.final_test.backtest._validate_final_coverage",
        lambda *_args, **_kwargs: None,
    )

    result = _resolve_verified_inputs(
        SimpleNamespace(files=(final_panel_path,)),
        _signals(),
        authorization=_authorization(),
        code_root=tmp_path,
        final_root=final_root,
        supported_markets=("sh", "sz"),
        verified_execution_inputs={
            "corporate_actions.parquet": final_actions_path,
            "security_events.parquet": final_events_path,
        },
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

    with pytest.raises(ValueError, match="corporate-action execution contract"):
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


def test_non_finite_reconciliation_and_nav_block_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ashare_multifactor.final_test.backtest import _execute_verified_inputs

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
            {"date": [date(2022, 1, 4)], "nav": [float("inf")]}
        ),
        "reconciliation": pl.DataFrame({"difference": [float("nan")]}),
        "scenario_reconciliation": pl.DataFrame({"difference": [0.0]}),
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
        lambda *_args, **_kwargs: ({"status": "ready"}, pl.DataFrame()),
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.backtest.audit_stale_positions",
        lambda *_args, **_kwargs: (
            {"status": "ready", "unexplained_symbols": 0},
            pl.DataFrame(),
        ),
    )

    result = _execute_verified_inputs(
        inputs,
        fees=object(),
        formal=config.formal_backtest,
    )

    assert result.publishable is False
    assert "non-finite reconciliation gate failed" in result.gate_failures
    assert "non-finite NAV gate failed" in result.gate_failures


@pytest.mark.parametrize(
    ("cash_per_share", "share_ratio"),
    [(float("inf"), 0.0), (0.0, float("nan"))],
)
def test_final_actions_require_finite_numeric_values(
    tmp_path: Path,
    cash_per_share: float,
    share_ratio: float,
) -> None:
    from ashare_multifactor.final_test.backtest import _load_final_actions

    path = tmp_path / "corporate_actions.parquet"
    pl.DataFrame(
        {
            "symbol": ["000001"],
            "ex_date": [date(2022, 1, 4)],
            "effective_date": [date(2022, 1, 4)],
            "cash_per_share": [cash_per_share],
            "share_ratio": [share_ratio],
            "source": ["synthetic"],
        }
    ).write_parquet(path)

    with pytest.raises(ValueError, match="corporate-action execution contract"):
        _load_final_actions(path)


def test_final_action_effective_date_cannot_precede_ex_date(tmp_path: Path) -> None:
    from ashare_multifactor.final_test.backtest import _load_final_actions

    path = tmp_path / "corporate_actions.parquet"
    pl.DataFrame(
        {
            "symbol": ["000001"],
            "ex_date": [date(2022, 1, 5)],
            "effective_date": [date(2022, 1, 4)],
            "cash_per_share": [0.1],
            "share_ratio": [0.0],
            "source": ["synthetic"],
        }
    ).write_parquet(path)

    with pytest.raises(ValueError, match="corporate-action execution contract"):
        _load_final_actions(path)


def _complete_final_calendar() -> pl.DataFrame:
    dates = [
        date(year, month, 28)
        for year in range(2022, 2026)
        for month in range(1, 13)
    ]
    dates[-1] = date(2025, 12, 30)
    return pl.DataFrame({"date": dates})


def _complete_final_source() -> SimpleNamespace:
    return SimpleNamespace(
        manifest={
            "min_date": "2022-01-28",
            "max_date": "2025-12-30",
            "years": [2022, 2023, 2024, 2025],
        }
    )


def test_partial_year_manifest_cannot_masquerade_as_complete_final_period() -> None:
    from ashare_multifactor.final_test import backtest

    source = SimpleNamespace(
        manifest={
            "min_date": "2022-01-03",
            "max_date": "2023-12-29",
            "years": [2022, 2023],
        }
    )
    calendar = pl.DataFrame(
        {"date": [date(2022, 1, 31), date(2023, 12, 29)]}
    )

    with pytest.raises(ValueError, match="all four final-test years"):
        backtest._validate_final_coverage(
            source,
            calendar,
            calendar,
            calendar,
        )


def test_final_calendar_requires_all_48_continuous_months() -> None:
    from ashare_multifactor.final_test import backtest

    source = SimpleNamespace(
        manifest={
            "min_date": "2022-12-30",
            "max_date": "2025-01-27",
            "years": [2022, 2023, 2024, 2025],
        }
    )
    calendar = pl.DataFrame(
        {
            "date": [
                date(2022, 12, 30),
                date(2023, 6, 30),
                date(2024, 6, 28),
                date(2025, 1, 27),
            ]
        }
    )
    execution = pl.DataFrame({"date": [date(2025, 1, 27)]})

    with pytest.raises(ValueError, match="48 continuous final-test months"):
        backtest._validate_final_coverage(
            source,
            calendar,
            execution,
            calendar,
        )


def test_final_execution_rows_must_reach_manifest_last_trading_day() -> None:
    from ashare_multifactor.final_test import backtest

    calendar = _complete_final_calendar()
    targets = calendar.clone()
    execution = pl.DataFrame({"date": [date(2025, 12, 29)]})

    with pytest.raises(ValueError, match="manifest last trading day"):
        backtest._validate_final_coverage(
            _complete_final_source(),
            calendar,
            execution,
            targets,
        )


def test_final_targets_cover_every_manifest_month_end_rebalance() -> None:
    from ashare_multifactor.final_test import backtest

    calendar = _complete_final_calendar()
    incomplete_targets = calendar.head(calendar.height - 1)
    execution = pl.DataFrame({"date": [date(2025, 12, 30)]})

    with pytest.raises(ValueError, match="frozen month-end rebalance schedule"):
        backtest._validate_final_coverage(
            _complete_final_source(),
            calendar,
            execution,
            incomplete_targets,
        )


def test_second_validation_resolution_must_match_the_authorized_seal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ashare_multifactor.final_test.backtest import (
        _resolve_pretest_execution_inputs,
    )

    authorization = _authorization()
    robustness_artifacts = tmp_path / "robustness/artifacts"
    robustness_artifacts.mkdir(parents=True)
    (robustness_artifacts / "sealed_test_protocol.json").write_text(
        json.dumps(
            {
                "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
                "upstream_validation": {
                    "run_id": "authorized-validation",
                    "manifest_sha256": "d" * 64,
                },
            }
        ),
        encoding="utf-8",
    )
    robustness = SimpleNamespace(
        run_id=authorization.robustness_release,
        artifacts=robustness_artifacts,
    )
    substituted_validation = SimpleNamespace(
        run_id="substituted-validation",
        manifest_sha256="e" * 64,
    )
    empty = tmp_path / "empty.parquet"
    pl.DataFrame().write_parquet(empty)
    monkeypatch.setattr(
        "ashare_multifactor.final_test.backtest._load_frozen_config",
        lambda *_args: _config(tmp_path),
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.backtest.resolve_final_test_signal_inputs",
        lambda *_args, **_kwargs: SimpleNamespace(
            historical_daily=pl.DataFrame()
        ),
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.backtest.resolve_current",
        lambda path: (
            robustness if path.name == "robustness" else substituted_validation
        ),
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.backtest._release_file",
        lambda *_args: empty,
    )

    with pytest.raises(ValueError, match="validation release differs from authorized seal"):
        _resolve_pretest_execution_inputs(
            authorization,
            code_root=tmp_path,
            data_root=tmp_path,
        )
