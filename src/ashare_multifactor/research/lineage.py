"""Configuration, source-manifest, and per-file lineage for MVP intermediates."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Mapping

from ashare_multifactor.config import ResearchConfig
from ashare_multifactor.data.manifest import DailyPanelSource, validate_panel_source
from ashare_multifactor.research.pipeline_io import read_json, write_json


STAGE_ORDER = ("signals", "targets", "backtest", "report")
PROCESSED_STAGE_ARTIFACTS = {
    "signals": ("signals.parquet", "monthly_signals.parquet", "rank_ic.parquet"),
    "targets": ("target_weights.parquet",),
    "backtest": (
        "net_nav.parquet",
        "gross_nav.parquet",
        "trades.parquet",
        "holdings.parquet",
        "blocked_orders.parquet",
        "net_summary.json",
        "gross_summary.json",
    ),
    "report": (),
}
PUBLISHED_STAGE_ARTIFACTS = {
    "signals": ("rank_ic.csv",),
    "targets": ("target_weights.parquet",),
    "backtest": (
        "nav.csv",
        "gross_nav.csv",
        "trades.csv",
        "holdings.parquet",
        "blocked_orders.csv",
    ),
    "report": ("nav.png", "drawdown.png", "summary.json", "report.md"),
}


def config_snapshot(config: ResearchConfig) -> dict[str, object]:
    return {
        "paths": {
            "raw_unadjusted": str(config.paths.raw_unadjusted),
            "raw_backward_adjusted": str(config.paths.raw_backward_adjusted),
            "processed": str(config.paths.processed),
            "artifacts": str(config.paths.artifacts),
        },
        "periods": {
            name: {
                "start": getattr(config, name).start.isoformat(),
                "end": getattr(config, name).end.isoformat(),
            }
            for name in (
                "research",
                "validation",
                "test",
                "smoke_data",
                "smoke_analysis",
            )
        },
        "mvp": asdict(config.mvp),
    }


def validate_daily_panel_source(config: ResearchConfig) -> DailyPanelSource:
    try:
        return validate_panel_source(
            config.paths.processed / "daily_panel",
            config.smoke_data,
        )
    except ValueError as error:
        message = str(error).replace(
            "allowed period start", "configured smoke_data start"
        ).replace("allowed period end", "configured smoke_data end")
        raise ValueError(message) from error


def load_data_manifest(config: ResearchConfig) -> dict[str, object]:
    return validate_daily_panel_source(config).manifest


def daily_panel_files(config: ResearchConfig) -> list[Path]:
    return list(validate_daily_panel_source(config).files)


def _object_digest(payload: object) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _file_summary(path: Path) -> dict[str, int | str]:
    if not path.exists():
        raise FileNotFoundError(f"required intermediate result not found: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return {"sha256": digest.hexdigest(), "size_bytes": path.stat().st_size}


def _identity(
    config: ResearchConfig, source_manifest: Mapping[str, object] | None = None
) -> dict[str, object]:
    source_manifest = source_manifest or load_data_manifest(config)
    schema_version = source_manifest.get("schema_version")
    if not isinstance(schema_version, str):
        raise ValueError("daily panel manifest must contain schema_version")
    snapshot = config_snapshot(config)
    identity = {
        "periods": snapshot["periods"],
        "mvp": snapshot["mvp"],
        "schema_version": schema_version,
        "input_manifest_digest": _object_digest(source_manifest),
    }
    identity["fingerprint"] = _object_digest(identity)
    return identity


def _lineage_path(config: ResearchConfig) -> Path:
    return config.paths.processed / "mvp/lineage.json"


def _matching_lineage(
    config: ResearchConfig, source_manifest: Mapping[str, object] | None = None
) -> dict[str, object]:
    path = _lineage_path(config)
    lineage = read_json(path)
    expected = _identity(config, source_manifest)
    if lineage.get("fingerprint") != expected["fingerprint"]:
        raise ValueError(
            "intermediate lineage does not match current configuration and source manifest"
        )
    if not isinstance(lineage.get("artifacts"), dict):
        raise ValueError(f"invalid intermediate lineage artifacts: {path}")
    return lineage


def require_lineage(
    config: ResearchConfig,
    required_artifacts: tuple[str, ...],
    source_manifest: Mapping[str, object] | None = None,
) -> None:
    lineage = _matching_lineage(config, source_manifest)
    fingerprint = lineage["fingerprint"]
    artifacts = lineage["artifacts"]
    assert isinstance(artifacts, dict)
    for name in required_artifacts:
        record = artifacts.get(name)
        if not isinstance(record, dict) or record.get("fingerprint") != fingerprint:
            raise ValueError(f"intermediate lineage missing required artifact: {name}")
        actual = _file_summary(config.paths.processed / "mvp" / name)
        if (
            record.get("sha256") != actual["sha256"]
            or record.get("size_bytes") != actual["size_bytes"]
        ):
            raise ValueError(f"intermediate artifact digest mismatch: {name}")


def discard_stage_outputs(config: ResearchConfig, stage: str) -> None:
    if stage not in STAGE_ORDER:
        raise ValueError(f"invalid lineage stage: {stage}")
    stage_index = STAGE_ORDER.index(stage)
    processed_root = config.paths.processed / "mvp"
    artifact_root = config.paths.artifacts / "mvp"
    for downstream_stage in STAGE_ORDER[stage_index:]:
        for name in PROCESSED_STAGE_ARTIFACTS[downstream_stage]:
            (processed_root / name).unlink(missing_ok=True)
        for name in PUBLISHED_STAGE_ARTIFACTS[downstream_stage]:
            (artifact_root / name).unlink(missing_ok=True)


def invalidate_stage(
    config: ResearchConfig,
    stage: str,
    source_manifest: Mapping[str, object] | None = None,
) -> None:
    if stage not in STAGE_ORDER:
        raise ValueError(f"invalid lineage stage: {stage}")
    discard_stage_outputs(config, stage)
    identity = _identity(config, source_manifest)
    if stage == "signals" or not _lineage_path(config).exists():
        lineage: dict[str, object] = {
            **identity,
            "artifacts": {},
            "completed_stages": [],
        }
    else:
        lineage = _matching_lineage(config, source_manifest)
    stage_index = STAGE_ORDER.index(stage)
    artifacts = lineage["artifacts"]
    assert isinstance(artifacts, dict)
    retained_artifacts = {}
    for name, record in artifacts.items():
        artifact_stage = record.get("stage") if isinstance(record, dict) else None
        if artifact_stage in STAGE_ORDER and STAGE_ORDER.index(artifact_stage) < stage_index:
            retained_artifacts[name] = record
    lineage["artifacts"] = retained_artifacts
    completed = lineage.get("completed_stages", [])
    lineage["completed_stages"] = [
        name for name in completed if name in STAGE_ORDER and STAGE_ORDER.index(name) < stage_index
    ]
    write_json(_lineage_path(config), lineage)


def record_stage(
    config: ResearchConfig,
    stage: str,
    source_manifest: Mapping[str, object] | None = None,
) -> None:
    if stage not in STAGE_ORDER:
        raise ValueError(f"invalid lineage stage: {stage}")
    lineage = _matching_lineage(config, source_manifest)
    artifacts = lineage["artifacts"]
    assert isinstance(artifacts, dict)
    fingerprint = lineage["fingerprint"]
    for name in PROCESSED_STAGE_ARTIFACTS[stage]:
        artifacts[name] = {
            "stage": stage,
            "fingerprint": fingerprint,
            **_file_summary(config.paths.processed / "mvp" / name),
        }
    completed = set(lineage.get("completed_stages", []))
    completed.add(stage)
    lineage["completed_stages"] = [name for name in STAGE_ORDER if name in completed]
    write_json(_lineage_path(config), lineage)


def invalidate_build(config: ResearchConfig) -> None:
    _lineage_path(config).unlink(missing_ok=True)
