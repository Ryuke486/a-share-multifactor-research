from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import version
import json
from pathlib import Path
import platform

import polars as pl

from ashare_multifactor.audit.publication import PublishedRelease, publish_release, resolve_current
from ashare_multifactor.audit.records import file_record
from ashare_multifactor.final_test.backtest import (
    FinalTestBacktestResult,
    run_final_test_backtest,
)
from ashare_multifactor.final_test.data_extension import build_final_test_daily_panel
from ashare_multifactor.final_test.execution_sources import build_final_execution_inputs
from ashare_multifactor.final_test.gate import (
    FINAL_TEST_END,
    FINAL_TEST_START,
    FinalTestAuthorization,
    authorize_final_test,
)
from ashare_multifactor.final_test.registry import (
    append_attempt_outcome,
    assert_no_authoritative_success,
)
from ashare_multifactor.final_test.release_outputs import (
    build_final_metrics,
    build_final_report,
)
from ashare_multifactor.final_test.signals import (
    FinalTestSignals,
    build_final_test_signals,
    _load_frozen_config,
)


@dataclass(frozen=True)
class FinalTestPipelineResult:
    attempt_id: str
    publishable: bool
    attempt_root: Path
    release: PublishedRelease | None
    gate_failures: tuple[str, ...]


@dataclass(frozen=True)
class FinalTestPipelineSteps:
    """Internal seam for synthetic tests; the CLI always uses the sealed defaults."""

    authorize: Callable[..., FinalTestAuthorization]
    build_data: Callable[..., object]
    build_execution_inputs: Callable[..., Mapping[str, object]]
    build_signals: Callable[..., FinalTestSignals]
    run_backtest: Callable[..., FinalTestBacktestResult]
    build_metrics: Callable[..., tuple[pl.DataFrame, pl.DataFrame]]
    render_report: Callable[..., str]


