"""Configuration, source-manifest, and per-file lineage for MVP intermediates."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Mapping

from ashare_multifactor.config import ResearchConfig
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


@dataclass(frozen=True)
class DailyPanelSource:
    manifest: dict[str, object]
    files: tuple[Path, ...]


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


def _manifest_date(
    payload: Mapping[str, object], key: str, manifest_path: Path
) -> date:
    try:
        return date.fromisoformat(str(payload[key]))
    except (KeyError, ValueError) as error:
        raise ValueError(f"invalid {key} in data manifest: {manifest_path}") from error


def validate_daily_panel_source(config: ResearchConfig) -> DailyPanelSource:
    manifest_path = config.paths.processed / "daily_panel/manifest.json"
    manifest = read_json(manifest_path)
    minimum = _manifest_date(manifest, "min_date", manifest_path)
    maximum = _manifest_date(manifest, "max_date", manifest_path)
    if maximum < minimum:
        raise ValueError(f"data manifest max_date precedes min_date: {manifest_path}")
    if minimum < config.smoke_data.start:
        raise ValueError(
            "daily panel manifest min_date predates configured smoke_data start: "
            f"{minimum.isoformat()} < {config.smoke_data.start.isoformat()}"
        )
    if maximum > config.smoke_data.end:
        raise ValueError(
            "daily panel manifest max_date exceeds configured smoke_data end: "
            f"{maximum.isoformat()} > {config.smoke_data.end.isoformat()}"
        )

    partitions = manifest.get("partitions")
    if not isinstance(partitions, list) or not partitions:
        raise ValueError("daily panel manifest must contain partition summaries")
    years = manifest.get("years")
    if (
        not isinstance(years, list)
        or not years
        or any(not isinstance(year, int) or isinstance(year, bool) for year in years)
        or years != sorted(set(years))
    ):
        raise ValueError("daily panel manifest must contain sorted unique years")

    panel_root = manifest_path.parent
    records: list[tuple[dict[str, object], Path, date, date]] = []
    expected_relative_paths: set[str] = set()
    for item in partitions:
        if not isinstance(item, dict):
            raise ValueError("invalid daily panel partition summary")
        relative_path = item.get("relative_path")
        year = item.get("year")
        rows = item.get("rows")
        size_bytes = item.get("size_bytes")
        sha256 = item.get("sha256")
        if not isinstance(relative_path, str):
            raise ValueError("invalid daily panel partition relative_path")
        relative = PurePosixPath(relative_path)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or len(relative.parts) != 2
            or not relative.name.startswith("part-")
            or relative.suffix != ".parquet"
        ):
            raise ValueError(f"invalid daily panel partition path: {relative_path}")
        if not isinstance(year, int) or isinstance(year, bool):
            raise ValueError(f"invalid daily panel partition year: {relative_path}")
        if relative.parent.as_posix() != f"year={year}":
            raise ValueError(f"daily panel partition year/path mismatch: {relative_path}")
        if year not in years:
            raise ValueError(f"daily panel partition year absent from manifest years: {year}")
        if not config.smoke_data.start.year <= year <= config.smoke_data.end.year:
            raise ValueError(f"daily panel partition year outside configured smoke_data: {year}")
        if not isinstance(rows, int) or isinstance(rows, bool) or rows <= 0:
            raise ValueError(f"invalid daily panel partition rows: {relative_path}")
        if (
            not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes <= 0
        ):
            raise ValueError(f"invalid daily panel partition size: {relative_path}")
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise ValueError(f"invalid daily panel partition sha256: {relative_path}")
        partition_minimum = _manifest_date(item, "min_date", manifest_path)
        partition_maximum = _manifest_date(item, "max_date", manifest_path)
        if partition_maximum < partition_minimum:
            raise ValueError(f"daily panel partition date range is reversed: {relative_path}")
        if (
            partition_minimum < config.smoke_data.start
            or partition_maximum > config.smoke_data.end
        ):
            raise ValueError(
                f"daily panel partition dates outside configured smoke_data: {relative_path}"
            )
        if relative_path in expected_relative_paths:
            raise ValueError(f"duplicate daily panel partition path: {relative_path}")
        expected_relative_paths.add(relative_path)
        records.append(
            (item, panel_root.joinpath(*relative.parts), partition_minimum, partition_maximum)
        )

    actual_relative_paths = {
        path.relative_to(panel_root).as_posix()
        for path in panel_root.rglob("*.parquet")
        if path.is_file()
    }
    if actual_relative_paths != expected_relative_paths:
        missing = sorted(expected_relative_paths - actual_relative_paths)
        extra = sorted(actual_relative_paths - expected_relative_paths)
        raise ValueError(
            f"daily panel partition file list mismatch: missing={missing}, extra={extra}"
        )

    for record, path, _, _ in records:
        actual = _file_summary(path)
        if (
            record["sha256"] != actual["sha256"]
            or record["size_bytes"] != actual["size_bytes"]
        ):
            raise ValueError(
                "daily panel partition digest mismatch: "
                f"{path.relative_to(panel_root).as_posix()}"
            )

    declared_rows = manifest.get("rows")
    if (
        not isinstance(declared_rows, int)
        or isinstance(declared_rows, bool)
        or declared_rows != sum(int(record["rows"]) for record, _, _, _ in records)
    ):
        raise ValueError("daily panel partition rows do not match data manifest")
    partition_years = sorted({int(record["year"]) for record, _, _, _ in records})
    if partition_years != years:
        raise ValueError("daily panel partition years do not match data manifest")
    if min(item[2] for item in records) != minimum or max(item[3] for item in records) != maximum:
        raise ValueError("daily panel partition dates do not match data manifest")
    return DailyPanelSource(
        manifest=manifest,
        files=tuple(path for _, path, _, _ in sorted(records, key=lambda item: str(item[1]))),
    )


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
