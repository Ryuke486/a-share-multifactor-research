from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from uuid import uuid4

import polars as pl

from ashare_multifactor.config import ResearchConfig, load_config
from ashare_multifactor.data.discovery import discover_daily_pairs
from ashare_multifactor.data.reader import read_daily_pair
from ashare_multifactor.data.schema import SCHEMA_VERSION
from ashare_multifactor.data.validation import raise_on_errors, validate_daily_panel


@dataclass(frozen=True)
class PartitionManifest:
    relative_path: str
    year: int
    rows: int
    min_date: date
    max_date: date
    size_bytes: int
    sha256: str

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["min_date"] = self.min_date.isoformat()
        payload["max_date"] = self.max_date.isoformat()
        return payload


@dataclass(frozen=True)
class BuildManifest:
    schema_version: str
    file_pairs: int
    rows: int
    min_date: date
    max_date: date
    years: tuple[int, ...]
    partitions: tuple[PartitionManifest, ...]

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["min_date"] = self.min_date.isoformat()
        payload["max_date"] = self.max_date.isoformat()
        payload["years"] = list(self.years)
        payload["partitions"] = [partition.to_dict() for partition in self.partitions]
        return payload


def _write_json(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_year(root: Path, year: int, frames: list[pl.DataFrame]) -> PartitionManifest:
    frame = pl.concat(frames).sort(["date", "symbol"])
    partition = root / f"year={year}"
    partition.mkdir(parents=True)
    temporary = partition / "part-000.parquet.tmp"
    frame.write_parquet(temporary)
    output = partition / "part-000.parquet"
    os.replace(temporary, output)
    return PartitionManifest(
        relative_path=output.relative_to(root).as_posix(),
        year=year,
        rows=frame.height,
        min_date=frame.get_column("date").min(),
        max_date=frame.get_column("date").max(),
        size_bytes=output.stat().st_size,
        sha256=_file_sha256(output),
    )


def _publish_staging(staging: Path, target: Path) -> None:
    if not target.exists():
        os.replace(staging, target)
        return

    backup = staging.with_suffix(".backup")
    os.replace(target, backup)
    try:
        os.replace(staging, target)
    except BaseException:
        os.replace(backup, target)
        raise
    shutil.rmtree(backup)


def build_parquet_dataset(
    config: ResearchConfig,
    start: date,
    end: date,
    output_root: Path | None = None,
) -> BuildManifest:
    if end < start:
        raise ValueError("build end precedes start")
    if output_root is None:
        allowed_start = config.smoke_data.start
        allowed_end = config.smoke_data.end
        target = config.paths.processed / "daily_panel"
        period_name = "smoke_data"
    else:
        if config.factor_research is None:
            raise ValueError("explicit output_root requires factor_research settings")
        allowed_start = config.factor_research.data_start
        allowed_end = config.factor_research.analysis_end
        target = output_root
        period_name = "factor_research data"
    if start < allowed_start or end > allowed_end:
        raise ValueError(f"build dates must stay inside configured {period_name} period")
    pairs = discover_daily_pairs(
        config.paths.raw_unadjusted,
        config.paths.raw_backward_adjusted,
        start,
        end,
    )
    if not pairs:
        raise ValueError("no paired daily files found")

    staging = target.parent / f".{target.name}-{uuid4().hex}.tmp"
    quality_records: list[dict[str, object]] = []
    rows = 0
    partitions: list[PartitionManifest] = []
    try:
        staging.mkdir(parents=True)
        current_year: int | None = None
        year_frames: list[pl.DataFrame] = []
        for pair in pairs:
            if current_year is not None and pair.trading_date.year != current_year:
                partition = _write_year(staging, current_year, year_frames)
                partitions.append(partition)
                rows += partition.rows
                year_frames = []
            current_year = pair.trading_date.year
            frame = read_daily_pair(pair)
            issues = validate_daily_panel(frame, pair.trading_date)
            raise_on_errors(issues)
            quality_records.extend(
                {"date": pair.trading_date.isoformat(), **issue.to_dict()} for issue in issues
            )
            year_frames.append(frame)
        if current_year is not None:
            partition = _write_year(staging, current_year, year_frames)
            partitions.append(partition)
            rows += partition.rows

        manifest = BuildManifest(
            schema_version=SCHEMA_VERSION,
            file_pairs=len(pairs),
            rows=rows,
            min_date=pairs[0].trading_date,
            max_date=pairs[-1].trading_date,
            years=tuple(sorted({pair.trading_date.year for pair in pairs})),
            partitions=tuple(partitions),
        )
        _write_json(staging / "manifest.json", manifest.to_dict())
        _write_json(staging / "quality_issues.json", quality_records)
        _publish_staging(staging, target)
        return manifest
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--mode", choices=("smoke",), required=True)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    build_parquet_dataset(config, config.smoke_data.start, config.smoke_data.end)


if __name__ == "__main__":
    main()
