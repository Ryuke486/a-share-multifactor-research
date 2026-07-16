from __future__ import annotations

from datetime import date
import inspect
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from ashare_multifactor.audit.publication import publish_release, resolve_current
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
    _assert_reusable_data_claim,
    _assert_release_date_bounds,
    _assert_safe_roots,
    _copy_release_inputs,
    _recover_or_archive_data_claim,
    _slice_audit_frame,
    _validate_publication_id,
    run_final_test_release,
)
from ashare_multifactor.final_test.registry import (
    append_prepared_publication,
    recover_prepared_publication,
)
from ashare_multifactor.final_test.release_outputs import (
    _historical_backtest_root,
    _resolve_historical_releases,
    _resolve_authorized_robustness,
    _slice_backtest_period,
)
from ashare_multifactor.final_test.signals import FinalTestSignals


@pytest.mark.parametrize("changed", ["manifest.json", "lineage.json"])
def test_authorized_stage8_identity_rejects_post_authorization_change(
    tmp_path: Path, changed: str
) -> None:
    datasets = tmp_path / "datasets"
    artifacts = tmp_path / "artifacts"
    datasets.mkdir()
    artifacts.mkdir()
    (artifacts / "sealed_test_protocol.json").write_text("{}")
    release = publish_release(
        tmp_path / "processed/robustness", run_id="stage8-successor",
        staged_datasets=datasets, staged_artifacts=artifacts,
        lineage={"stage": "robustness"},
    )
    authorization = _authorization()
    authorization = type(authorization)(
        **{
            **authorization.__dict__,
            "robustness_manifest_sha256": release.manifest_sha256,
            "robustness_lineage_sha256": hashlib.sha256(
                release.lineage.read_bytes()
            ).hexdigest(),
        }
    )
    (release.root / changed).write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="authorized Stage-8"):
        _resolve_authorized_robustness(tmp_path, authorization)


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
    bounded = pl.DataFrame({"date": [date(2022, 1, 4)]})
    return FinalTestSignals(bounded, bounded, bounded, bounded)


def _patch_steps(monkeypatch: pytest.MonkeyPatch, *, publishable: bool) -> None:
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

    for name, value in {
        "authorize_final_test": authorize,
        "build_or_reuse_final_test_daily_panel": build_data,
        "build_final_execution_inputs": build_execution_inputs,
        "build_final_test_signals": build_signals,
        "run_final_test_backtest": backtest,
        "build_final_metrics": metrics,
        "build_final_report": report,
        "_copy_release_inputs": lambda **_kwargs: None,
        "resolve_final_test_data_panel": lambda final_root: SimpleNamespace(
            root=final_root / "daily_panel"
        ),
        "_upstream_identity": lambda *_args, **_kwargs: {
            "robustness_release": "stage8-successor",
            "self_contained_inputs": [],
        },
    }.items():
        monkeypatch.setattr(f"ashare_multifactor.final_test.pipeline.{name}", value)


def test_success_publishes_immutable_release_and_locks_another_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_steps(monkeypatch, publishable=True)
    result = run_final_test_release(
        code_root=Path.cwd(),
        data_root=tmp_path,
        opening_token_path=tmp_path / "token.json",
        approval_key=b"synthetic-approval-key",
        attempt_id="attempt-001",
        run_id="final-release",
        security_event_coverage_path=tmp_path / "security-coverage.json",
        corporate_action_coverage_root=tmp_path / "action-coverage",
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
        security_event_coverage_path=tmp_path / "security-coverage.json",
        corporate_action_coverage_root=tmp_path / "action-coverage",
    )
    assert recovered.release == current
    assert recovered.attempt_id == "attempt-001"


def test_date_gate_failure_cannot_leave_publishable_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_steps(monkeypatch, publishable=True)
    monkeypatch.setattr(
        "ashare_multifactor.final_test.pipeline._assert_release_date_bounds",
        lambda _root: (_ for _ in ()).throw(
            ValueError("artifact outside final-test period")
        ),
    )
    with pytest.raises(ValueError, match="artifact outside"):
        run_final_test_release(
            code_root=Path.cwd(), data_root=tmp_path,
            opening_token_path=tmp_path / "token.json",
            approval_key=b"synthetic-approval-key", attempt_id="attempt-001",
            run_id="final-release",
            security_event_coverage_path=tmp_path / "security.json",
            corporate_action_coverage_root=tmp_path / "actions",
        )
    attempt = tmp_path / "processed/final_test/attempt_runs/attempt-001"
    assert json.loads((attempt / "attempt_manifest.json").read_text())["status"] == "failed"
    outcome = tmp_path / "processed/final_test/attempts/attempt-001.outcome.json"
    assert json.loads(outcome.read_text())["status"] == "failed"


