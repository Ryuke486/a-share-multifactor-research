from __future__ import annotations

from datetime import date
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from ashare_multifactor.audit.publication import resolve_current
from ashare_multifactor.audit.records import file_record
from ashare_multifactor.data.security import market_for_symbol
from ashare_multifactor.final_test.backtest import FinalTestBacktestResult
from ashare_multifactor.final_test.gate import FinalTestAuthorization
from ashare_multifactor.final_test.execution_sources import (
    _normalize_final_dividends,
    _validate_coverage_markets,
    _validate_event_counts,
    validate_security_event_coverage,
)
from ashare_multifactor.final_test.pipeline import (
    FinalTestPipelineSteps,
    _assert_reusable_data_claim,
    _assert_safe_roots,
    _copy_release_inputs,
    _recover_or_archive_data_claim,
    _validate_publication_id,
    run_final_test_release,
)
from ashare_multifactor.final_test.registry import (
    append_prepared_publication,
    recover_prepared_publication,
)
from ashare_multifactor.final_test.release_outputs import (
    _historical_backtest_root,
    _slice_backtest_period,
)
from ashare_multifactor.final_test.signals import FinalTestSignals


def _authorization(attempt_id: str = "attempt-001") -> FinalTestAuthorization:
    return FinalTestAuthorization(
        attempt_id=attempt_id,
        approval_id="approval-001",
        registered_at="2026-07-16T00:00:00+00:00",
        git_commit="a" * 40,
        git_tree="b" * 40,
        sealed_protocol_sha256="c" * 64,
        robustness_release="stage8-successor",
        test_period=(date(2022, 1, 1), date(2025, 12, 31)),
    )


def _signals() -> FinalTestSignals:
    empty = pl.DataFrame()
    return FinalTestSignals(empty, empty, empty, empty)


def _steps(*, publishable: bool) -> FinalTestPipelineSteps:
    def authorize(**_kwargs: object) -> FinalTestAuthorization:
        return _authorization()

    def build_data(*_args: object, **_kwargs: object) -> object:
        return object()

    def build_execution_inputs(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"status": "ready", "files": []}

    def build_signals(*_args: object, **_kwargs: object) -> FinalTestSignals:
        return _signals()

    def backtest(*_args: object, **_kwargs: object) -> FinalTestBacktestResult:
        return FinalTestBacktestResult(
            outputs={"nav": pl.DataFrame({"date": [date(2025, 12, 31)], "nav": [1.0]})},
            audits={"maximum_reconciliation_difference": 0.0},
            preflight={"status": "ready", "execution_started": True},
            publishable=publishable,
            gate_failures=() if publishable else ("shadow NAV gate failed",),
        )

    def metrics(*_args: object, **_kwargs: object) -> tuple[pl.DataFrame, pl.DataFrame]:
        metric = pl.DataFrame(
            {
                "period": ["test"],
                "scope": ["portfolio"],
                "entity": ["main"],
                "metric": ["annual_return"],
                "value": [-0.1],
            }
        )
        comparison = metric.select(
            "scope",
            "entity",
            "metric",
            pl.lit(0.1).alias("research"),
            pl.lit(0.05).alias("validation"),
            pl.col("value").alias("test"),
        )
        return metric, comparison

    def report(*_args: object, **_kwargs: object) -> str:
        return "# sealed final-test report\n"

    return FinalTestPipelineSteps(
        authorize=authorize,
        build_data=build_data,
        build_execution_inputs=build_execution_inputs,
        build_signals=build_signals,
        run_backtest=backtest,
        build_metrics=metrics,
        render_report=report,
    )


