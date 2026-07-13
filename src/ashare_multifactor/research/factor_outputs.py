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
from ashare_multifactor.research.factor_lineage import (
    file_records,
    lineage_identity,
    stage_outputs_digest,
    validate_file_records,
)
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


def expected_stage_outputs(
    factor_root: Path,
    stage: str,
    definitions: tuple[FactorDefinition, ...],
) -> tuple[Path, ...]:
    """Return the exact preregistered file set for a completed stage."""
    if stage == "audit":
        return (factor_root / "data_readiness.json",)
    if stage == "factors":
        families = tuple(dict.fromkeys(definition.family for definition in definitions))
        monthly = tuple(
            factor_root / "monthly_raw" / f"family={family}" / "part-000.parquet"
            for family in families
        )
        return monthly + tuple(
            factor_root / name for name in FACTOR_OUTPUT_NAMES if name != "monthly_raw"
        )
    if stage == "evaluate":
        return tuple(factor_root / name for name in EVALUATION_OUTPUT_NAMES)
    if stage == "report":
        return (factor_root / "report_manifest.json",)
    raise ValueError(f"unsupported factor output stage: {stage}")


def write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_parquet_atomic(path: Path, frame: pl.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        frame.write_parquet(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def copy_file_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def replace_directory(staging: Path, target: Path) -> None:
    if not target.exists():
        os.replace(staging, target)
        return
    backup = target.parent / f".{target.name}-{uuid4().hex}.backup"
    os.replace(target, backup)
    try:
        os.replace(staging, target)
    except BaseException as publish_error:
        try:
            os.replace(backup, target)
        except BaseException as rollback_error:
            try:
                shutil.copytree(backup, target)
            except BaseException as recovery_error:
                error = RuntimeError(
                    "directory publish failed; rollback rename failed; "
                    f"backup copy recovery failed; backup retained at {backup}"
                )
                error.add_note(f"publish error: {publish_error!r}")
                error.add_note(f"rollback rename error: {rollback_error!r}")
                error.add_note(f"backup copy error: {recovery_error!r}")
                raise error from publish_error
            error = RuntimeError(
                "directory publish failed; rollback rename failed; "
                f"restored target from backup copy; backup retained at {backup}"
            )
            error.add_note(f"publish error: {publish_error!r}")
            error.add_note(f"rollback rename error: {rollback_error!r}")
            raise error from publish_error
        raise
    shutil.rmtree(backup, ignore_errors=True)


def invalidate_factor_outputs(factor_root: Path, artifact_root: Path) -> None:
    """Remove every published factor output while preserving the daily cache."""
    if factor_root.exists():
        for path in factor_root.iterdir():
            if path.name != "daily_panel":
                _remove_path(path)
    shutil.rmtree(artifact_root, ignore_errors=True)


def clear_stage_and_downstream(
    factor_root: Path,
    artifact_root: Path,
    stage: str,
) -> None:
    """Remove current and downstream outputs without touching the daily cache."""
    stage_names = {
        "audit": (
            "data_readiness.json",
            *FACTOR_OUTPUT_NAMES,
            *EVALUATION_OUTPUT_NAMES,
            "report_manifest.json",
        ),
        "factors": (
            *FACTOR_OUTPUT_NAMES,
            *EVALUATION_OUTPUT_NAMES,
            "report_manifest.json",
        ),
        "evaluate": (*EVALUATION_OUTPUT_NAMES, "report_manifest.json"),
        "report": ("report_manifest.json",),
    }
    try:
        names = stage_names[stage]
    except KeyError as error:
        raise ValueError(f"unsupported factor cleanup stage: {stage}") from error
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
    *,
    research_identity: str,
    evaluate_outputs_sha256: str,
) -> Path:
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
            {
                "version": 2,
                "research_identity": research_identity,
                "evaluate_outputs_sha256": evaluate_outputs_sha256,
                "files": file_records(manifest_files, staging),
            },
        )
        replace_directory(staging, output_root)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return output_root / "manifest.json"


def validate_published_report(
    lineage: dict[str, object],
    factor_root: Path,
    artifact_root: Path,
) -> None:
    """Bind report lineage to its copied manifest and every artifact file."""
    report_manifest = factor_root / "report_manifest.json"
    artifact_manifest = artifact_root / "manifest.json"
    if not report_manifest.is_file() or not artifact_manifest.is_file():
        raise ValueError("published report manifest is missing")
    if report_manifest.is_symlink() or artifact_manifest.is_symlink():
        raise ValueError("published report manifest uses symlink")
    if report_manifest.read_bytes() != artifact_manifest.read_bytes():
        raise ValueError("published report manifest copies do not match")
    try:
        payload = json.loads(report_manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise ValueError("invalid published report manifest") from error
    if not isinstance(payload, dict) or payload.get("version") != 2:
        raise ValueError("invalid published report manifest")
    if payload.get("research_identity") != lineage_identity(lineage):
        raise ValueError("published report research identity mismatch")
    if payload.get("evaluate_outputs_sha256") != stage_outputs_digest(lineage, "evaluate"):
        raise ValueError("published report evaluate output digest mismatch")
    records = payload.get("files")
    if not isinstance(records, list):
        raise ValueError("invalid published report artifact records")
    artifact_entries = tuple(artifact_root.rglob("*"))
    if any(path.is_symlink() for path in artifact_entries):
        raise ValueError("published report artifact uses symlink")
    recorded_paths = validate_file_records(records, artifact_root)
    actual_paths = {
        path.relative_to(artifact_root).as_posix()
        for path in artifact_entries
        if path.is_file() and path != artifact_manifest
    }
    if recorded_paths != actual_paths:
        raise ValueError(
            "published report artifact set mismatch: "
            f"missing={sorted(recorded_paths - actual_paths)}, "
            f"extra={sorted(actual_paths - recorded_paths)}"
        )


def _write_csv_atomic(path: Path, frame: pl.DataFrame) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        frame.write_csv(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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
