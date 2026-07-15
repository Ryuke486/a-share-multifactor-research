from __future__ import annotations

from datetime import date
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import resource
import shutil
import time
import uuid

import polars as pl

from ashare_multifactor.audit.identity import code_identity
from ashare_multifactor.audit.publication import (
    PublishedRelease,
    publish_release,
    resolve_current,
)
from ashare_multifactor.audit.records import file_record, sha256_file
from ashare_multifactor.config import Period, load_config
from ashare_multifactor.data.manifest import validate_panel_source
from ashare_multifactor.research.combination_inputs import validate_stage_five_inputs
from ashare_multifactor.research.factor_lineage import validate_file_records
from ashare_multifactor.validation.protocol import assert_stage_six_release_allowed


SEALED_TEST_START = date(2022, 1, 1)
DATE_COLUMNS = {
    "date",
    "ex_date",
    "effective_date",
    "signal_date",
    "history_start",
    "history_end",
    "realization_date",
}
CORE_BACKTEST_FILES = (
    "orders.parquet",
    "order_events.parquet",
    "trades.parquet",
    "cash_ledger.parquet",
    "receivable_ledger.parquet",
    "positions.parquet",
    "nav.parquet",
    "reconciliation.parquet",
    "scenario_orders.parquet",
    "scenario_order_events.parquet",
    "scenario_trades.parquet",
    "scenario_cash_ledger.parquet",
    "scenario_receivable_ledger.parquet",
    "scenario_positions.parquet",
    "scenario_nav.parquet",
    "scenario_reconciliation.parquet",
    "scenario_target_diagnostics.parquet",
    "target_diagnostics.parquet",
)
FROZEN_CANDIDATES = (
    "family_equal_size_stratified_buffered",
    "rolling_ic_family_size_stratified_buffered",
    "family_equal_top100_equal",
)
CORE_DATASET_FILES = (
    "datasets/factor_features.parquet",
    "datasets/forward_returns.parquet",
    "datasets/factor_panel.parquet",
    "datasets/composite_scores.parquet",
    "datasets/composite_weights.parquet",
    "datasets/target_weights.parquet",
    "datasets/execution_panel.parquet",
    "datasets/corporate_actions.parquet",
    "datasets/security_events.parquet",
    *(
        f"datasets/target_weights_{candidate}.parquet"
        for candidate in FROZEN_CANDIDATES
    ),
    *(
        f"datasets/continuous_targets_{candidate}.parquet"
        for candidate in FROZEN_CANDIDATES
    ),
)
CORE_RESEARCH_FILES = (
    "artifacts/factor_audit.parquet",
    "artifacts/factor_rank_ic.parquet",
    "artifacts/factor_quantile_returns.parquet",
    "artifacts/factor_summary.parquet",
    "artifacts/factor_turnover.parquet",
    "artifacts/combination_audit.parquet",
    "artifacts/composite_rank_ic.parquet",
    "artifacts/composite_quantile_returns.parquet",
    "artifacts/composite_subperiods.parquet",
    "artifacts/composite_summary.parquet",
    "artifacts/target_diagnostics.parquet",
    *(
        f"artifacts/target_diagnostics_{candidate}.parquet"
        for candidate in FROZEN_CANDIDATES
    ),
    "artifacts/validation_metrics.parquet",
    "artifacts/selection_decision.json",
    "artifacts/report.md",
)
CORE_VALIDATION_FILES = (
    *CORE_DATASET_FILES,
    *CORE_RESEARCH_FILES,
    *(
        f"backtests/{candidate}/datasets/{name}"
        for candidate in FROZEN_CANDIDATES
        for name in CORE_BACKTEST_FILES
    ),
    *(
        f"backtests/{candidate}/artifacts/{name}"
        for candidate in FROZEN_CANDIDATES
        for name in (
            "shadow_nav_audit.json",
            "shadow_nav_daily.parquet",
            "stale_audit.json",
            "stale_intervals.parquet",
        )
    ),
)


def resolve_validation_data_root(root: Path) -> Path:
    root = root.resolve()
    marker = Path("processed/validation_evaluation/daily_panel/manifest.json")
    if (root / marker).is_file():
        return root
    if root.parent.name == ".worktrees":
        candidate = root.parent.parent
        if (candidate / marker).is_file():
            return candidate
    raise FileNotFoundError("cannot locate validation daily-panel data")


