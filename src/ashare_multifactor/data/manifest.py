"""Validate published daily-panel manifests and their Parquet partitions."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
import stat
from typing import Mapping

import polars as pl

from ashare_multifactor.config import Period


@dataclass(frozen=True)
class DailyPanelSource:
    manifest: dict[str, object]
    files: tuple[Path, ...]
    manifest_sha256: str | None = None
    manifest_size_bytes: int | None = None
    quality_file: Path | None = None
    quality_record: dict[str, object] | None = None


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
    quality_record = manifest.get("quality_issues")
    if not isinstance(quality_record, dict):
        raise ValueError("daily panel manifest must contain quality issues identity")
    expected_quality_keys = {
        "relative_path",
        "records",
        "quarantined_rows",
        "size_bytes",
        "sha256",
    }
    if set(quality_record) != expected_quality_keys:
        raise ValueError("invalid daily panel quality issues identity")
    if quality_record.get("relative_path") != "quality_issues.json":
        raise ValueError("invalid daily panel quality issues relative_path")
    quality_path = root / "quality_issues.json"
    if quality_path.is_symlink():
        raise ValueError(f"daily panel quality issues file uses symlink: {quality_path}")
    if not quality_path.is_file():
        raise ValueError(f"daily panel quality issues file is missing: {quality_path}")
    if not quality_path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"daily panel quality issues file escapes root: {quality_path}")
    quality_bytes = quality_path.read_bytes()
    try:
        quality_payload = json.loads(quality_bytes)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid daily panel quality issues JSON: {quality_path}") from error
    if not isinstance(quality_payload, list):
        raise ValueError("daily panel quality issues must contain a JSON list")
    records_count = quality_record.get("records")
    quarantined_rows = quality_record.get("quarantined_rows")
    if (
        not isinstance(records_count, int)
        or isinstance(records_count, bool)
        or records_count != len(quality_payload)
    ):
        raise ValueError("daily panel quality issues record count mismatch")
    actual_quarantined_rows = sum(
        int(record.get("count", 0))
        for record in quality_payload
        if isinstance(record, dict)
        and record.get("code") == "invalid_ohlc_quarantined"
        and isinstance(record.get("count", 0), int)
        and not isinstance(record.get("count", 0), bool)
    )
    if (
        not isinstance(quarantined_rows, int)
        or isinstance(quarantined_rows, bool)
        or quarantined_rows < 0
        or quarantined_rows != actual_quarantined_rows
    ):
        raise ValueError("daily panel quality issues quarantined row count mismatch")
    quality_summary = _file_summary(quality_path)
    if quality_record.get("sha256") != quality_summary["sha256"]:
        raise ValueError("daily panel quality issues digest mismatch")
    if quality_record.get("size_bytes") != quality_summary["size_bytes"]:
        raise ValueError("daily panel quality issues size mismatch")
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
        quality_file=quality_path,
        quality_record=dict(quality_record),
    )


def validate_panel_source_at(root_fd: int, allowed: Period) -> DailyPanelSource:
    """Validate one panel using only an already-open root directory.

    The returned ``files`` are validated relative names.  Callers that need
    readable paths must replace them with their own held file descriptors.
    This avoids reopening a mutable parent path after an attempt has bound the
    panel directory.
    """
    manifest_path = Path("<bound-daily-panel>/manifest.json")
    manifest_bytes = _read_regular_file_at(root_fd, "manifest.json")
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid daily panel manifest: {manifest_path}") from error
    if not isinstance(manifest, dict):
        raise ValueError(f"daily panel manifest must be a JSON object: {manifest_path}")
    quality_record = manifest.get("quality_issues")
    if not isinstance(quality_record, dict):
        raise ValueError("daily panel manifest must contain quality issues identity")
    expected_quality_keys = {
        "relative_path",
        "records",
        "quarantined_rows",
        "size_bytes",
        "sha256",
    }
    if set(quality_record) != expected_quality_keys:
        raise ValueError("invalid daily panel quality issues identity")
    if quality_record.get("relative_path") != "quality_issues.json":
        raise ValueError("invalid daily panel quality issues relative_path")
    quality_bytes = _read_regular_file_at(root_fd, "quality_issues.json")
    try:
        quality_payload = json.loads(quality_bytes)
    except json.JSONDecodeError as error:
        raise ValueError("invalid daily panel quality issues JSON") from error
    if not isinstance(quality_payload, list):
        raise ValueError("daily panel quality issues must contain a JSON list")
    records_count = quality_record.get("records")
    quarantined_rows = quality_record.get("quarantined_rows")
    if (
        not isinstance(records_count, int)
        or isinstance(records_count, bool)
        or records_count != len(quality_payload)
    ):
        raise ValueError("daily panel quality issues record count mismatch")
    actual_quarantined_rows = sum(
        int(record.get("count", 0))
        for record in quality_payload
        if isinstance(record, dict)
        and record.get("code") == "invalid_ohlc_quarantined"
        and isinstance(record.get("count", 0), int)
        and not isinstance(record.get("count", 0), bool)
    )
    if (
        not isinstance(quarantined_rows, int)
        or isinstance(quarantined_rows, bool)
        or quarantined_rows < 0
        or quarantined_rows != actual_quarantined_rows
    ):
        raise ValueError("daily panel quality issues quarantined row count mismatch")
    quality_summary = _bytes_summary(quality_bytes)
    if quality_record.get("sha256") != quality_summary["sha256"]:
        raise ValueError("daily panel quality issues digest mismatch")
    if quality_record.get("size_bytes") != quality_summary["size_bytes"]:
        raise ValueError("daily panel quality issues size mismatch")

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

    records: list[tuple[dict[str, object], str, date, date]] = []
    expected_relative_paths: set[str] = set()
    for item in partitions:
        if not isinstance(item, dict):
            raise ValueError("invalid daily panel partition summary")
        relative_path, relative = _bound_partition_relative(item.get("relative_path"))
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
        if partition_minimum < allowed.start or partition_maximum > allowed.end:
            raise ValueError(f"daily panel partition dates outside allowed period: {relative_path}")
        if partition_minimum.year != year or partition_maximum.year != year:
            raise ValueError(f"daily panel partition year/date mismatch: {relative_path}")
        if relative_path in expected_relative_paths:
            raise ValueError(f"duplicate daily panel partition path: {relative_path}")
        expected_relative_paths.add(relative_path)
        records.append((item, relative_path, partition_minimum, partition_maximum))

    actual_relative_paths = _bound_parquet_paths(root_fd)
    if actual_relative_paths != expected_relative_paths:
        missing = sorted(expected_relative_paths - actual_relative_paths)
        extra = sorted(actual_relative_paths - expected_relative_paths)
        raise ValueError(
            f"daily panel partition file list mismatch: missing={missing}, extra={extra}"
        )
    for record, relative_path, partition_minimum, partition_maximum in records:
        descriptor = _open_bound_partition(root_fd, relative_path)
        try:
            actual_file = _descriptor_summary(descriptor)
            if record["sha256"] != actual_file["sha256"]:
                raise ValueError(
                    "daily panel partition digest mismatch: " f"{relative_path}"
                )
            if record["size_bytes"] != actual_file["size_bytes"]:
                raise ValueError(
                    "daily panel partition size mismatch: " f"{relative_path}"
                )
            actual_rows, actual_minimum, actual_maximum = _descriptor_parquet_summary(
                descriptor
            )
        finally:
            os.close(descriptor)
        if record["rows"] != actual_rows:
            raise ValueError(
                "daily panel partition rows do not match parquet: " f"{relative_path}"
            )
        if actual_minimum < allowed.start or actual_maximum > allowed.end:
            raise ValueError(
                "daily panel partition dates outside allowed period: " f"{relative_path}"
            )
        if actual_minimum != partition_minimum or actual_maximum != partition_maximum:
            raise ValueError(
                "daily panel partition dates do not match parquet: " f"{relative_path}"
            )
        year = int(record["year"])
        if actual_minimum.year != year or actual_maximum.year != year:
            raise ValueError(
                "daily panel partition year does not match parquet: " f"{relative_path}"
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
        files=tuple(Path(relative) for _, relative, _, _ in sorted(records, key=lambda item: item[1])),
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        manifest_size_bytes=len(manifest_bytes),
        quality_file=Path("quality_issues.json"),
        quality_record=dict(quality_record),
    )


def _read_regular_file_at(directory_fd: int, name: str) -> bytes:
    descriptor = _open_regular_file_at(directory_fd, name)
    try:
        return _read_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _open_regular_file_at(directory_fd: int, name: str) -> int:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | _no_follow_flag(),
            dir_fd=directory_fd,
        )
    except OSError as error:
        raise ValueError("daily panel file is missing, unsafe, or uses a symlink") from error
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise ValueError("daily panel file is not regular")
    return descriptor


def _read_descriptor(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    offset = 0
    size_bytes = os.fstat(descriptor).st_size
    while offset < size_bytes:
        chunk = os.pread(descriptor, min(1024 * 1024, size_bytes - offset), offset)
        if not chunk:
            raise ValueError("daily panel file changed while reading")
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def _bytes_summary(payload: bytes) -> dict[str, int | str]:
    return {"sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload)}


def _bound_partition_relative(relative_path: object) -> tuple[str, PurePosixPath]:
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
    return relative_path, relative


def _bound_parquet_paths(directory_fd: int, *, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    for name in os.listdir(directory_fd):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        relative = f"{prefix}/{name}" if prefix else name
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("daily panel contains a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | _no_follow_flag(),
                dir_fd=directory_fd,
            )
            try:
                paths.update(_bound_parquet_paths(child_fd, prefix=relative))
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(metadata.st_mode) and name.endswith(".parquet"):
            paths.add(relative)
    return paths


def _open_bound_partition(root_fd: int, relative_path: str) -> int:
    _, relative = _bound_partition_relative(relative_path)
    parent_fd = os.open(
        relative.parent.name,
        os.O_RDONLY | os.O_DIRECTORY | _no_follow_flag(),
        dir_fd=root_fd,
    )
    try:
        return _open_regular_file_at(parent_fd, relative.name)
    finally:
        os.close(parent_fd)


def _descriptor_summary(descriptor: int) -> dict[str, int | str]:
    size_bytes = os.fstat(descriptor).st_size
    digest = hashlib.sha256()
    offset = 0
    while offset < size_bytes:
        chunk = os.pread(descriptor, min(1024 * 1024, size_bytes - offset), offset)
        if not chunk:
            raise ValueError("daily panel partition changed during hashing")
        digest.update(chunk)
        offset += len(chunk)
    return {"sha256": digest.hexdigest(), "size_bytes": size_bytes}


def _no_follow_flag() -> int:
    flag = getattr(os, "O_NOFOLLOW", None)
    if flag is None:
        raise RuntimeError("secure no-follow file open is unavailable")
    return flag


def _descriptor_parquet_summary(descriptor: int) -> tuple[int, date, date]:
    try:
        summary = (
            pl.scan_parquet(f"/dev/fd/{descriptor}")
            .select(
                pl.len().alias("rows"),
                pl.col("date").min().alias("min_date"),
                pl.col("date").max().alias("max_date"),
            )
            .collect()
            .row(0, named=True)
        )
    except (OSError, pl.exceptions.PolarsError) as error:
        raise ValueError("invalid daily panel parquet partition") from error
    minimum = summary["min_date"]
    maximum = summary["max_date"]
    if not isinstance(minimum, date) or not isinstance(maximum, date):
        raise ValueError("invalid date column in daily panel partition")
    return int(summary["rows"]), minimum, maximum