def test_non_publishable_attempt_is_retained_without_switching_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_steps(monkeypatch, publishable=False)
    result = run_final_test_release(
        code_root=Path.cwd(),
        data_root=tmp_path,
        opening_token_path=tmp_path / "token.json",
        approval_key=b"synthetic-approval-key",
        attempt_id="attempt-001",
        run_id="blocked-release",
        security_event_coverage_path=tmp_path / "security-coverage.json",
        corporate_action_coverage_root=tmp_path / "action-coverage",
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
    assert "corporate-action-coverage" in source
    assert "parameter" not in source
    assert "search" not in source


def test_final_pipeline_signature_has_no_security_step_injection() -> None:
    parameters = inspect.signature(run_final_test_release).parameters
    assert "steps" not in parameters
    assert "security_event_coverage_path" in parameters
    assert "corporate_action_coverage_root" in parameters


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


def test_release_date_scan_rejects_pretest_rows(tmp_path: Path) -> None:
    pl.DataFrame(
        {"date": [date(2021, 12, 31)], "symbol": ["000001"]}
    ).write_parquet(tmp_path / "leaked.parquet")
    with pytest.raises(ValueError, match="leaked.parquet.*outside"):
        _assert_release_date_bounds(tmp_path)


def test_release_date_scan_rejects_early_artifact_rows(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    pl.DataFrame({"start_date": [date(2021, 12, 31)]}).write_parquet(
        artifacts / "stale_intervals.parquet"
    )
    with pytest.raises(ValueError, match="artifacts/stale_intervals.parquet.*outside"):
        _assert_release_date_bounds(tmp_path)


def test_stale_interval_slice_keeps_and_clips_cross_boundary_rows() -> None:
    frame = pl.DataFrame(
        {
            "symbol": ["a", "b", "c"],
            "first_stale_date": [date(2021, 12, 1), date(2025, 12, 1), date(2021, 1, 1)],
            "last_stale_date": [date(2022, 1, 5), date(2026, 1, 5), date(2021, 2, 1)],
        }
    )
    sliced = _slice_audit_frame("stale_intervals", frame)
    assert sliced.get_column("symbol").to_list() == ["a", "b"]
    assert sliced.get_column("first_stale_date").to_list() == [
        date(2022, 1, 1), date(2025, 12, 1)
    ]
    assert sliced.get_column("last_stale_date").to_list() == [
        date(2022, 1, 5), date(2025, 12, 31)
    ]


def test_research_and_validation_use_same_stage7_continuous_candidate() -> None:
    validation = SimpleNamespace(datasets=Path("validation/datasets"))

    research = _historical_backtest_root(validation, period="research")
    holdout = _historical_backtest_root(validation, period="validation")

    expected = Path(
        "validation/datasets/backtests/rolling_ic_family_size_stratified_buffered"
    )
    assert research == expected
    assert holdout == expected


def test_historical_releases_ignore_drifting_current_pointers(tmp_path: Path) -> None:
    empty_datasets = tmp_path / "empty-datasets"
    empty_artifacts = tmp_path / "empty-artifacts"
    empty_datasets.mkdir()
    empty_artifacts.mkdir()
    stage5_base = tmp_path / "processed/factor_combination"
    bound_stage5 = publish_release(
        stage5_base,
        run_id="stage5-bound",
        staged_datasets=empty_datasets,
        staged_artifacts=empty_artifacts,
        lineage={"stage": 5},
    )
    publish_release(
        stage5_base,
        run_id="stage5-drift",
        staged_datasets=empty_datasets,
        staged_artifacts=empty_artifacts,
        lineage={"stage": "drift"},
    )
    validation_lineage = {
        "upstream": {
            "stage_five_manifest": file_record(
                bound_stage5.manifest, root=tmp_path, role="stage_five_manifest"
            ).to_dict(),
            "stage_five_lineage": file_record(
                bound_stage5.lineage, root=tmp_path, role="stage_five_lineage"
            ).to_dict(),
        }
    }
    validation_base = tmp_path / "processed/validation_evaluation"
    bound_validation = publish_release(
        validation_base,
        run_id="validation-bound",
        staged_datasets=empty_datasets,
        staged_artifacts=empty_artifacts,
        lineage=validation_lineage,
    )
    publish_release(
        validation_base,
        run_id="validation-drift",
        staged_datasets=empty_datasets,
        staged_artifacts=empty_artifacts,
        lineage={"stage": "drift"},
    )

    stage5, validation = _resolve_historical_releases(
        tmp_path,
        {
            "upstream_validation": {
                "run_id": bound_validation.run_id,
                "manifest_sha256": bound_validation.manifest_sha256,
            }
        },
        {
            "inputs": {
                "validation_manifest": {
                    "sha256": bound_validation.manifest_sha256,
                    "size": bound_validation.manifest.stat().st_size,
                }
            }
        },
    )

    assert stage5.run_id == "stage5-bound"
    assert validation.run_id == "validation-bound"


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


@pytest.mark.parametrize("symbol", ["920001", "830001", "430001"])
def test_bse_security_event_coverage_is_rejected(tmp_path: Path, symbol: str) -> None:
    coverage = _write_security_event_coverage(
        tmp_path,
        symbol=symbol,
        market="bj",
        source="bse",
        source_url="https://www.bse.cn/disclosure/2024/2024-08-16/notice.pdf",
    )
    with pytest.raises(ValueError, match="evidence source|market source"):
        validate_security_event_coverage(coverage, symbols=[symbol])


def test_bse_symbol_rejects_self_consistent_wrong_exchange(tmp_path: Path) -> None:
    coverage = _write_security_event_coverage(
        tmp_path,
        symbol="920001",
        market="sh",
        source="sse",
        source_url=(
            "https://www.sse.com.cn/disclosure/listedinfo/announcement/notice.pdf"
        ),
    )
    with pytest.raises(ValueError, match="symbol market"):
        validate_security_event_coverage(coverage, symbols=["920001"])


def test_bse_evidence_rejects_non_bse_official_url(tmp_path: Path) -> None:
    coverage = _write_security_event_coverage(
        tmp_path,
        symbol="920001",
        market="bj",
        source="bse",
        source_url=(
            "https://www.sse.com.cn/disclosure/listedinfo/announcement/notice.pdf"
        ),
    )
    with pytest.raises(ValueError, match="evidence source"):
        validate_security_event_coverage(coverage, symbols=["920001"])


def _write_security_event_coverage(
    root: Path,
    *,
    symbol: str,
    market: str,
    source: str,
    source_url: str,
) -> Path:
    evidence = root / "official.pdf"
    evidence.write_bytes(b"official")
    evidence_index = root / "evidence_index.parquet"
    pl.DataFrame(
        {
            "evidence_id": ["ev-1"],
            "source": [source],
            "market": [market],
            "source_url": [source_url],
            "cache_file": [evidence.name],
            "sha256": [file_record(evidence, root=root, role="x").sha256],
        }
    ).write_parquet(evidence_index)
    query_coverage = root / "query_coverage.parquet"
    pl.DataFrame(
        {
            "symbol": [symbol],
            "source": [source],
            "market": [market],
            "query_start": [date(2022, 1, 1)],
            "query_end": [date(2025, 12, 31)],
            "status": ["ok"],
            "event_count": [1],
            "evidence_id": ["ev-1"],
        }
    ).write_parquet(query_coverage)
    events = root / "security_events.parquet"
    pl.DataFrame(
        {
            "source_symbol": [symbol],
            "effective_date": [date(2024, 8, 16)],
            "source": [source],
            "evidence_id": ["ev-1"],
        }
    ).write_parquet(events)
    coverage = root / "coverage.json"
    coverage.write_text(
        json.dumps(
            {
                "status": "ready",
                "period": ["2022-01-01", "2025-12-31"],
                "scope": "all_final_execution_symbols",
                "symbol_count": 1,
                "symbols_sha256": hashlib.sha256(f"{symbol}\n".encode()).hexdigest(),
                "event_rows": 1,
                "evidence_index": file_record(
                    evidence_index,
                    root=root,
                    role="official_security_event_evidence_index",
                ).to_dict(),
                "coverage": [
                    file_record(
                        query_coverage,
                        root=root,
                        role="official_security_event_coverage",
                    ).to_dict()
                ],
                "events_file": file_record(
                    events,
                    root=root,
                    role="official_security_events",
                ).to_dict(),
            }
        ),
        encoding="utf-8",
    )
    return coverage


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
        "robustness_manifest_sha256": "e" * 64,
        "robustness_lineage_sha256": "f" * 64,
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
        ("robustness_manifest_sha256", "3" * 64),
        ("robustness_lineage_sha256", "4" * 64),
        ("robustness_manifest_sha256", None),
        ("robustness_lineage_sha256", None),
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
        "robustness_manifest_sha256": "e" * 64,
        "robustness_lineage_sha256": "f" * 64,
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
    expected_error = (
        "authorization identity is incomplete"
        if replacement is None
        else "prepared data recovery"
    )
    with pytest.raises(ValueError, match=expected_error):
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
        "robustness_manifest_sha256": authorization.robustness_manifest_sha256,
        "robustness_lineage_sha256": authorization.robustness_lineage_sha256,
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