def assert_validation_outputs_sealed(root: Path) -> None:
    """Inspect actual Parquet date columns before any validation publication."""
    for path in sorted(root.rglob("*.parquet")):
        schema = pl.scan_parquet(path).collect_schema()
        columns = [name for name in DATE_COLUMNS if name in schema]
        if not columns:
            continue
        maximums = pl.scan_parquet(path).select(
            *(pl.col(column).max().alias(column) for column in columns)
        ).collect().row(0, named=True)
        leaks = {
            column: value
            for column, value in maximums.items()
            if value is not None and value >= SEALED_TEST_START
        }
        if leaks:
            raise ValueError(f"sealed final test date found in {path}: {leaks}")


def compare_validation_outputs(
    first: Path,
    second: Path,
    relative_paths: tuple[str, ...],
) -> None:
    for relative in relative_paths:
        left = first / relative
        right = second / relative
        if not left.is_file() or not right.is_file():
            raise FileNotFoundError(relative)
        if sha256_file(left) != sha256_file(right):
            raise ValueError(f"validation runs differ: {relative}")


def finalize_validation_run(
    run_root: Path,
    *,
    run_id: str,
    identity: dict[str, object],
    inputs: dict[str, object],
    elapsed_seconds: float,
    maximum_rss_bytes: int,
) -> dict[str, object]:
    """Validate and hash one independently generated full-pipeline run."""
    actual = _assert_validation_run_tree(run_root, require_manifest=False)
    missing = [relative for relative in CORE_VALIDATION_FILES if relative not in actual]
    if missing:
        raise FileNotFoundError("validation run is incomplete: " + ", ".join(missing))
    assert_validation_outputs_sealed(run_root)
    records = [
        {
            "path": relative,
            "sha256": sha256_file(run_root / relative),
            "size": (run_root / relative).stat().st_size,
        }
        for relative in CORE_VALIDATION_FILES
    ]
    manifest: dict[str, object] = {
        "run_id": run_id,
        "code": identity,
        "inputs": inputs,
        "core_file_count": len(records),
        "files": records,
        "resource_usage": {
            "elapsed_seconds": elapsed_seconds,
            "maximum_rss_bytes": maximum_rss_bytes,
        },
    }
    (run_root / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def certify_full_validation_runs(
    data_root: Path,
    first_run_id: str,
    second_run_id: str,
) -> dict[str, object]:
    """Recheck two complete runs and record whether the clean-release gate passes."""
    if first_run_id == second_run_id:
        raise ValueError("full validation reproducibility requires distinct runs")
    runs = data_root / "artifacts/validation_evaluation/full_runs"
    roots = (runs / first_run_id, runs / second_run_id)
    manifests = [
        json.loads((root / "run_manifest.json").read_text(encoding="utf-8"))
        for root in roots
    ]
    for root, manifest in zip(roots, manifests, strict=True):
        _assert_validation_run_tree(root, require_manifest=True)
        if manifest.get("core_file_count") != len(CORE_VALIDATION_FILES):
            raise ValueError(f"validation run manifest is incomplete: {root.name}")
        recorded = {item["path"]: item["sha256"] for item in manifest["files"]}
        for relative in CORE_VALIDATION_FILES:
            path = root / relative
            if not path.is_file() or recorded.get(relative) != sha256_file(path):
                raise ValueError(f"validation run manifest hash mismatch: {root.name}/{relative}")
    if manifests[0]["inputs"] != manifests[1]["inputs"]:
        raise ValueError("validation runs used different frozen inputs")
    if manifests[0]["code"] != manifests[1]["code"]:
        raise ValueError("validation runs used different code identities")
    compare_validation_outputs(roots[0], roots[1], CORE_VALIDATION_FILES)
    identity = manifests[0]["code"]
    clean = identity.get("dirty") is False
    result = {
        "scope": "full_pipeline",
        "run_ids": [first_run_id, second_run_id],
        "core_file_count": len(CORE_VALIDATION_FILES),
        "outputs_identical": True,
        "full_pipeline_reproducible": clean,
        "release_eligible": clean,
        "reason": None if clean else "git_identity_is_dirty",
        "code": identity,
        "inputs": manifests[0]["inputs"],
        "core_hashes": {
            item["path"]: item["sha256"] for item in manifests[1]["files"]
        },
        "run_manifest_sha256": [
            sha256_file(root / "run_manifest.json") for root in roots
        ],
    }
    destination = data_root / "processed/validation_evaluation/full_reproducibility.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def _assert_validation_run_tree(
    run_root: Path,
    *,
    require_manifest: bool,
) -> set[str]:
    if run_root.is_symlink():
        raise ValueError("validation run root must not be a symlink")
    if any(path.is_symlink() for path in run_root.rglob("*")):
        raise ValueError("validation run must not contain symlinks")
    actual = {
        path.relative_to(run_root).as_posix()
        for path in run_root.rglob("*")
        if path.is_file()
    }
    allowed = set(CORE_VALIDATION_FILES)
    if require_manifest:
        allowed.add("run_manifest.json")
    unexpected = sorted(actual - allowed)
    if unexpected:
        raise ValueError("validation run contains unexpected files: " + ", ".join(unexpected))
    return actual