def run_final_test_release(
    *,
    code_root: Path,
    data_root: Path,
    opening_token_path: Path,
    approval_key: bytes,
    attempt_id: str | None = None,
    run_id: str | None = None,
    steps: FinalTestPipelineSteps | None = None,
) -> FinalTestPipelineResult:
    """Run and publish the single authorized 2022--2025 final-test attempt."""
    code_root = code_root.resolve()
    data_root = data_root.resolve()
    final_root = data_root / "processed/final_test"
    registry_root = final_root / "attempts"
    if (final_root / "CURRENT.json").is_file():
        resolve_current(final_root)
        raise ValueError("authoritative final-test run already succeeded")
    assert_no_authoritative_success(registry_root)
    selected = steps or _default_steps()
    authorization = selected.authorize(
        code_root=code_root,
        robustness_root=data_root / "processed/robustness",
        validation_root=data_root / "processed/validation_evaluation",
        opening_token_path=opening_token_path,
        approval_key=approval_key,
        registry_root=registry_root,
        requested_start=FINAL_TEST_START,
        requested_end=FINAL_TEST_END,
        attempt_id=attempt_id,
    )
    _assert_authorization(authorization)
    actual_run_id = run_id or authorization.attempt_id
    attempt_root = final_root / "attempt_runs" / authorization.attempt_id
    if attempt_root.exists() or attempt_root.is_symlink():
        raise FileExistsError(f"final-test attempt artifacts already exist: {attempt_root}")
    datasets = attempt_root / "datasets"
    artifacts = attempt_root / "artifacts"
    datasets.mkdir(parents=True)
    artifacts.mkdir()
    started_at = datetime.now(timezone.utc).isoformat()
    published_release: PublishedRelease | None = None
    try:
        config = _load_frozen_config(code_root, data_root)
        selected.build_data(
            config,
            authorization,
            FINAL_TEST_START,
            FINAL_TEST_END,
            code_root=code_root,
        )
        execution_manifest = selected.build_execution_inputs(
            authorization,
            code_root=code_root,
            data_root=data_root,
            final_root=final_root,
        )
        signals = selected.build_signals(
            authorization,
            code_root=code_root,
            final_root=final_root,
        )
        backtest = selected.run_backtest(
            authorization,
            signals,
            code_root=code_root,
            final_root=final_root,
        )
        _write_attempt_core(datasets, artifacts, signals, backtest)
        if not backtest.publishable:
            reason = "; ".join(backtest.gate_failures) or "publishable=false"
            _write_attempt_manifest(
                attempt_root,
                authorization=authorization,
                status="failed",
                reason=reason,
            )
            append_attempt_outcome(
                registry_root,
                attempt_id=authorization.attempt_id,
                status="failed",
                authoritative=False,
                reason=reason,
            )
            return FinalTestPipelineResult(
                authorization.attempt_id,
                False,
                attempt_root,
                None,
                backtest.gate_failures,
            )

        test_metrics, comparison = selected.build_metrics(
            authorization,
            signals,
            backtest,
            code_root=code_root,
            data_root=data_root,
        )
        test_metrics.write_parquet(datasets / "final_test_metrics.parquet")
        comparison.write_parquet(datasets / "period_comparison.parquet")
        completed_at = datetime.now(timezone.utc).isoformat()
        upstream = _upstream_identity(
            data_root,
            authorization,
            execution_manifest,
            allow_missing=steps is not None,
        )
        lineage = _lineage(
            authorization,
            upstream=upstream,
            started_at=started_at,
            completed_at=completed_at,
        )
        report = selected.render_report(
            authorization,
            comparison,
            code_root=code_root,
            registry_root=registry_root,
        )
        (artifacts / "report.md").write_text(report, encoding="utf-8")
        _write_json(artifacts / "execution_inputs.json", dict(execution_manifest))
        _write_json(artifacts / "lineage_preview.json", lineage)
        _write_json(
            artifacts / "checkpoint_c.json",
            {
                "status": "required",
                "development_stopped": True,
                "delivery_started": False,
            },
        )
        _write_attempt_manifest(
            attempt_root,
            authorization=authorization,
            status="publishable",
            reason="all frozen final-test publication gates passed",
        )
        release = publish_release(
            final_root,
            run_id=actual_run_id,
            staged_datasets=datasets,
            staged_artifacts=artifacts,
            lineage=lineage,
            manifest_metadata={
                "stage": "final_test",
                "period": [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()],
                "attempt_id": authorization.attempt_id,
                "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
                "publishable": True,
            },
        )
        published_release = release
        append_attempt_outcome(
            registry_root,
            attempt_id=authorization.attempt_id,
            status="succeeded",
            authoritative=True,
            reason="all frozen final-test publication gates passed",
            release_run_id=release.run_id,
            release_manifest_sha256=release.manifest_sha256,
        )
        return FinalTestPipelineResult(
            authorization.attempt_id,
            True,
            attempt_root,
            release,
            (),
        )
    except BaseException as error:
        if attempt_root.exists() and not (attempt_root / "attempt_manifest.json").exists():
            _write_attempt_manifest(
                attempt_root,
                authorization=authorization,
                status="failed",
                reason=f"{type(error).__name__}: {error}",
            )
        outcome = registry_root / f"{authorization.attempt_id}.outcome.json"
        if published_release is None and not outcome.exists():
            append_attempt_outcome(
                registry_root,
                attempt_id=authorization.attempt_id,
                status="failed",
                authoritative=False,
                reason=f"{type(error).__name__}: {error}",
            )
        raise


def _default_steps() -> FinalTestPipelineSteps:
    return FinalTestPipelineSteps(
        authorize=authorize_final_test,
        build_data=build_final_test_daily_panel,
        build_execution_inputs=build_final_execution_inputs,
        build_signals=build_final_test_signals,
        run_backtest=run_final_test_backtest,
        build_metrics=build_final_metrics,
        render_report=build_final_report,
    )


