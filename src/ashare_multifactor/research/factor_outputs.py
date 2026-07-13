"""Atomic, stably ordered outputs for factor research."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

import polars as pl

from ashare_multifactor.config import FactorResearchSettings
from ashare_multifactor.factors.definitions import FactorDefinition
from ashare_multifactor.research.factor_cards import write_factor_cards
from ashare_multifactor.research.factor_evaluation import FactorEvaluationBundle
from ashare_multifactor.research.factor_lineage import file_records
from ashare_multifactor.research.factor_redundancy import FactorRedundancyBundle


FACTOR_OUTPUT_NAMES = (
    "monthly_raw",
    "factor_features.parquet",
    "forward_returns.parquet",
    "factor_panel.parquet",
)
EVALUATION_FILE_NAMES = (
    "rank_ic.parquet",
    "quantile_returns.parquet",
    "subperiod_metrics.parquet",
    "factor_turnover.parquet",
    "factor_summary.parquet",
)
EVALUATION_OUTPUT_NAMES = (
    *EVALUATION_FILE_NAMES,
    "factor_classifications.parquet",
    "factor_correlations.parquet",
    "redundancy_flags.parquet",
)


def write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def write_parquet_atomic(path: Path, frame: pl.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.write_parquet(temporary)
    os.replace(temporary, path)


def replace_directory(staging: Path, target: Path) -> None:
    backup = target.parent / f".{target.name}-{uuid4().hex}.backup"
    if target.exists():
        os.replace(target, backup)
    try:
        os.replace(staging, target)
    except BaseException:
        if backup.exists():
            os.replace(backup, target)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def invalidate_factor_outputs(factor_root: Path, artifact_root: Path) -> None:
    """Remove every published factor output while preserving the daily cache."""
    if factor_root.exists():
        for path in factor_root.iterdir():
            if path.name != "daily_panel":
                _remove_path(path)
    shutil.rmtree(artifact_root, ignore_errors=True)


def clear_downstream(
    factor_root: Path,
    artifact_root: Path,
    *,
    after: str,
) -> None:
    """Physically invalidate stale outputs after a successful stage rerun."""
    if after == "audit":
        names = (*FACTOR_OUTPUT_NAMES, *EVALUATION_OUTPUT_NAMES)
    elif after == "factors":
        names = EVALUATION_OUTPUT_NAMES
    elif after == "evaluate":
        names = ()
    else:
        raise ValueError(f"unsupported invalidation boundary: {after}")
    for name in names:
        _remove_path(factor_root / name)
    shutil.rmtree(artifact_root, ignore_errors=True)


def write_monthly_raw(root: Path, panel: pl.DataFrame) -> None:
    staging = root.parent / f".{root.name}-{uuid4().hex}.tmp"
    staging.mkdir(parents=True)
    try:
        for family_frame in panel.partition_by("family", maintain_order=True):
            family = family_frame.item(0, "family")
            output = staging / f"family={family}" / "part-000.parquet"
            write_parquet_atomic(output, family_frame.sort("date", "symbol", "factor_name"))
        replace_directory(staging, root)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def publish_report(
    output_root: Path,
    readiness_payload: object,
    evaluation: FactorEvaluationBundle,
    classifications: pl.DataFrame,
    redundancy: FactorRedundancyBundle,
    definitions: tuple[FactorDefinition, ...],
    settings: FactorResearchSettings,
) -> None:
    staging = output_root.parent / f".{output_root.name}-{uuid4().hex}.tmp"
    staging.mkdir(parents=True)
    try:
        write_json_atomic(staging / "data_readiness.json", readiness_payload)
        tables = {
            "factor_summary.csv": evaluation.factor_summary,
            "rank_ic.csv": evaluation.rank_ic,
            "quantile_returns.csv": evaluation.quantile_returns,
            "subperiod_metrics.csv": evaluation.subperiod_metrics,
            "factor_turnover.csv": evaluation.factor_turnover,
            "factor_classifications.csv": classifications,
            "factor_correlations.csv": redundancy.factor_correlations,
            "redundancy_flags.csv": redundancy.redundancy_flags,
        }
        for name, frame in tables.items():
            _write_csv_atomic(staging / name, frame)
        write_factor_cards(
            evaluation,
            classifications,
            redundancy,
            definitions,
            settings,
            staging,
        )
        (staging / "report.md").write_text(
            _report_text(classifications, definitions, settings),
            encoding="utf-8",
        )
        manifest_files = [
            path for path in staging.rglob("*") if path.is_file() and path.name != "manifest.json"
        ]
        write_json_atomic(
            staging / "manifest.json",
            {"version": 1, "files": file_records(manifest_files, staging)},
        )
        replace_directory(staging, output_root)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _write_csv_atomic(path: Path, frame: pl.DataFrame) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.write_csv(temporary)
    os.replace(temporary, path)


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _report_text(
    classifications: pl.DataFrame,
    definitions: tuple[FactorDefinition, ...],
    settings: FactorResearchSettings,
) -> str:
    counts = {
        label: classifications.filter(pl.col("classification") == label).height
        for label in ("candidate", "watch", "reject")
    }
    return f"""# A股单因子研究报告

- 研究区间：{settings.analysis_start.isoformat()} 至 {settings.analysis_end.isoformat()}
- 预注册因子：{len(definitions)}
- candidate / watch / reject：{counts["candidate"]} / {counts["watch"]} / {counts["reject"]}

零个candidate是合法研究结果。价值因子在point-in-time口径未核验前最多为watch。
"""