def test_success_publishes_immutable_release_and_locks_another_attempt(
    tmp_path: Path,
) -> None:
    result = run_final_test_release(
        code_root=Path.cwd(),
        data_root=tmp_path,
        opening_token_path=tmp_path / "token.json",
        approval_key=b"synthetic-approval-key",
        attempt_id="attempt-001",
        run_id="final-release",
        steps=_steps(publishable=True),
    )

    assert result.publishable is True
    assert result.release is not None
    current = resolve_current(tmp_path / "processed/final_test")
    assert current.run_id == "final-release"
    manifest = json.loads(current.manifest.read_text(encoding="utf-8"))
    lineage = json.loads(current.lineage.read_text(encoding="utf-8"))
    assert manifest["period"] == ["2022-01-01", "2025-12-31"]
    assert manifest["sealed_protocol_sha256"] == "c" * 64
    assert lineage["authorization"]["attempt_id"] == "attempt-001"
    assert lineage["authorization"]["git_commit"] == "a" * 40
    assert lineage["upstream"]["robustness_release"] == "stage8-successor"
    assert lineage["execution"]["started_at"]
    assert lineage["execution"]["completed_at"]
    checkpoint = json.loads(
        (current.artifacts / "checkpoint_c.json").read_text(encoding="utf-8")
    )
    assert checkpoint == {
        "delivery_started": False,
        "development_stopped": True,
        "status": "required",
    }
    outcome = json.loads(
        (tmp_path / "processed/final_test/attempts/attempt-001.outcome.json").read_text()
    )
    assert outcome["status"] == "succeeded"
    assert outcome["authoritative"] is True
    (tmp_path / "processed/final_test/attempts/attempt-001.outcome.json").unlink()

    recovered = run_final_test_release(
        code_root=Path.cwd(),
        data_root=tmp_path,
        opening_token_path=tmp_path / "second-token.json",
        approval_key=b"synthetic-approval-key",
        attempt_id="attempt-002",
        run_id="another-release",
        steps=_steps(publishable=True),
    )
    assert recovered.release == current
    assert recovered.attempt_id == "attempt-001"


def test_non_publishable_attempt_is_retained_without_switching_current(
    tmp_path: Path,
) -> None:
    result = run_final_test_release(
        code_root=Path.cwd(),
        data_root=tmp_path,
        opening_token_path=tmp_path / "token.json",
        approval_key=b"synthetic-approval-key",
        attempt_id="attempt-001",
        run_id="blocked-release",
        steps=_steps(publishable=False),
    )

    assert result.publishable is False
    assert result.release is None
    root = tmp_path / "processed/final_test"
    assert not (root / "CURRENT.json").exists()
    assert (root / "attempt_runs/attempt-001/artifacts/preflight.json").is_file()
    assert (root / "attempt_runs/attempt-001/attempt_manifest.json").is_file()
    outcome = json.loads((root / "attempts/attempt-001.outcome.json").read_text())
    assert outcome["status"] == "failed"
    assert outcome["reason"] == "shadow NAV gate failed"


def test_cli_exposes_only_the_authorized_one_shot_entrypoint() -> None:
    source = Path("src/ashare_multifactor/cli/final_test.py").read_text(encoding="utf-8")

    assert "opening-token" in source
    assert "approval-key-file" in source
    assert "security-event-coverage" in source
    assert "parameter" not in source
    assert "search" not in source


def test_final_execution_source_normalization_is_exactly_date_bounded() -> None:
    raw = pl.DataFrame(
        {
            "code": ["sz.000001"],
            "query_year": [2022],
            "query_year_type": ["operate"],
            "dividOperateDate": ["2022-06-01"],
            "dividPayDate": ["2022-06-08"],
            "dividStockMarketDate": [""],
            "dividCashPsBeforeTax": ["0.10"],
            "dividStocksPs": ["0"],
            "dividReserveToStockPs": ["0"],
        }
    )

    actions = _normalize_final_dividends(raw, symbols={"000001"})

    assert actions.get_column("ex_date").to_list() == [date(2022, 6, 1)]
    assert actions.get_column("effective_date").to_list() == [date(2022, 6, 8)]
    with pytest.raises(ValueError, match="sealed final-test query"):
        _normalize_final_dividends(
            raw.with_columns(pl.lit(2026).alias("query_year")),
            symbols={"000001"},
        )


