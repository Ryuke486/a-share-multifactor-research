from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import polars as pl
import pytest

from ashare_multifactor.audit.publication import resolve_current
from ashare_multifactor.final_test.backtest import FinalTestBacktestResult
from ashare_multifactor.final_test.gate import FinalTestAuthorization
from ashare_multifactor.final_test.execution_sources import _normalize_final_dividends
from ashare_multifactor.final_test.pipeline import (
    FinalTestPipelineSteps,
    run_final_test_release,
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

    with pytest.raises(ValueError, match="already succeeded"):
        run_final_test_release(
            code_root=Path.cwd(),
            data_root=tmp_path,
            opening_token_path=tmp_path / "second-token.json",
            approval_key=b"synthetic-approval-key",
            attempt_id="attempt-002",
            run_id="another-release",
            steps=_steps(publishable=True),
        )


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