def assert_validation_runtime_gates(
    work: Path,
    candidates: tuple[str, ...],
) -> None:
    for candidate in candidates:
        artifacts = work / "backtests" / candidate / "artifacts"
        shadow = json.loads(
            (artifacts / "shadow_nav_audit.json").read_text(encoding="utf-8")
        )
        stale = json.loads(
            (artifacts / "stale_audit.json").read_text(encoding="utf-8")
        )
        if (
            shadow.get("status") != "ready"
            or stale.get("status") != "ready"
            or stale.get("unexplained_symbols") != 0
        ):
            raise ValueError(f"validation runtime gate failed: {candidate}")


def assert_validation_upstream_releases(data_root: Path) -> None:
    assert_stage_six_release_allowed(data_root / "processed/formal_backtest")


def execute_full_validation_run(
    code_root: Path,
    *,
    run_id: str | None = None,
) -> Path:
    """Execute every Stage 7 calculation into a new isolated run directory."""
    from ashare_multifactor.validation.full_run import execute_validation_stages

    code_root = code_root.resolve()
    data_root = resolve_validation_data_root(code_root)
    assert_validation_upstream_releases(data_root)
    actual_run_id = validate_run_id(run_id or uuid.uuid4().hex)
    run_root = (
        data_root / "artifacts/validation_evaluation/full_runs" / actual_run_id
    )
    if run_root.exists():
        raise FileExistsError(run_root)
    run_root.mkdir(parents=True)
    identity = code_identity(code_root)
    inputs = _validation_input_identity(code_root, data_root)
    started = time.monotonic()
    try:
        execute_validation_stages(code_root, data_root, run_root)
        if code_identity(code_root) != identity:
            raise ValueError("code identity changed during validation run")
        if _validation_input_identity(code_root, data_root) != inputs:
            raise ValueError("frozen inputs changed during validation run")
        elapsed = time.monotonic() - started
        finalize_validation_run(
            run_root,
            run_id=actual_run_id,
            identity=identity,
            inputs=inputs,
            elapsed_seconds=elapsed,
            maximum_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        )
    except BaseException:
        shutil.rmtree(run_root, ignore_errors=True)
        raise
    return run_root


