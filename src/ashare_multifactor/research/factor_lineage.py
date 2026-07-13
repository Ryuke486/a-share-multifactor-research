"""Deterministic lineage and upstream-integrity gates for factor research."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Iterable, Mapping

import yaml

from ashare_multifactor.config import FactorResearchSettings
from ashare_multifactor.data.manifest import DailyPanelSource


LINEAGE_VERSION = 1


def load_field_evidence(config_path: Path) -> dict[str, object]:
    """Parse and hash the evidence file beside the research config."""
    path = config_path.with_name("data_field_evidence.yaml")
    raw_bytes = path.read_bytes()
    payload = yaml.safe_load(raw_bytes)
    try:
        groups = payload["field_groups"]
        statuses = {
            name: groups[name]["point_in_time_status"]
            for name in ("valuation", "industry", "historical_st")
        }
    except (KeyError, TypeError) as error:
        raise ValueError(f"invalid field evidence file: {path}") from error
    if any(status not in {"verified", "unverified"} for status in statuses.values()):
        raise ValueError(f"invalid field evidence status: {path}")
    return {
        "file_name": path.name,
        "size_bytes": len(raw_bytes),
        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "statuses": statuses,
    }


def new_lineage(
    settings: FactorResearchSettings,
    source: DailyPanelSource,
    factor_root: Path,
    field_evidence: Mapping[str, object],
) -> dict[str, object]:
    return {
        "version": LINEAGE_VERSION,
        "factor_config": _config_snapshot(settings),
        "field_evidence": dict(field_evidence),
        "daily_panel": _daily_snapshot(source, factor_root),
        "stages": {},
    }


def load_and_validate_lineage(
    path: Path,
    settings: FactorResearchSettings,
    source: DailyPanelSource,
    factor_root: Path,
    field_evidence: Mapping[str, object],
    required_stages: Iterable[str],
) -> dict[str, object]:
    if not path.is_file():
        raise ValueError("factor research lineage is missing; run audit first")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != LINEAGE_VERSION:
        raise ValueError("invalid factor research lineage")
    if payload.get("factor_config") != _config_snapshot(settings):
        raise ValueError("factor research config does not match frozen lineage")
    if payload.get("field_evidence") != dict(field_evidence):
        raise ValueError("field evidence does not match frozen lineage")
    if payload.get("daily_panel") != _daily_snapshot(source, factor_root):
        raise ValueError("daily panel does not match frozen factor research lineage")
    stages = payload.get("stages")
    if not isinstance(stages, dict):
        raise ValueError("invalid factor research lineage stages")
    for stage in required_stages:
        record = stages.get(stage)
        if not isinstance(record, dict):
            raise ValueError(f"factor research stage lineage is missing: {stage}")
        outputs = record.get("outputs")
        if not isinstance(outputs, list):
            raise ValueError(f"invalid factor research stage outputs: {stage}")
        _validate_file_records(outputs, factor_root)
    return payload


def record_stage(
    lineage: dict[str, object],
    stage: str,
    inputs: Iterable[Path],
    outputs: Iterable[Path],
    factor_root: Path,
) -> None:
    stages = lineage.get("stages")
    if not isinstance(stages, dict):
        raise ValueError("invalid factor research lineage stages")
    stages[stage] = {
        "inputs": file_records(inputs, factor_root),
        "outputs": file_records(outputs, factor_root),
    }


def file_records(paths: Iterable[Path], root: Path) -> list[dict[str, object]]:
    files = sorted(_expand_files(paths), key=lambda path: _display_path(path, root))
    return [
        {
            "path": _display_path(path, root),
            "size_bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in files
    ]


def _validate_file_records(records: list[object], root: Path) -> None:
    for item in records:
        if not isinstance(item, Mapping):
            raise ValueError("invalid upstream file record")
        path_value = item.get("path")
        if not isinstance(path_value, str):
            raise ValueError("invalid upstream file path")
        path = root / path_value
        if not path.is_file():
            raise ValueError(f"upstream file is missing: {path_value}")
        if _sha256(path) != item.get("sha256"):
            raise ValueError(f"upstream file digest mismatch: {path_value}")
        if path.stat().st_size != item.get("size_bytes"):
            raise ValueError(f"upstream file size mismatch: {path_value}")


def _config_snapshot(settings: FactorResearchSettings) -> dict[str, object]:
    raw_payload = {
        key: value.isoformat() if isinstance(value, date) else value
        for key, value in asdict(settings).items()
    }
    payload = json.loads(_canonical_json(raw_payload))
    encoded = _canonical_json(payload)
    return {"payload": payload, "sha256": hashlib.sha256(encoded).hexdigest()}


def _daily_snapshot(source: DailyPanelSource, factor_root: Path) -> dict[str, object]:
    manifest_path = factor_root / "daily_panel" / "manifest.json"
    return {
        "manifest": file_records((manifest_path,), factor_root)[0],
        "partitions": file_records(source.files, factor_root),
    }


def _canonical_json(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _expand_files(paths: Iterable[Path]) -> tuple[Path, ...]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(item for item in path.rglob("*") if item.is_file())
        else:
            files.append(path)
    return tuple(files)


def _display_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as error:
        raise ValueError(f"lineage path escapes factor research root: {path}") from error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
