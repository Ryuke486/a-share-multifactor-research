from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import shutil
from typing import Any
import uuid

import polars as pl

from ashare_multifactor.audit.identity import code_identity
from ashare_multifactor.audit.publication import publish_release, resolve_current
from ashare_multifactor.audit.records import sha256_file
from ashare_multifactor.combination.definitions import CANDIDATE_FACTORS
from ashare_multifactor.combination.panel import build_rolling_composite_scores
from ashare_multifactor.combination.rolling_ic import rolling_ic_weights
from ashare_multifactor.config import load_config
from ashare_multifactor.execution.broker import BacktestSettings
from ashare_multifactor.execution.fee_protocol import validate_fee_protocol
from ashare_multifactor.execution.fees import load_market_rules
from ashare_multifactor.robustness.execution_sensitivity import (
    build_execution_scenarios,
    select_backtest_scenario,
    summarize_execution_result,
)
from ashare_multifactor.robustness.factor_ablation import build_ablation_scores
from ashare_multifactor.robustness.portfolio_sensitivity import (
    ExpectedExperimentUnavailability,
    build_portfolio_variants,
    build_variant_targets,
)
from ashare_multifactor.robustness.protocol import (
    RobustnessProtocol,
    assert_authoritative_period_contracts,
    assert_robustness_read_allowed,
    load_robustness_protocol,
    read_bounded_parquet,
)
from ashare_multifactor.robustness.regime_analysis import (
    classify_market_regimes,
    industry_limitation,
    size_segments,
    summarize_segment_returns,
    time_segments,
)
from ashare_multifactor.robustness.report import render_robustness_report
from ashare_multifactor.robustness.summary import (
    assess_test_protocol_gate,
    build_robustness_long_table,
)
from ashare_multifactor.robustness.successor_seal import (
    Stage8Supersession,
    build_stage8_successor_lineage,
    build_stage8_supersession,
    verify_action_coverage_audit,
)
from ashare_multifactor.robustness.test_protocol import seal_test_protocol
from ashare_multifactor.validation.backtest_extension import run_validation_backtest
from ashare_multifactor.validation.backtest_inputs import build_continuous_targets


SEALED_TEST_START = date(2022, 1, 1)
CORE_ROBUSTNESS_FILES = (
    "robustness_results.parquet",
    "report.md",
    "protocol_gate.json",
)


def assert_robustness_outputs_sealed(root: Path) -> None:
    for path in sorted(root.rglob("*.parquet")):
        schema = pl.scan_parquet(path).collect_schema()
        columns = [
            name
            for name, dtype in schema.items()
            if dtype == pl.Date and (name == "date" or name.endswith("_date"))
        ]
        if not columns:
            continue
        maximums = (
            pl.scan_parquet(path)
            .select(*(pl.col(name).max().alias(name) for name in columns))
            .collect()
            .row(0, named=True)
        )
        leaks = {
            name: value
            for name, value in maximums.items()
            if value is not None and value >= SEALED_TEST_START
        }
        if leaks:
            raise ValueError(f"sealed final test date found in {path}: {leaks}")