def test_portfolio_inputs_use_boundary_nav_and_strict_period_slices() -> None:
    frames = {
        "nav": pl.DataFrame(
            {
                "date": [date(2004, 12, 31), date(2005, 1, 4), date(2017, 1, 3)],
                "nav": [100.0, 101.0, 999.0],
            }
        ),
        "orders": pl.DataFrame(
            {
                "order_id": ["old", "cross", "late"],
                "signal_date": [date(2004, 12, 31), date(2004, 12, 31), date(2017, 1, 3)],
                "quantity": [10, 20, 30],
                "remaining_quantity": [0, 20, 30],
            }
        ),
        "order_events": pl.DataFrame(
            {
                "order_id": ["old", "cross", "cross", "late"],
                "date": [date(2004, 12, 31), date(2005, 1, 4), date(2005, 1, 5), date(2017, 1, 3)],
                "event_seq": [1, 1, 2, 1],
                "remaining_quantity": [0, 20, 5, 30],
                "status": ["filled", "submitted", "partial", "submitted"],
            }
        ),
        "trades": pl.DataFrame(
            {"date": [date(2004, 12, 31), date(2005, 1, 4), date(2017, 1, 3)]}
        ),
        "target_diagnostics": pl.DataFrame(
            {"date": [date(2004, 12, 31), date(2005, 1, 4), date(2017, 1, 3)]}
        ),
    }

    sliced = _slice_backtest_period(
        frames, start=date(2005, 1, 1), end=date(2016, 12, 31)
    )

    assert sliced["nav"].get_column("date").to_list() == [
        date(2004, 12, 31),
        date(2005, 1, 4),
    ]
    assert sliced["orders"].get_column("order_id").to_list() == ["cross"]
    assert sliced["orders"].get_column("remaining_quantity").to_list() == [5]


def test_research_and_validation_use_same_stage7_continuous_candidate() -> None:
    validation = SimpleNamespace(datasets=Path("validation/datasets"))

    research = _historical_backtest_root(validation, period="research")
    holdout = _historical_backtest_root(validation, period="validation")

    expected = Path(
        "validation/datasets/backtests/rolling_ic_family_size_stratified_buffered"
    )
    assert research == expected
    assert holdout == expected


