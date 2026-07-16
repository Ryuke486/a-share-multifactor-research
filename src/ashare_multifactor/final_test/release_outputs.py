from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import polars as pl

from ashare_multifactor.audit.publication import resolve_current
from ashare_multifactor.final_test.backtest import FinalTestBacktestResult
from ashare_multifactor.final_test.gate import FinalTestAuthorization
from ashare_multifactor.final_test.metrics import (
    build_period_comparison,
    compute_final_test_metrics,
)
from ashare_multifactor.final_test.report import render_final_test_report
from ashare_multifactor.final_test.signals import (
    MAIN_CANDIDATE,
    FinalTestSignals,
)
from ashare_multifactor.research.combination_evaluation import evaluate_combinations


def build_final_metrics(
    authorization: FinalTestAuthorization,
    signals: FinalTestSignals,
    backtest: FinalTestBacktestResult,
    *,
    code_root: Path,
    data_root: Path,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    del code_root
    robustness = resolve_current(data_root / "processed/robustness")
    if robustness.run_id != authorization.robustness_release:
        raise ValueError("robustness release changed before final metrics")
    sealed = json.loads(
        (robustness.artifacts / "sealed_test_protocol.json").read_text(encoding="utf-8")
    )
    evaluation = evaluate_combinations(
        signals.composite_scores,
        signals.factor_panel.select(
            "date", "symbol", "forward_return_5", "forward_return_20", "forward_return_60"
        ).unique(),
    )
    rank_ic, groups = _metric_inputs(evaluation.ic, evaluation.quantiles)
    metrics = tuple(str(value) for value in sealed["metrics"])
    test_metrics = compute_final_test_metrics(
        period="test",
        factor_rank_ic=rank_ic,
        factor_groups=groups,
        backtest=_slice_backtest_period(
            {
                name: backtest.outputs[name]
                for name in ("nav", "orders", "trades", "target_diagnostics")
            },
            start=date(2022, 1, 1),
            end=date(2025, 12, 31),
        ),
        metric_manifest=metrics,
        sealed_protocol=sealed,
    )
    historical = _historical_metrics(data_root, sealed, metrics)
    comparison = build_period_comparison(
        (*historical, test_metrics),
        metric_manifest=metrics,
        sealed_protocol=sealed,
    )
    return test_metrics, comparison


def build_final_report(
    authorization: FinalTestAuthorization,
    comparison: pl.DataFrame,
    *,
    code_root: Path,
    registry_root: Path,
) -> str:
    robustness = resolve_current(registry_root.parent.parent / "robustness")
    sealed = json.loads(
        (robustness.artifacts / "sealed_test_protocol.json").read_text(encoding="utf-8")
    )
    failures = []
    for path in sorted(registry_root.glob("*.outcome.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("status") == "failed":
            failures.append(record)
    return render_final_test_report(
        code_root / "docs/templates/stage9-final-test-report-template.md",
        expected_template_sha256=str(sealed["report_template_sha256"]),
        comparison=comparison,
        identity={
            "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
            "approval_id": authorization.approval_id,
            "attempt_id": authorization.attempt_id,
            "git_commit": authorization.git_commit,
            "robustness_release": authorization.robustness_release,
        },
        failed_runs=failures,
    )


def _metric_inputs(
    rank_ic: pl.DataFrame, quantiles: pl.DataFrame
) -> tuple[pl.DataFrame, pl.DataFrame]:
    method = "rolling_ic_family"
    return (
        rank_ic.filter(pl.col("method") == method).select(
            "date",
            pl.lit("composite").alias("factor_name"),
            pl.lit(method).alias("score_variant"),
            "horizon",
            "rank_ic",
        ),
        quantiles.filter(pl.col("method") == method).select(
            "date",
            pl.lit("composite").alias("factor_name"),
            pl.lit(method).alias("score_variant"),
            "quantile",
            pl.col("mean_return").alias("mean_forward_return_20"),
        ),
    )


def _historical_metrics(
    data_root: Path,
    sealed: dict[str, object],
    metrics: tuple[str, ...],
) -> tuple[pl.DataFrame, pl.DataFrame]:
    stage_five = resolve_current(data_root / "processed/factor_combination")
    validation = resolve_current(data_root / "processed/validation_evaluation")
    sources = (
        (
            "research",
            stage_five.artifacts / "composite_ic.parquet",
            stage_five.artifacts / "composite_quantiles.parquet",
            _historical_backtest_root(validation, period="research"),
        ),
        (
            "validation",
            validation.artifacts / "research/composite_rank_ic.parquet",
            validation.artifacts / "research/composite_quantile_returns.parquet",
            _historical_backtest_root(validation, period="validation"),
        ),
    )
    result = []
    for period, ic_path, groups_path, backtest_root in sources:
        ic, groups = _metric_inputs(pl.read_parquet(ic_path), pl.read_parquet(groups_path))
        continuous = {
            name: pl.read_parquet(backtest_root / f"{name}.parquet")
            for name in ("nav", "orders", "trades", "target_diagnostics")
        }
        bounds = {
            "research": (date(2005, 1, 1), date(2016, 12, 31)),
            "validation": (date(2017, 1, 1), date(2021, 12, 31)),
        }
        backtest = _slice_backtest_period(
            continuous, start=bounds[period][0], end=bounds[period][1]
        )
        result.append(
            compute_final_test_metrics(
                period=period,
                factor_rank_ic=ic,
                factor_groups=groups,
                backtest=backtest,
                metric_manifest=metrics,
                sealed_protocol=sealed,
            )
        )
    return result[0], result[1]


def _historical_backtest_root(validation: object, *, period: str) -> Path:
    if period not in {"research", "validation"}:
        raise ValueError("unknown historical metric period")
    return Path(getattr(validation, "datasets")) / f"backtests/{MAIN_CANDIDATE}"


def _slice_backtest_period(
    frames: dict[str, pl.DataFrame],
    *,
    start: date,
    end: date,
) -> dict[str, pl.DataFrame]:
    sliced: dict[str, pl.DataFrame] = {}
    for name, frame in frames.items():
        date_column = "signal_date" if name == "orders" and "signal_date" in frame.columns else "date"
        if date_column not in frame.columns:
            raise ValueError(f"{name} lacks date for strict period slicing")
        within = frame.filter(pl.col(date_column).is_between(start, end)).sort(date_column)
        if name == "nav":
            boundary = frame.filter(pl.col(date_column) < start).sort(date_column).tail(1)
            if boundary.is_empty():
                if within.is_empty():
                    raise ValueError("portfolio metrics require opening boundary NAV")
            else:
                within = pl.concat((boundary, within), how="vertical_relaxed")
        sliced[name] = within
    return sliced