def finalize_robustness_run(
    run_root: Path,
    *,
    run_id: str,
    identity: dict[str, object],
    inputs: dict[str, object] | None = None,
) -> dict[str, Any]:
    missing = [name for name in CORE_ROBUSTNESS_FILES if not (run_root / name).is_file()]
    if missing:
        raise FileNotFoundError("robustness run is incomplete: " + ", ".join(missing))
    assert_robustness_outputs_sealed(run_root)
    files = [
        {
            "path": name,
            "sha256": sha256_file(run_root / name),
            "size": (run_root / name).stat().st_size,
        }
        for name in CORE_ROBUSTNESS_FILES
    ]
    manifest = {
        "run_id": run_id,
        "code": identity,
        "inputs": inputs or {},
        "core_file_count": len(files),
        "files": files,
    }
    (run_root / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def compare_robustness_runs(first: Path, second: Path) -> dict[str, Any]:
    manifests = [
        json.loads((root / "run_manifest.json").read_text(encoding="utf-8"))
        for root in (first, second)
    ]
    if manifests[0]["code"] != manifests[1]["code"]:
        raise ValueError("robustness runs used different code identities")
    if manifests[0]["inputs"] != manifests[1]["inputs"]:
        raise ValueError("robustness runs used different inputs")
    hashes: dict[str, str] = {}
    for name in CORE_ROBUSTNESS_FILES:
        left = sha256_file(first / name)
        right = sha256_file(second / name)
        if left != right:
            raise ValueError(f"robustness runs differ: {name}")
        hashes[name] = left
    return {
        "outputs_identical": True,
        "core_file_count": len(CORE_ROBUSTNESS_FILES),
        "core_hashes": hashes,
        "run_ids": [manifests[0]["run_id"], manifests[1]["run_id"]],
        "code": manifests[0]["code"],
        "inputs": manifests[0]["inputs"],
    }


def resolve_robustness_data_root(code_root: Path) -> Path:
    root = code_root.resolve()
    marker = Path("processed/validation_evaluation/CURRENT.json")
    if (root / marker).is_file():
        return root
    if root.parent.name == ".worktrees" and (root.parent.parent / marker).is_file():
        return root.parent.parent
    raise FileNotFoundError("cannot locate authoritative validation release")


def execute_robustness_run(
    code_root: Path,
    *,
    run_id: str | None = None,
) -> Path:
    """Execute every pre-registered Stage-8 diagnostic without reading test data."""
    code_root = code_root.resolve()
    data_root = resolve_robustness_data_root(code_root)
    protocol = load_robustness_protocol(code_root / "configs/robustness_protocol.yaml")
    validation = resolve_current(data_root / "processed/validation_evaluation")
    if validation.run_id != protocol.validation_release:
        raise ValueError("authoritative validation release changed")
    decision = json.loads(
        (validation.artifacts / "research/selection_decision.json").read_text(
            encoding="utf-8"
        )
    )
    if decision.get("selected_candidate") != protocol.main_candidate:
        raise ValueError("validation main candidate changed")
    _assert_input_period_contracts(data_root, validation)
    assert_robustness_read_allowed(protocol.analysis_start, protocol.analysis_end)
    actual_run_id = run_id or uuid.uuid4().hex
    run_root = data_root / "artifacts/robustness/full_runs" / actual_run_id
    if run_root.exists():
        raise FileExistsError(run_root)
    run_root.mkdir(parents=True)
    identity = code_identity(code_root)
    inputs = _robustness_input_identity(code_root, data_root, validation)
    try:
        results, fatal_events = _execute_experiments(
            code_root, data_root, validation, protocol
        )
        table = build_robustness_long_table(results, protocol)
        gate = assess_test_protocol_gate(
            table,
            fatal_events=fatal_events,
            protocol=protocol,
        )
        table.write_parquet(run_root / "robustness_results.parquet")
        (run_root / "protocol_gate.json").write_text(
            json.dumps(gate, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (run_root / "report.md").write_text(
            render_robustness_report(table, gate, protocol), encoding="utf-8"
        )
        if code_identity(code_root) != identity:
            raise ValueError("code identity changed during robustness run")
        if _robustness_input_identity(code_root, data_root, validation) != inputs:
            raise ValueError("robustness inputs changed during run")
        finalize_robustness_run(
            run_root,
            run_id=actual_run_id,
            identity=identity,
            inputs=inputs,
        )
    except BaseException:
        shutil.rmtree(run_root, ignore_errors=True)
        raise
    return run_root


def execute_robustness_reproducibility(
    code_root: Path,
    *,
    run_id_prefix: str | None = None,
) -> dict[str, Any]:
    prefix = run_id_prefix or uuid.uuid4().hex
    first = execute_robustness_run(code_root, run_id=f"{prefix}_1")
    second = execute_robustness_run(code_root, run_id=f"{prefix}_2")
    result = compare_robustness_runs(first, second)
    result["release_eligible"] = result["code"].get("dirty") is False
    result["reason"] = None if result["release_eligible"] else "git_identity_is_dirty"
    data_root = resolve_robustness_data_root(code_root)
    destination = data_root / "processed/robustness/reproducibility.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def publish_robustness_release(
    code_root: Path,
    *,
    run_id: str,
    successor_audit_root: Path | None = None,
) -> object:
    """Publish only two-run-identical output from one clean Git identity."""
    code_root = code_root.resolve()
    data_root = resolve_robustness_data_root(code_root)
    successor_contract = (
        _successor_release_contract(code_root, data_root, successor_audit_root)
        if successor_audit_root is not None
        else None
    )
    identity = code_identity(code_root)
    if identity.get("dirty") is not False:
        raise ValueError("robustness release requires a clean Git identity")
    reproducibility_path = data_root / "processed/robustness/reproducibility.json"
    reproducibility = json.loads(reproducibility_path.read_text(encoding="utf-8"))
    if (
        reproducibility.get("outputs_identical") is not True
        or reproducibility.get("release_eligible") is not True
        or reproducibility.get("code") != identity
    ):
        raise ValueError("robustness reproducibility gate is incomplete")
    source = (
        data_root
        / "artifacts/robustness/full_runs"
        / reproducibility["run_ids"][1]
    )
    verify_reproducible_source(source, reproducibility)
    gate = json.loads((source / "protocol_gate.json").read_text(encoding="utf-8"))
    if gate.get("sealed_test_protocol_allowed") is not True:
        raise ValueError("robustness gate does not allow publication")
    protocol = load_robustness_protocol(code_root / "configs/robustness_protocol.yaml")
    research_config = load_config(code_root / "configs/research_protocol.yaml")
    validation_pointer = json.loads(
        (data_root / "processed/validation_evaluation/CURRENT.json").read_text(
            encoding="utf-8"
        )
    )
    staged = data_root / "artifacts/robustness/release_staging" / run_id
    if staged.exists():
        raise FileExistsError(staged)
    datasets = staged / "datasets"
    artifacts = staged / "artifacts"
    datasets.mkdir(parents=True)
    artifacts.mkdir()
    shutil.copy2(source / "robustness_results.parquet", datasets)
    shutil.copy2(source / "report.md", artifacts)
    shutil.copy2(source / "protocol_gate.json", artifacts)
    if successor_contract is not None:
        shutil.copytree(successor_audit_root, artifacts / "action_coverage_audit")
    sealed = seal_test_protocol(
        artifacts / "sealed_test_protocol.json",
        protocol=protocol,
        code_identity=identity,
        validation_pointer=validation_pointer,
        market_rules_sha256=sha256_file(code_root / "configs/market_rules.yaml"),
        report_template_sha256=sha256_file(
            code_root / "docs/templates/stage9-final-test-report-template.md"
        ),
        opening_ledger_root=(
            data_root / "processed/robustness/final_test_opening_ledger"
        ),
        gate=gate,
        supported_markets=research_config.supported_markets,
        action_source_contract_sha256=(
            str(successor_contract["action_source_contract_sha256"])
            if successor_contract is not None
            else None
        ),
        action_coverage_audit_sha256=(
            str(successor_contract["action_coverage_audit_sha256"])
            if successor_contract is not None
            else None
        ),
        predecessor=(
            successor_contract["predecessor"]
            if successor_contract is not None
            else None
        ),
    )
    lineage = {
        "stage": "robustness",
        "period": ["2005-01-01", "2021-12-31"],
        "sealed_test_start": "2022-01-01",
        "main_candidate": protocol.main_candidate,
        "code": identity,
        "inputs": reproducibility["inputs"],
        "reproducibility": reproducibility,
        "sealed_protocol_sha256": sealed["sealed_protocol_sha256"],
        "supported_markets": list(research_config.supported_markets),
    }
    if successor_contract is not None:
        lineage = build_stage8_successor_lineage(
            lineage,
            supersession=Stage8Supersession(**successor_contract["predecessor"]),
            action_source_contract_sha256=str(
                successor_contract["action_source_contract_sha256"]
            ),
            action_coverage_audit_sha256=str(
                successor_contract["action_coverage_audit_sha256"]
            ),
        )
        lineage["execution_protocol"]["supported_markets"] = list(
            research_config.supported_markets
        )
    return publish_release(
        data_root / "processed/robustness",
        run_id=run_id,
        staged_datasets=datasets,
        staged_artifacts=artifacts,
        lineage=lineage,
        manifest_metadata={"stage": "robustness"},
    )


def _successor_release_contract(
    code_root: Path,
    data_root: Path,
    action_audit_root: Path,
) -> dict[str, object]:
    predecessor = resolve_current(data_root / "processed/robustness")
    audit_sha256 = verify_action_coverage_audit(action_audit_root)
    market_rules_path = code_root / "configs/market_rules.yaml"
    source_contract_path = code_root / "configs/final_execution_sources.yaml"
    validate_fee_protocol(
        load_market_rules(market_rules_path),
        date(2022, 1, 1),
        date(2025, 12, 31),
    )
    supersession = build_stage8_supersession(
        predecessor_pointer={
            "run_id": predecessor.run_id,
            "manifest_sha256": predecessor.manifest_sha256,
        },
        predecessor_manifest=predecessor.manifest,
        reason="final execution fee and corporate-action coverage incomplete",
    )
    return {
        "predecessor": supersession.to_dict(),
        "market_rules_sha256": sha256_file(market_rules_path),
        "action_source_contract_sha256": sha256_file(source_contract_path),
        "action_coverage_audit_sha256": audit_sha256,
    }


def verify_reproducible_source(
    source: Path, reproducibility: dict[str, Any]
) -> None:
    """Revalidate the selected run instead of trusting a mutable summary file."""
    manifest = json.loads((source / "run_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("run_id") != source.name:
        raise ValueError("reproducible source run identity changed")
    if manifest.get("code") != reproducibility.get("code"):
        raise ValueError("reproducible source code identity changed")
    if manifest.get("inputs") != reproducibility.get("inputs"):
        raise ValueError("reproducible source input identity changed")
    manifest_hashes = {
        item["path"]: item["sha256"] for item in manifest.get("files", [])
    }
    expected = reproducibility.get("core_hashes", {})
    for name in CORE_ROBUSTNESS_FILES:
        actual = sha256_file(source / name)
        if actual != expected.get(name) or actual != manifest_hashes.get(name):
            raise ValueError(f"reproducible source hash changed: {name}")


def _execute_experiments(
    code_root: Path,
    data_root: Path,
    validation: object,
    protocol: RobustnessProtocol,
) -> tuple[list[dict[str, Any]], list[dict[str, object]]]:
    config = load_config(code_root / "configs/research_protocol.yaml")
    formal = config.formal_backtest
    combination = config.factor_combination
    portfolio = config.portfolio_construction
    validation_settings = config.validation_evaluation
    if any(item is None for item in (formal, combination, portfolio, validation_settings)):
        raise ValueError("robustness requires frozen Stage-5 through Stage-7 settings")
    inputs = validation.datasets / "inputs"
    execution = read_bounded_parquet(inputs / "execution_panel.parquet")
    actions = read_bounded_parquet(
        inputs / "corporate_actions.parquet", date_column="effective_date"
    )
    events = read_bounded_parquet(
        inputs / "security_events.parquet", date_column="effective_date"
    )
    targets = read_bounded_parquet(
        inputs / f"continuous_targets_{protocol.main_candidate}.parquet"
    )
    baseline_result = _load_published_backtest(validation, protocol.main_candidate)
    fees = load_market_rules(code_root / "configs/market_rules.yaml")
    settings = BacktestSettings(
        initial_cash=formal.initial_cash,
        maximum_participation=formal.maximum_participation,
        fixed_slippage_bps=formal.fixed_slippage_bps,
        reference_impact_bps=formal.reference_impact_bps,
        reference_participation=formal.reference_participation,
        maximum_impact_bps=formal.maximum_impact_bps,
        buy_lot_size=formal.buy_lot_size,
        analysis_end=protocol.analysis_end,
    )
    results: list[dict[str, Any]] = []
    fatal: list[dict[str, object]] = []
    for scenario in build_execution_scenarios(protocol, settings, fees):
        if scenario.settings == settings and scenario.fees == fees:
            selected = select_backtest_scenario(
                baseline_result, scenario.result_scenario
            )
        else:
            rerun = run_validation_backtest(
                execution, targets, actions, scenario.fees, scenario.settings, events
            )
            selected = select_backtest_scenario(rerun, scenario.result_scenario)
        results.append(_wide_result(scenario.experiment_id, selected))

    scores = read_bounded_parquet(inputs / "composite_scores.parquet")
    features = read_bounded_parquet(inputs / "factor_features.parquet")
    log_size = features.filter(pl.col("factor_name") == "log_market_cap").select(
        "date", "symbol", pl.col("raw_value").alias("log_market_cap")
    )
    liquidity = features.filter(pl.col("factor_name") == "amihud_20").select(
        "date", "symbol", (-pl.col("raw_value")).alias("liquidity")
    )
    stage_five = resolve_current(data_root / "processed/factor_combination")
    research_targets = read_bounded_parquet(
        stage_five.datasets / "target_weights.parquet", end=date(2016, 12, 31)
    )
    previous_targets = research_targets.filter(
        (pl.col("portfolio_name") == "size_stratified_buffered")
        & (pl.col("date") == pl.col("date").max())
    )
    portfolio_variants = build_portfolio_variants(protocol)
    for variant in portfolio_variants:
        if variant.experiment_id == "portfolio_baseline":
            results.append(_wide_result(variant.experiment_id, baseline_result))
            continue
        try:
            built = build_variant_targets(
                scores,
                log_size,
                liquidity,
                previous_targets,
                variant,
                size_groups=portfolio.size_groups,
            )
            continuous = _continuous_variant(research_targets, built.targets, protocol)
            rerun = run_validation_backtest(
                execution, continuous, actions, fees, settings, events
            )
            results.append(_wide_result(variant.experiment_id, rerun))
        except ExpectedExperimentUnavailability as exc:
            results.append(_failed_result(variant.experiment_id, str(exc)))

    panel = read_bounded_parquet(inputs / "factor_panel.parquet")
    weights = read_bounded_parquet(inputs / "composite_weights.parquet")
    for experiment in protocol.experiments:
        if experiment.category != "factor_ablation":
            continue
        ablated = build_ablation_scores(
            panel,
            weights,
            removed_family=str(experiment.parameters["removed_family"]),
        )
        baseline_variant = next(
            item for item in portfolio_variants if item.experiment_id == "portfolio_baseline"
        )
        built = build_variant_targets(
            ablated,
            log_size,
            liquidity,
            previous_targets,
            baseline_variant,
            size_groups=portfolio.size_groups,
        )
        continuous = _continuous_variant(research_targets, built.targets, protocol)
        rerun = run_validation_backtest(
            execution, continuous, actions, fees, settings, events
        )
        results.append(_wide_result(experiment.experiment_id, rerun))

    # Static classification metadata has no observations or date column.
    classifications = pl.read_parquet(
        data_root / "processed/factor_research/factor_classifications.parquet"
    )
    historical_ic = read_bounded_parquet(
        data_root / "processed/factor_research/rank_ic.parquet",
        end=date(2016, 12, 31),
    )
    validation_ic = read_bounded_parquet(
        validation.artifacts / "research/factor_rank_ic.parquet"
    )
    historical_returns = read_bounded_parquet(
        data_root / "processed/factor_research/forward_returns.parquet",
        end=date(2016, 12, 31),
    )
    validation_returns = read_bounded_parquet(inputs / "forward_returns.parquet")
    realization_dates = pl.concat(
        (historical_returns, validation_returns), how="vertical_relaxed"
    ).group_by("date").agg(
        (
            pl.col("date").first()
            + pl.duration(days=pl.col("forward_calendar_days_20").max())
        ).alias("realization_date")
    )
    rank_ic = pl.concat((historical_ic, validation_ic), how="diagonal_relaxed")
    weight_dates = panel.get_column("date").unique().sort().to_list()
    baseline_variant = next(
        item for item in portfolio_variants if item.experiment_id == "portfolio_baseline"
    )
    for experiment in protocol.experiments:
        if experiment.category != "rolling_window":
            continue
        rolling = rolling_ic_weights(
            rank_ic,
            weight_dates=weight_dates,
            factors=CANDIDATE_FACTORS,
            window_months=int(experiment.parameters["window_months"]),
            minimum_months=int(experiment.parameters["minimum_months"]),
            shrinkage=combination.ic_shrinkage,
            realization_dates=realization_dates,
        )
        window_scores = build_rolling_composite_scores(
            panel,
            classifications,
            rolling,
            minimum_families=combination.minimum_families,
        )
        built = build_variant_targets(
            window_scores,
            log_size,
            liquidity,
            previous_targets,
            baseline_variant,
            size_groups=portfolio.size_groups,
        )
        continuous = _continuous_variant(research_targets, built.targets, protocol)
        rerun = run_validation_backtest(
            execution, continuous, actions, fees, settings, events
        )
        results.append(_wide_result(experiment.experiment_id, rerun))

    results.extend(_regime_results(baseline_result, execution, panel))
    return results, fatal


def _load_published_backtest(validation: object, candidate: str) -> dict[str, pl.DataFrame]:
    root = validation.datasets / "backtests" / candidate
    return {
        name: read_bounded_parquet(
            root / f"{name}.parquet",
            date_column=(
                "signal_date" if name in {"orders", "scenario_orders"} else "date"
            ),
        )
        for name in (
            "nav",
            "orders",
            "trades",
            "target_diagnostics",
            "scenario_nav",
            "scenario_orders",
            "scenario_trades",
            "scenario_target_diagnostics",
        )
    }


def _wide_result(experiment_id: str, result: dict[str, pl.DataFrame]) -> dict[str, Any]:
    row = summarize_execution_result(experiment_id, result)
    metric_names = (
        "annual_return",
        "annual_volatility",
        "maximum_drawdown",
        "turnover",
        "cost_erosion",
        "unfilled_rate",
        "target_deviation",
        "total_cost_rate",
    )
    return {
        "experiment_id": experiment_id,
        "status": "ready",
        "sample": "2005-2021",
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "observations": row["observations"],
        "metrics": {name: row[name] for name in metric_names},
    }


def _failed_result(experiment_id: str, reason: str) -> dict[str, Any]:
    return {
        "experiment_id": experiment_id,
        "status": "unavailable",
        "sample": "2005-2021",
        "observations": 0,
        "reason": reason,
        "metrics": {},
    }


def _continuous_variant(
    research_targets: pl.DataFrame,
    validation_targets: pl.DataFrame,
    protocol: RobustnessProtocol,
) -> pl.DataFrame:
    labeled = validation_targets.with_columns(
        pl.lit(protocol.main_candidate).alias("candidate")
    )
    return build_continuous_targets(
        research_targets,
        labeled,
        candidate=protocol.main_candidate,
    )


def _regime_results(
    baseline_result: dict[str, pl.DataFrame],
    execution: pl.DataFrame,
    panel: pl.DataFrame,
) -> list[dict[str, Any]]:
    nav = baseline_result["nav"].select("date", "return")
    market = (
        execution.with_columns(
            (pl.col("close_adj") / pl.col("prev_close_adj") - 1.0).alias(
                "market_return"
            )
        )
        .filter(pl.col("market_return").is_finite())
        .group_by("date")
        .agg(pl.col("market_return").mean())
        .sort("date")
    )
    regimes = classify_market_regimes(market)
    segmented_frames = [time_segments(nav)]
    joined = nav.join(regimes, on="date", how="inner", validate="1:1")
    for column in ("market_trend", "volatility_regime"):
        segmented_frames.append(
            joined.select(
                "date",
                "return",
                pl.lit(column).alias("segment_type"),
                pl.col(column).alias("segment"),
            ).filter(pl.col("segment") != "insufficient_history")
        )
    segmented_frames.append(size_segments(panel))
    summary = summarize_segment_returns(
        pl.concat(segmented_frames),
        annualization_by_segment_type={"market_size": 12},
    )
    results = []
    for row in summary.iter_rows(named=True):
        results.append(
            {
                "experiment_id": "regime_pre_registered",
                "status": "ready",
                "sample": f"{row['segment_type']}:{row['segment']}",
                "start_date": row["start_date"],
                "end_date": row["end_date"],
                "observations": row["observations"],
                "metrics": {
                    "annual_return": row["annualized_return"],
                    "annual_volatility": row["annualized_volatility"],
                    "standard_error": row["standard_error"],
                    "ci95_low": row["ci95_low"],
                    "ci95_high": row["ci95_high"],
                },
            }
        )
    limitation = industry_limitation()
    results.append(
        {
            "experiment_id": "regime_pre_registered",
            "status": limitation["status"],
            "sample": "industry",
            "observations": 0,
            "reason": limitation["reason"],
            "metrics": {},
        }
    )
    return results


def _robustness_input_identity(
    code_root: Path, data_root: Path, validation: object
) -> dict[str, object]:
    stage_five = resolve_current(data_root / "processed/factor_combination")
    factor_root = data_root / "processed/factor_research"
    paths = {
        "robustness_protocol": code_root / "configs/robustness_protocol.yaml",
        "research_protocol": code_root / "configs/research_protocol.yaml",
        "market_rules": code_root / "configs/market_rules.yaml",
        "stage9_report_template": (
            code_root / "docs/templates/stage9-final-test-report-template.md"
        ),
        "validation_manifest": validation.manifest,
        "validation_lineage": validation.lineage,
        "stage5_manifest": stage_five.manifest,
        "stage5_lineage": stage_five.lineage,
        "stage5_target_weights": stage_five.datasets / "target_weights.parquet",
        "factor_research_lineage": factor_root / "lineage.json",
        "factor_research_report_manifest": factor_root / "report_manifest.json",
        "factor_classifications": factor_root / "factor_classifications.parquet",
        "historical_rank_ic": factor_root / "rank_ic.parquet",
        "historical_forward_returns": factor_root / "forward_returns.parquet",
    }
    return {
        name: {"sha256": sha256_file(path), "size": path.stat().st_size}
        for name, path in paths.items()
    }


def _assert_input_period_contracts(data_root: Path, validation: object) -> None:
    stage_five = resolve_current(data_root / "processed/factor_combination")
    validation_lineage = json.loads(validation.lineage.read_text(encoding="utf-8"))
    stage5_lineage = json.loads(stage_five.lineage.read_text(encoding="utf-8"))
    factor_lineage = json.loads(
        (data_root / "processed/factor_research/lineage.json").read_text(
            encoding="utf-8"
        )
    )
    assert_authoritative_period_contracts(
        validation_lineage, stage5_lineage, factor_lineage
    )