def test_security_event_zero_rows_still_require_ready_official_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "official.json"
    evidence.write_text('{"source":"official"}\n', encoding="utf-8")
    evidence_index = tmp_path / "evidence_index.parquet"
    pl.DataFrame(
        {
            "evidence_id": ["ev-1"], "source": ["szse"], "market": ["sz"],
            "source_url": ["https://disc.static.szse.cn/download/disc/security.pdf"],
            "cache_file": [evidence.name],
            "sha256": [file_record(evidence, root=tmp_path, role="x").sha256],
        }
    ).write_parquet(evidence_index)
    query_coverage = tmp_path / "query_coverage.parquet"
    pl.DataFrame(
        {
            "symbol": ["000001"], "source": ["szse"], "market": ["sz"],
            "query_start": [date(2022, 1, 1)], "query_end": [date(2025, 12, 31)],
            "status": ["ok"], "event_count": [0], "evidence_id": ["ev-1"],
        }
    ).write_parquet(query_coverage)
    coverage = tmp_path / "coverage.json"
    coverage.write_text(
        json.dumps(
            {
                "status": "ready",
                "period": ["2022-01-01", "2025-12-31"],
                "scope": "all_final_execution_symbols",
                "symbol_count": 1,
                "symbols_sha256": hashlib.sha256(b"000001\n").hexdigest(),
                "event_rows": 0,
                "evidence_index": file_record(
                    evidence_index,
                    root=tmp_path,
                    role="official_security_event_evidence_index",
                ).to_dict(),
                "coverage": [
                    file_record(
                        query_coverage,
                        root=tmp_path,
                        role="official_security_event_coverage",
                    ).to_dict()
                ],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    verified = validate_security_event_coverage(Path("coverage.json"), symbols=["000001"])
    assert verified["event_rows"] == 0
    assert verified["coverage_root"] == tmp_path.resolve()
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        validate_security_event_coverage(alias / "coverage.json", symbols=["000001"])
    pl.concat([pl.read_parquet(query_coverage)] * 2).write_parquet(query_coverage)
    with pytest.raises(ValueError, match="query coverage"):
        validate_security_event_coverage(coverage, symbols=["000001"])
    coverage.write_text('{"status":"not_ready"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="security-event coverage"):
        validate_security_event_coverage(coverage)


def test_all_mutable_final_roots_reject_symlink_ancestors(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    final_root = data_root / "processed/final_test"
    final_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (final_root / "attempt_inputs").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        _assert_safe_roots(data_root, final_root)


def test_failed_claim_without_panel_is_archived_before_retry(tmp_path: Path) -> None:
    root = tmp_path / "final_test"
    root.mkdir()
    claim = root / "data-build-claim.json"
    claim.write_text('{"attempt_id":"a","status":"failed"}\n')
    assert _recover_or_archive_data_claim(root) == "archived_failed"
    assert not claim.exists()
    assert len(list((root / "failed-claims").glob("*.json"))) == 1


def test_security_events_must_match_coverage_counts_by_source_key() -> None:
    coverage = pl.DataFrame(
        {
            "symbol": ["000001", "600000"],
            "market": ["sz", "sh"],
            "source": ["szse", "sse"],
            "event_count": [1, 0],
        }
    )
    events = pl.DataFrame(
        {
            "source_symbol": ["600000"],
            "market": ["sh"],
            "source": ["sse"],
        }
    )
    with pytest.raises(ValueError, match="event counts"):
        _validate_event_counts(coverage, events)


@pytest.mark.parametrize(
    ("symbol", "market"),
    [("000001", "sz"), ("300001", "sz"), ("600000", "sh"),
     ("688001", "sh"), ("430001", "bj"), ("830001", "bj"),
     ("920001", "bj"), ("900901", "sh")],
)
def test_authoritative_symbol_market_boundaries(symbol: str, market: str) -> None:
    assert market_for_symbol(symbol) == market


def test_coverage_market_must_match_source_symbol() -> None:
    invalid = pl.DataFrame(
        {"source_symbol": ["000001"], "market": ["sh"], "source": ["sse"]}
    )
    with pytest.raises(ValueError, match="symbol market"):
        _validate_coverage_markets(invalid)


def test_publishing_recovery_writes_prepared_then_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "final_test"
    panel = root / "daily_panel"
    panel.mkdir(parents=True)
    claim = {
        "attempt_id": "attempt-1",
        "approval_id": "approval-1",
        "git_commit": "a" * 40,
        "git_tree": "b" * 40,
        "sealed_protocol_sha256": "c" * 64,
        "robustness_release": "stage8-release",
        "status": "publishing",
        "data_manifest": {"sha256": "d" * 64},
    }
    (root / "data-build-claim.json").write_text(json.dumps(claim))
    monkeypatch.setattr(
        "ashare_multifactor.final_test.pipeline.resolve_final_test_data_panel",
        lambda _root: SimpleNamespace(requires_recovery=True),
    )

    assert _recover_or_archive_data_claim(root) == "recovered_published"
    recovery = root / "data-recovery"
    assert (recovery / "attempt-1.prepared.json").is_file()
    assert (recovery / "attempt-1.completed.json").is_file()
    published = json.loads((root / "data-build-claim.json").read_text())
    assert published["status"] == "published"

    (recovery / "attempt-1.completed.json").unlink()
    assert _recover_or_archive_data_claim(root) == "recovered_published"
    assert (recovery / "attempt-1.completed.json").is_file()


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("attempt_id", "attempt-2"),
        ("approval_id", "approval-2"),
        ("git_commit", "f" * 40),
        ("git_tree", "e" * 40),
        ("sealed_protocol_sha256", "1" * 64),
        ("robustness_release", "replacement-release"),
        ("data_manifest", {"sha256": "2" * 64}),
    ],
)
def test_prepared_recovery_rejects_mutated_publishing_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: object,
) -> None:
    root = tmp_path / "final_test"
    (root / "daily_panel").mkdir(parents=True)
    claim_path = root / "data-build-claim.json"
    claim = {
        "attempt_id": "attempt-1",
        "approval_id": "approval-1",
        "git_commit": "a" * 40,
        "git_tree": "b" * 40,
        "sealed_protocol_sha256": "c" * 64,
        "robustness_release": "stage8-release",
        "status": "publishing",
        "data_manifest": {"sha256": "d" * 64},
    }
    claim_path.write_text(json.dumps(claim))
    monkeypatch.setattr(
        "ashare_multifactor.final_test.pipeline.resolve_final_test_data_panel",
        lambda _root: SimpleNamespace(requires_recovery=True),
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.pipeline._write_json_atomic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("crash")),
    )
    with pytest.raises(RuntimeError, match="crash"):
        _recover_or_archive_data_claim(root)

    claim[field] = replacement
    claim_path.write_text(json.dumps(claim))
    with pytest.raises(ValueError, match="prepared data recovery"):
        _recover_or_archive_data_claim(root)


@pytest.mark.parametrize("value", ["../escape", "/absolute", "a/b", "a..b", "."])
def test_publication_identifiers_reject_escape_forms(value: str) -> None:
    with pytest.raises(ValueError, match="identifier"):
        _validate_publication_id(value)


def test_prepared_publication_recovers_from_verified_current(tmp_path: Path) -> None:
    registry = tmp_path / "attempts"
    append_prepared_publication(
        registry,
        attempt_id="attempt-001",
        release_run_id="release-001",
        sealed_protocol_sha256="c" * 64,
    )
    current = {
        "run_id": "release-001",
        "manifest_sha256": "d" * 64,
    }

    outcome = recover_prepared_publication(
        registry,
        attempt_id="attempt-001",
        current=current,
    )

    assert outcome["status"] == "succeeded"
    assert outcome["release_manifest_sha256"] == "d" * 64


def test_release_copies_final_data_and_execution_evidence(tmp_path: Path) -> None:
    panel = tmp_path / "daily_panel"
    panel.mkdir()
    (panel / "data_manifest.json").write_text("{}\n")
    (panel / "input_files.json").write_text("{}\n")
    execution = tmp_path / "attempt_inputs"
    execution.mkdir()
    (execution / "manifest.json").write_text("{}\n")
    (execution / "coverage.json").write_text("{}\n")
    datasets = tmp_path / "staged/datasets"
    artifacts = tmp_path / "staged/artifacts"
    datasets.mkdir(parents=True)
    artifacts.mkdir()

    _copy_release_inputs(
        panel_root=panel,
        execution_root=execution,
        datasets=datasets,
        artifacts=artifacts,
    )

    assert (datasets / "final_daily_panel/data_manifest.json").is_file()
    assert (datasets / "final_daily_panel/input_files.json").is_file()
    assert (artifacts / "execution_inputs/coverage.json").is_file()


def test_failed_attempt_data_reuse_requires_same_seal_git_and_raw_inventory() -> None:
    authorization = _authorization("attempt-002")
    claim = {
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "git_commit": authorization.git_commit,
        "git_tree": authorization.git_tree,
        "robustness_release": authorization.robustness_release,
    }
    inventory = {"files": [{"path": "2022.csv", "sha256": "d" * 64}]}

    _assert_reusable_data_claim(claim, authorization, inventory, inventory)
    with pytest.raises(ValueError, match="raw inventory"):
        _assert_reusable_data_claim(
            claim,
            authorization,
            inventory,
            {"files": [{"path": "2022.csv", "sha256": "e" * 64}]},
        )
