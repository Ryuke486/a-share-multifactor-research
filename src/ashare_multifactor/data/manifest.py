"""Validate published daily-panel manifests and their Parquet partitions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Mapping

import polars as pl

from ashare_multifactor.config import Period


@dataclass(frozen=True)
class DailyPanelSource:
    manifest: dict[str, object]
    files: tuple[Path, ...]
    manifest_sha256: str | None = None
    manifest_size_bytes: int | None = None


def _manifest_date(
    payload: Mapping[str, object], key: str, manifest_path: Path
) -> date:
    try:
        return date.fromisoformat(str(payload[key]))
    except (KeyError, ValueError) as error:
        raise ValueError(f"invalid {key} in data manifest: {manifest_path}") from error


def _read_manifest(path: Path) -> tuple[dict[str, object], bytes]:
    raw_bytes = path.read_bytes()
    payload = json.loads(raw_bytes)
    if not isinstance(payload, dict):
        raise ValueError(f"daily panel manifest must be a JSON object: {path}")
    return payload, raw_bytes


def _file_summary(path: Path) -> dict[str, int | str]:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return {"sha256": digest.hexdigest(), "size_bytes": path.stat().st_size}


def _parquet_summary(path: Path) -> tuple[int, date, date]:
    try:
        summary = (
            pl.scan_parquet(path)
            .select(
                pl.len().alias("rows"),
                pl.col("date").min().alias("min_date"),
                pl.col("date").max().alias("max_date"),
            )
            .collect()
            .row(0, named=True)
        )
    except pl.exceptions.PolarsError as error:
        raise ValueError(f"invalid daily panel parquet partition: {path}") from error
    minimum = summary["min_date"]
    maximum = summary["max_date"]
    if not isinstance(minimum, date) or not isinstance(maximum, date):
        raise ValueError(f"invalid date column in daily panel partition: {path}")
    return int(summary["rows"]), minimum, maximum


def _partition_path(root: Path, relative_path: object) -> tuple[str, PurePosixPath, Path]:
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
    path = root.joinpath(*relative.parts)
    return relative_path, relative, path


def validate_panel_source(root: Path, allowed: Period) -> DailyPanelSource:
    """Validate a daily panel without trusting paths or declared partition statistics."""
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink():
        raise ValueError(f"daily panel manifest uses symlink: {manifest_path}")
    if not manifest_path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"daily panel manifest escapes daily panel root: {manifest_path}")
    manifest, manifest_bytes = _read_manifest(manifest_path)
    minimum = _manifest_date(manifest, "min_date", manifest_path)
    maximum = _manifest_date(manifest, "max_date", manifest_path)
    if maximum < minimum:
        raise ValueError(f"data manifest max_date precedes min_date: {manifest_path}")
    if minimum < allowed.start:
        raise ValueError(
            "daily panel manifest min_date predates allowed period start: "
            f"{minimum.isoformat()} < {allowed.start.isoformat()}"
        )
    if maximum > allowed.end:
        raise ValueError(
            "daily panel manifest max_date exceeds allowed period end: "
            f"{maximum.isoformat()} > {allowed.end.isoformat()}"
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
    if years != list(range(minimum.year, maximum.year + 1)):
        raise ValueError(
            "daily panel manifest years must match the continuous min/max year range"
        )

    records: list[tuple[dict[str, object], Path, date, date]] = []
    expected_relative_paths: set[str] = set()
    for item in partitions:
        if not isinstance(item, dict):
            raise ValueError("invalid daily panel partition summary")
        relative_path, relative, path = _partition_path(root, item.get("relative_path"))
        year = item.get("year")
        rows = item.get("rows")
        size_bytes = item.get("size_bytes")
        sha256 = item.get("sha256")
        if not isinstance(year, int) or isinstance(year, bool):
            raise ValueError(f"invalid daily panel partition year: {relative_path}")
        if relative.parent.as_posix() != f"year={year}":
            raise ValueError(f"daily panel partition year/path mismatch: {relative_path}")
        if relative_path != f"year={year}/part-000.parquet":
            raise ValueError(
                "daily panel manifest must contain exactly one part-000 partition per year"
            )
        if year not in years:
            raise ValueError(f"daily panel partition year absent from manifest years: {year}")
        if not allowed.start.year <= year <= allowed.end.year:
            raise ValueError(f"daily panel partition year outside allowed period: {year}")
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
            partition_minimum < allowed.start
            or partition_maximum > allowed.end
        ):
            raise ValueError(f"daily panel partition dates outside allowed period: {relative_path}")
        if partition_minimum.year != year or partition_maximum.year != year:
            raise ValueError(f"daily panel partition year/date mismatch: {relative_path}")
        if relative_path in expected_relative_paths:
            raise ValueError(f"duplicate daily panel partition path: {relative_path}")
        expected_relative_paths.add(relative_path)
        records.append((item, path, partition_minimum, partition_maximum))

    actual_relative_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.parquet")
        if path.is_file()
    }
    if actual_relative_paths != expected_relative_paths:
        missing = sorted(expected_relative_paths - actual_relative_paths)
        extra = sorted(actual_relative_paths - expected_relative_paths)
        raise ValueError(
            f"daily panel partition file list mismatch: missing={missing}, extra={extra}"
        )

    root_resolved = root.resolve()
    for record, path, partition_minimum, partition_maximum in records:
        if path.is_symlink() or not path.resolve().is_relative_to(root_resolved):
            raise ValueError(
                "invalid daily panel partition path: "
                f"{path.relative_to(root).as_posix()}"
            )
        actual_file = _file_summary(path)
        if record["sha256"] != actual_file["sha256"]:
            raise ValueError(
                "daily panel partition digest mismatch: "
                f"{path.relative_to(root).as_posix()}"
            )
        if record["size_bytes"] != actual_file["size_bytes"]:
            raise ValueError(
                "daily panel partition size mismatch: "
                f"{path.relative_to(root).as_posix()}"
            )
        actual_rows, actual_minimum, actual_maximum = _parquet_summary(path)
        if record["rows"] != actual_rows:
            raise ValueError(
                "daily panel partition rows do not match parquet: "
                f"{path.relative_to(root).as_posix()}"
            )
        if actual_minimum < allowed.start or actual_maximum > allowed.end:
            raise ValueError(
                "daily panel partition dates outside allowed period: "
                f"{path.relative_to(root).as_posix()}"
            )
        if actual_minimum != partition_minimum or actual_maximum != partition_maximum:
            raise ValueError(
                "daily panel partition dates do not match parquet: "
                f"{path.relative_to(root).as_posix()}"
            )
        year = int(record["year"])
        if actual_minimum.year != year or actual_maximum.year != year:
            raise ValueError(
                "daily panel partition year does not match parquet: "
                f"{path.relative_to(root).as_posix()}"
            )

    declared_rows = manifest.get("rows")
    if (
        not isinstance(declared_rows, int)
        or isinstance(declared_rows, bool)
        or declared_rows != sum(int(record["rows"]) for record, _, _, _ in records)
    ):
        raise ValueError("daily panel partition rows do not match data manifest")
    partition_years = sorted(int(record["year"]) for record, _, _, _ in records)
    if partition_years != years:
        raise ValueError(
            "daily panel manifest must contain exactly one part-000 partition per year"
        )
    if min(item[2] for item in records) != minimum or max(item[3] for item in records) != maximum:
        raise ValueError("daily panel partition dates do not match data manifest")
    return DailyPanelSource(
        manifest=manifest,
        files=tuple(path for _, path, _, _ in sorted(records, key=lambda item: str(item[1]))),
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        manifest_size_bytes=len(manifest_bytes),
    )