def validate_run_id(run_id: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", run_id) is None:
        raise ValueError("validation run ID must be a safe slug")
    return run_id


def execute_validation_reproducibility(
    code_root: Path,
    *,
    run_id_prefix: str | None = None,
) -> dict[str, object]:
    prefix = run_id_prefix or uuid.uuid4().hex
    first = execute_full_validation_run(code_root, run_id=f"{prefix}_1")
    second = execute_full_validation_run(code_root, run_id=f"{prefix}_2")
    data_root = resolve_validation_data_root(code_root)
    return certify_full_validation_runs(data_root, first.name, second.name)


def _validation_input_identity(
    code_root: Path,
    data_root: Path,
) -> dict[str, object]:
    stage_four_root = data_root / "processed/factor_research"
    stage_four = validate_stage_five_inputs(stage_four_root)
    lineage = json.loads((stage_four_root / "lineage.json").read_text(encoding="utf-8"))
    readiness_records = [
        record
        for record in lineage.get("stages", {}).get("audit", {}).get("outputs", [])
        if record.get("path") == "data_readiness.json"
    ]
    if len(readiness_records) != 1:
        raise ValueError("stage four lineage does not bind data_readiness.json")
    validate_file_records(readiness_records, stage_four_root)
    validation_daily_root = (
        data_root / "processed/validation_evaluation/daily_panel"
    )
    validation_daily = validate_panel_source(
        validation_daily_root,
        Period(date(2017, 1, 1), date(2021, 12, 31)),
    )
    stage_five = resolve_current(data_root / "processed/factor_combination")
    stage_six = resolve_current(data_root / "processed/formal_backtest")
    paths = {
        "research_protocol": code_root / "configs/research_protocol.yaml",
        "market_rules": code_root / "configs/market_rules.yaml",
        "validation_corporate_action_evidence": (
            code_root / "configs/validation_corporate_action_evidence.csv"
        ),
        "validation_security_events": (
            code_root / "configs/validation_security_events.csv"
        ),
        "stage_five_manifest": stage_five.manifest,
        "stage_six_manifest": stage_six.manifest,
        "baostock_dividends": (
            data_root
            / "artifacts/validation_evaluation/source_cache/baostock_dividend/"
            "baostock_dividends_execution_union.parquet"
        ),
    }
    identity = {
        name: {
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }
        for name, path in paths.items()
    }
    identity["stage_four_files"] = [
        record.to_dict() for record in stage_four.consumed_files
    ] + readiness_records
    validation_paths = (
        validation_daily_root / "manifest.json",
        validation_daily.quality_file,
        *validation_daily.files,
    )
    identity["validation_daily_panel_files"] = [
        {
            "path": path.relative_to(data_root).as_posix(),
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }
        for path in validation_paths
        if path is not None
    ]
    return identity


def stage_validation_release(
    code_root: Path,
    *,
    run_id: str | None = None,
    certified: dict[str, object] | None = None,
) -> Path:
    """Stage an auditable release from already verified validation outputs."""
    code_root = code_root.resolve()
    data_root = resolve_validation_data_root(code_root)
    settings = load_config(
        code_root / "configs/research_protocol.yaml"
    ).validation_evaluation
    if settings is None:
        raise ValueError("validation evaluation configuration is missing")
    assert_validation_upstream_releases(data_root)
    reproducibility = certified or _certify_recorded_runs(data_root)
    current_inputs = _validation_input_identity(code_root, data_root)
    if current_inputs != reproducibility.get("inputs"):
        raise ValueError("current validation inputs differ from certified runs")
    run_ids = reproducibility["run_ids"]
    source = (
        data_root / "artifacts/validation_evaluation/full_runs" / str(run_ids[1])
    )
    assert_validation_outputs_sealed(source)
    assert_validation_runtime_gates(source, settings.candidates)

    actual_run_id = validate_run_id(run_id or uuid.uuid4().hex)
    staged = (
        data_root
        / "artifacts/validation_evaluation/validation_runs"
        / actual_run_id
    )
    if staged.exists():
        raise FileExistsError(staged)
    _copy_certified_core(
        source,
        staged,
        reproducibility["core_hashes"],
    )
    if _validation_input_identity(code_root, data_root) != current_inputs:
        shutil.rmtree(staged, ignore_errors=True)
        raise ValueError("validation inputs changed during staging")

    lineage = _validation_lineage(
        code_root,
        data_root,
        settings.candidates,
        reproducibility=reproducibility,
    )
    (staged / "lineage.json").write_text(
        json.dumps(lineage, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if _validation_input_identity(code_root, data_root) != current_inputs:
        shutil.rmtree(staged, ignore_errors=True)
        raise ValueError("validation inputs changed while writing lineage")
    return staged


def run_validation_release(
    code_root: Path,
    *,
    publish: bool,
    run_id: str | None = None,
) -> Path | PublishedRelease:
    identity = code_identity(code_root.resolve())
    if publish and identity["dirty"]:
        raise ValueError("validation release requires a clean Git identity")
    if publish:
        try:
            data_root = resolve_validation_data_root(code_root)
        except FileNotFoundError as error:
            raise ValueError(
                "full validation pipeline reproducibility gate is incomplete"
            ) from error
        reproducibility = _certify_recorded_runs(data_root)
        if (
            reproducibility.get("full_pipeline_reproducible") is not True
            or reproducibility.get("release_eligible") is not True
            or reproducibility.get("code") != identity
        ):
            raise ValueError("full validation pipeline reproducibility gate is incomplete")
    staged = stage_validation_release(
        code_root,
        run_id=run_id,
        certified=reproducibility if publish else None,
    )
    if not publish:
        return staged
    if code_identity(code_root.resolve()) != identity:
        raise ValueError("code identity changed during validation publication")
    data_root = resolve_validation_data_root(code_root)
    if _validation_input_identity(code_root, data_root) != reproducibility["inputs"]:
        raise ValueError("validation inputs changed during validation publication")
    lineage = json.loads((staged / "lineage.json").read_text(encoding="utf-8"))
    return publish_release(
        data_root / "processed/validation_evaluation",
        run_id=staged.name,
        staged_datasets=staged / "datasets",
        staged_artifacts=staged / "artifacts",
        lineage=lineage,
        manifest_metadata={"stage": "validation_evaluation"},
    )


def _certify_recorded_runs(data_root: Path) -> dict[str, object]:
    path = data_root / "processed/validation_evaluation/full_reproducibility.json"
    if not path.is_file():
        raise ValueError("full validation pipeline reproducibility gate is incomplete")
    recorded = json.loads(path.read_text(encoding="utf-8"))
    run_ids = recorded.get("run_ids", [])
    if not isinstance(run_ids, list) or len(run_ids) != 2:
        raise ValueError("full validation pipeline reproducibility gate is incomplete")
    return certify_full_validation_runs(
        data_root,
        validate_run_id(str(run_ids[0])),
        validate_run_id(str(run_ids[1])),
    )


def _copy_certified_core(
    source: Path,
    staged: Path,
    recorded: dict[str, str],
) -> None:
    if set(recorded) != set(CORE_VALIDATION_FILES):
        raise ValueError("certified validation core hash set is incomplete")
    for relative in CORE_VALIDATION_FILES:
        source_path = source / relative
        if relative.startswith("datasets/"):
            destination = staged / "datasets/inputs" / relative.removeprefix("datasets/")
        elif relative.startswith("artifacts/"):
            destination = staged / "artifacts/research" / relative.removeprefix(
                "artifacts/"
            )
        else:
            _, candidate, kind, name = relative.split("/", 3)
            destination_root = "datasets" if kind == "datasets" else "artifacts"
            destination = staged / destination_root / "backtests" / candidate / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination)
        if sha256_file(destination) != recorded[relative]:
            raise ValueError(f"certified validation file changed during staging: {relative}")


def _validation_lineage(
    code_root: Path,
    data_root: Path,
    candidates: tuple[str, ...],
    *,
    reproducibility: dict[str, object],
) -> dict[str, object]:
    source_cache = data_root / "artifacts/validation_evaluation/source_cache"
    stage_five = resolve_current(data_root / "processed/factor_combination")
    stage_six = resolve_current(data_root / "processed/formal_backtest")
    upstream = {
        "stage_four": (
            data_root / "processed/factor_research/report_manifest.json"
        ),
        "stage_four_lineage": data_root / "processed/factor_research/lineage.json",
        "stage_five_manifest": stage_five.manifest,
        "stage_five_lineage": stage_five.lineage,
        "stage_six_manifest": stage_six.manifest,
        "stage_six_lineage": stage_six.lineage,
        "validation_daily_panel": (
            data_root / "processed/validation_evaluation/daily_panel/manifest.json"
        ),
    }
    source_paths = (
        source_cache / "baostock_dividend/baostock_dividends_execution_union.parquet",
        source_cache
        / "baostock_dividend/baostock_dividends_execution_union.metadata.json",
        code_root / "configs/validation_corporate_action_evidence.csv",
        code_root / "configs/validation_security_events.csv",
        source_cache / "cninfo_official_pdfs/000819_1210269787.pdf",
        source_cache / "cninfo_official_pdfs/000916_1204242424.pdf",
        source_cache / "cninfo_official_pdfs/000979_c0f86b35.pdf",
    )
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "period": ["2017-01-01", "2021-12-31"],
        "sealed_final_test_start": "2022-01-01",
        "candidates": list(candidates),
        "upstream": {
            name: file_record(path, root=data_root, role=name).to_dict()
            for name, path in upstream.items()
        },
        "configuration": [
            file_record(path, root=code_root, role="configuration").to_dict()
            for path in (
                code_root / "configs/research_protocol.yaml",
                code_root / "configs/market_rules.yaml",
            )
        ],
        "corporate_action_sources": [
            file_record(
                path,
                root=code_root if path.is_relative_to(code_root) else data_root,
                role="corporate_action_source",
            ).to_dict()
            for path in source_paths
        ],
        "code": code_identity(code_root),
        "reproducibility": reproducibility,
    }
