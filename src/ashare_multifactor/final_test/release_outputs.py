from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import polars as pl

from ashare_multifactor.audit.publication import PublishedRelease
from ashare_multifactor.audit.records import sha256_file, verify_file_record
from ashare_multifactor.final_test.backtest import FinalTestBacktestResult
from ashare_multifactor.final_test.gate import (
    FinalTestAuthorization,
    _verify_sealed_payload,
)
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
    robustness = _resolve_authorized_robustness(data_root, authorization)
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
                for name in ("nav", "orders", "order_events", "trades", "target_diagnostics")
            },
            start=date(2022, 1, 1),
            end=date(2025, 12, 31),
        ),
        metric_manifest=metrics,
        sealed_protocol=sealed,
    )
    stage8_lineage = json.loads(robustness.lineage.read_text(encoding="utf-8"))
    historical = _historical_metrics(data_root, sealed, metrics, stage8_lineage)
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
    robustness = _resolve_authorized_robustness(
        registry_root.parent.parent.parent, authorization
    )
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
            "supported_markets": ",".join(sealed["supported_markets"]),
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
    stage8_lineage: dict[str, object],
) -> tuple[pl.DataFrame, pl.DataFrame]:
    stage_five, validation = _resolve_historical_releases(
        data_root, sealed, stage8_lineage
    )
    sources = (
        (
            "research",
            _bound_release_file(stage_five, "artifacts/composite_ic.parquet"),
            _bound_release_file(stage_five, "artifacts/composite_quantiles.parquet"),
            _historical_backtest_root(validation, period="research"),
        ),
        (
            "validation",
            _bound_release_file(
                validation, "artifacts/research/composite_rank_ic.parquet"
            ),
            _bound_release_file(
                validation, "artifacts/research/composite_quantile_returns.parquet"
            ),
            _historical_backtest_root(validation, period="validation"),
        ),
    )
    result = []
    for period, ic_path, groups_path, backtest_root in sources:
        ic, groups = _metric_inputs(pl.read_parquet(ic_path), pl.read_parquet(groups_path))
        continuous = {
            name: pl.read_parquet(
                _bound_release_file(
                    validation,
                    (backtest_root / f"{name}.parquet").relative_to(validation.root).as_posix(),
                )
            )
            for name in ("nav", "orders", "order_events", "trades", "target_diagnostics")
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


def _resolve_historical_releases(
    data_root: Path,
    sealed: dict[str, object],
    stage8_lineage: dict[str, object],
) -> tuple[PublishedRelease, PublishedRelease]:
    """Resolve immutable Stage-5/7 inputs from the sealed Stage-8 lineage."""
    validation_identity = sealed.get("upstream_validation")
    if not isinstance(validation_identity, dict):
        raise ValueError("sealed protocol lacks upstream validation identity")
    validation = _resolve_bound_release(
        data_root / "processed/validation_evaluation", validation_identity
    )
    stage8_inputs = stage8_lineage.get("inputs")
    validation_input = (
        stage8_inputs.get("validation_manifest")
        if isinstance(stage8_inputs, dict)
        else None
    )
    if (
        not isinstance(validation_input, dict)
        or validation_input.get("sha256") != validation.manifest_sha256
    ):
        raise ValueError("Stage-8 lineage does not bind sealed Stage-7 manifest")
    lineage = json.loads(validation.lineage.read_text(encoding="utf-8"))
    upstream = lineage.get("upstream")
    if not isinstance(upstream, dict):
        raise ValueError("validation lineage lacks frozen Stage-5 inputs")
    manifest_record = upstream.get("stage_five_manifest")
    lineage_record = upstream.get("stage_five_lineage")
    if not isinstance(manifest_record, dict) or not isinstance(lineage_record, dict):
        raise ValueError("validation lineage lacks frozen Stage-5 records")
    manifest_path = verify_file_record(manifest_record, root=data_root)
    lineage_path = verify_file_record(lineage_record, root=data_root)
    stage_five_root = manifest_path.parent
    expected_parent = (data_root / "processed/factor_combination/releases").resolve()
    if (
        stage_five_root.parent.resolve() != expected_parent
        or lineage_path.parent != stage_five_root
        or manifest_path.name != "manifest.json"
        or lineage_path.name != "lineage.json"
    ):
        raise ValueError("validation lineage Stage-5 paths escape release root")
    stage_five = _resolve_bound_release(
        data_root / "processed/factor_combination",
        {
            "run_id": stage_five_root.name,
            "manifest_sha256": manifest_record.get("sha256"),
        },
    )
    return stage_five, validation


def _resolve_authorized_robustness(
    data_root: Path, authorization: FinalTestAuthorization
) -> PublishedRelease:
    """Resolve the authorized Stage-8 run without consulting CURRENT.json."""
    base = data_root / "processed/robustness"
    root = base / "releases" / authorization.robustness_release
    robustness = _resolve_bound_release(
        base,
        {
            "run_id": authorization.robustness_release,
            "manifest_sha256": sha256_file(root / "manifest.json"),
        },
    )
    sealed = json.loads(
        (robustness.artifacts / "sealed_test_protocol.json").read_text(encoding="utf-8")
    )
    if _verify_sealed_payload(sealed) != authorization.sealed_protocol_sha256:
        raise ValueError("authorized Stage-8 sealed protocol changed")
    return robustness


def _resolve_bound_release(
    base: Path, identity: dict[str, object]
) -> PublishedRelease:
    run_id = identity.get("run_id")
    expected_hash = identity.get("manifest_sha256")
    if (
        not isinstance(run_id, str)
        or not run_id
        or Path(run_id).name != run_id
        or not isinstance(expected_hash, str)
        or len(expected_hash) != 64
    ):
        raise ValueError("sealed release identity is invalid")
    root = base / "releases" / run_id
    manifest_path = root / "manifest.json"
    lineage_path = root / "lineage.json"
    if sha256_file(manifest_path) != expected_hash:
        raise ValueError("sealed release manifest hash changed")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("run_id") != run_id or not isinstance(manifest.get("files"), list):
        raise ValueError("sealed release manifest is invalid")
    for record in manifest["files"]:
        verify_file_record(record, root=root)
    return PublishedRelease(
        run_id=run_id,
        root=root,
        datasets=root / "datasets",
        artifacts=root / "artifacts",
        manifest=manifest_path,
        lineage=lineage_path,
        manifest_sha256=expected_hash,
    )


def _bound_release_file(release: PublishedRelease, relative: str) -> Path:
    manifest = json.loads(release.manifest.read_text(encoding="utf-8"))
    records = [
        record
        for record in manifest.get("files", [])
        if isinstance(record, dict) and record.get("path") == relative
    ]
    if len(records) != 1:
        raise ValueError(f"sealed release does not bind required file: {relative}")
    return verify_file_record(records[0], root=release.root)


def _historical_backtest_root(validation: object, *, period: str) -> Path:
    if period not in {"research", "validation"}:
        raise ValueError("unknown historical metric period")
    return Path(getattr(validation, "datasets")) / f"backtests/{MAIN_CANDIDATE}"


def _slice_backtest_period(
    frames: dict[str, pl.DataFrame],
    *,
    start: date,
    end: date,
    include_nav_boundary: bool = True,
) -> dict[str, pl.DataFrame]:
    sliced: dict[str, pl.DataFrame] = {}
    order_names = tuple(name for name in frames if name.endswith("orders"))
    for order_name in order_names:
        events_name = order_name.replace("orders", "order_events")
        orders = frames[order_name]
        events = frames.get(events_name)
        required = {"order_id", "date", "event_seq", "remaining_quantity"}
        if events is None or not required.issubset(events.columns):
            raise ValueError(f"{order_name} require dated {events_name} history")
        keys = ["order_id"]
        if "scenario" in orders.columns and "scenario" in events.columns:
            keys.insert(0, "scenario")
        created = events.group_by(keys).agg(
            pl.col("date").min().alias("first_event_date")
        )
        selected_ids = created.filter(
            pl.col("first_event_date").is_between(start, end)
        )
        terminal = (
            events.filter(pl.col("date") <= end)
            .join(selected_ids, on=keys, how="inner")
            .sort(*keys, "date", "event_seq")
            .group_by(keys, maintain_order=True)
            .tail(1)
            .select(
                *keys,
                pl.col("date").alias("terminal_event_date"),
                "remaining_quantity",
                *(["status"] if "status" in events.columns else []),
            )
        )
        temporal_columns = [
            name
            for name, dtype in orders.schema.items()
            if dtype == pl.Date or isinstance(dtype, pl.Datetime)
        ]
        drop_columns = [
            name
            for name in (*temporal_columns, "remaining_quantity", "status")
            if name in orders.columns
        ]
        sliced[order_name] = (
            orders.drop(drop_columns)
            .join(selected_ids, on=keys, how="inner", validate="1:1")
            .join(terminal, on=keys, how="inner", validate="1:1")
            .sort(*keys)
        )
    for name, frame in frames.items():
        if name in order_names:
            continue
        date_column = "date"
        if date_column not in frame.columns:
            raise ValueError(f"{name} lacks date for strict period slicing")
        within = frame.filter(pl.col(date_column).is_between(start, end)).sort(date_column)
        if name == "nav" and include_nav_boundary:
            boundary = frame.filter(pl.col(date_column) < start).sort(date_column).tail(1)
            if boundary.is_empty():
                if within.is_empty():
                    raise ValueError("portfolio metrics require opening boundary NAV")
            else:
                within = pl.concat((boundary, within), how="vertical_relaxed")
        sliced[name] = within
    return sliced