def _write_attempt_core(
    datasets: Path,
    artifacts: Path,
    signals: FinalTestSignals,
    backtest: FinalTestBacktestResult,
) -> None:
    for name, frame in {
        "factor_panel": signals.factor_panel,
        "composite_scores": signals.composite_scores,
        "composite_weights": signals.composite_weights,
        "target_weights": signals.target_weights,
        **backtest.outputs,
    }.items():
        frame.write_parquet(datasets / f"{name}.parquet")
    _write_json(artifacts / "preflight.json", backtest.preflight)
    _write_json(
        artifacts / "runtime_audits.json",
        {key: value for key, value in backtest.audits.items() if not isinstance(value, pl.DataFrame)},
    )
    for name, value in backtest.audits.items():
        if isinstance(value, pl.DataFrame):
            value.write_parquet(artifacts / f"{name}.parquet")


def _write_attempt_manifest(
    root: Path,
    *,
    authorization: FinalTestAuthorization,
    status: str,
    reason: str,
) -> None:
    records = [
        file_record(path, root=root, role=path.parts[-2]).to_dict()
        for path in sorted(item for item in root.rglob("*") if item.is_file())
        if path.name != "attempt_manifest.json"
    ]
    _write_json(
        root / "attempt_manifest.json",
        {
            "attempt_id": authorization.attempt_id,
            "status": status,
            "reason": reason,
            "period": [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()],
            "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
            "git_commit": authorization.git_commit,
            "git_tree": authorization.git_tree,
            "files": records,
        },
    )


def _upstream_identity(
    data_root: Path,
    authorization: FinalTestAuthorization,
    execution_manifest: Mapping[str, object],
    *,
    allow_missing: bool,
) -> dict[str, object]:
    try:
        robustness = resolve_current(data_root / "processed/robustness")
        validation = resolve_current(data_root / "processed/validation_evaluation")
    except FileNotFoundError:
        if not allow_missing:
            raise ValueError("final-test upstream release identity is incomplete") from None
        return {
            "robustness_release": authorization.robustness_release,
            "execution_inputs": dict(execution_manifest),
        }
    if robustness.run_id != authorization.robustness_release:
        raise ValueError("authorized robustness release changed")
    sealed = json.loads(
        (robustness.artifacts / "sealed_test_protocol.json").read_text(encoding="utf-8")
    )
    expected_validation = sealed.get("upstream_validation")
    if not isinstance(expected_validation, dict) or (
        validation.run_id != expected_validation.get("run_id")
        or validation.manifest_sha256 != expected_validation.get("manifest_sha256")
    ):
        raise ValueError("authorized validation release changed")
    return {
        "robustness_release": robustness.run_id,
        "robustness_manifest_sha256": robustness.manifest_sha256,
        "validation_release": validation.run_id,
        "validation_manifest_sha256": validation.manifest_sha256,
        "execution_inputs": dict(execution_manifest),
    }


def _lineage(
    authorization: FinalTestAuthorization,
    *,
    upstream: Mapping[str, object],
    started_at: str,
    completed_at: str,
) -> dict[str, object]:
    return {
        "stage": "final_test",
        "period": [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()],
        "authorization": {
            "attempt_id": authorization.attempt_id,
            "approval_id": authorization.approval_id,
            "registered_at": authorization.registered_at,
            "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
            "git_commit": authorization.git_commit,
            "git_tree": authorization.git_tree,
        },
        "upstream": dict(upstream),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "dependencies": {
                package: version(package)
                for package in (
                    "baostock",
                    "numpy",
                    "polars",
                    "scipy",
                    "PyYAML",
                    "matplotlib",
                )
            },
        },
        "execution": {"started_at": started_at, "completed_at": completed_at},
        "policy": {
            "parameter_search": False,
            "test_tuning": False,
            "checkpoint_c_required": True,
        },
    }


def _assert_authorization(authorization: FinalTestAuthorization) -> None:
    if not isinstance(authorization, FinalTestAuthorization):
        raise TypeError("authorization must be a FinalTestAuthorization")
    if authorization.test_period != (FINAL_TEST_START, FINAL_TEST_END):
        raise ValueError("authorization differs from the exact final-test period")


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
