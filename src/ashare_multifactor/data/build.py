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
from typing import Protocol

import polars as pl

from ashare_multifactor.config import ResearchConfig, load_config
from ashare_multifactor.data.discovery import DailyFilePair, discover_daily_pairs
from ashare_multifactor.data.reader import read_daily_pair
from ashare_multifactor.data.schema import SCHEMA_VERSION
from ashare_multifactor.data.security import filter_supported_markets
from ashare_multifactor.data.validation import (
    invalid_ohlc_expression,
    raise_on_errors,
    validate_daily_panel,
)


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
class QualityIssuesManifest:
    relative_path: str
    records: int
    quarantined_rows: int
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class BuildManifest:
    schema_version: str
    file_pairs: int
    rows: int
    min_date: date
    max_date: date
    years: tuple[int, ...]
    partitions: tuple[PartitionManifest, ...]
    quality_issues: QualityIssuesManifest

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["min_date"] = self.min_date.isoformat()
        payload["max_date"] = self.max_date.isoformat()
        payload["years"] = list(self.years)
        payload["partitions"] = [partition.to_dict() for partition in self.partitions]
        return payload


class SecureBuildOutput(Protocol):
    def write_year(
        self, year: int, frames: list[pl.DataFrame]
    ) -> PartitionManifest: ...

    def write_json(self, name: str, payload: object) -> bytes: ...

    def publish(self) -> None: ...


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
    except BaseException as publish_error:
        try:
            os.replace(backup, target)
        except BaseException as rollback_error:
            try:
                shutil.copytree(backup, target)
            except BaseException as recovery_error:
                error = RuntimeError(
                    "dataset publish failed; rollback rename failed; "
                    f"backup copy recovery failed; backup retained at {backup}"
                )
                error.add_note(f"publish error: {publish_error!r}")
                error.add_note(f"rollback rename error: {rollback_error!r}")
                error.add_note(f"backup copy error: {recovery_error!r}")
                raise error from publish_error
            error = RuntimeError(
                "dataset publish failed; rollback rename failed; "
                f"restored target from backup copy; backup retained at {backup}"
            )
            error.add_note(f"publish error: {publish_error!r}")
            error.add_note(f"rollback rename error: {rollback_error!r}")
            raise error from publish_error
        raise
    try:
        shutil.rmtree(backup)
    except OSError:
        pass


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _validated_output_root(
    config: ResearchConfig,
    output_root: Path | None,
) -> Path:
    processed = config.paths.processed.resolve()
    for raw_root in (
        config.paths.raw_unadjusted.resolve(),
        config.paths.raw_backward_adjusted.resolve(),
    ):
        if _paths_overlap(processed, raw_root):
            raise ValueError("configured processed path overlaps raw input path")

    if output_root is None:
        return config.paths.processed / "daily_panel"

    processed_logical = Path(os.path.abspath(config.paths.processed))
    requested_logical = Path(os.path.abspath(output_root))
    validation_logical = processed_logical / "validation_evaluation/daily_panel"
    if requested_logical == validation_logical:
        return config.paths.processed / "validation_evaluation/daily_panel"
    expected_logical = processed_logical / "factor_research/daily_panel"
    if requested_logical != expected_logical:
        raise ValueError(
            "output_root must equal configured factor research daily panel "
            "or validation daily panel"
        )

    factor_root = config.paths.processed / "factor_research"
    expected_factor_root = processed / "factor_research"
    resolved_factor_root = factor_root.resolve()
    if resolved_factor_root != expected_factor_root:
        if resolved_factor_root.is_relative_to(processed):
            raise ValueError("configured factor_research path uses a symlink alias")
        raise ValueError(
            "configured factor research daily panel escapes configured processed root"
        )

    target = factor_root / "daily_panel"
    expected_target = expected_factor_root / "daily_panel"
    resolved_target = target.resolve()
    if resolved_target != expected_target:
        if resolved_target.is_relative_to(processed):
            raise ValueError("configured daily_panel path uses a symlink alias")
        raise ValueError(
            "configured factor research daily panel escapes configured processed root"
        )
    return target


def build_parquet_dataset(
    config: ResearchConfig,
    start: date,
    end: date,
    output_root: Path | None = None,
    *,
    discovered_pairs: list[DailyFilePair] | None = None,
    supported_markets: tuple[str, ...] | None = None,
    secure_output: SecureBuildOutput | None = None,
) -> BuildManifest:
    if end < start:
        raise ValueError("build end precedes start")
    quarantine_invalid_ohlc = output_root is not None
    target = _validated_output_root(config, output_root)
    if output_root is None:
        allowed_start = config.smoke_data.start
        allowed_end = config.smoke_data.end
        period_name = "smoke_data"
    elif target == config.paths.processed / "validation_evaluation/daily_panel":
        allowed_start = config.validation.start
        allowed_end = config.validation.end
        period_name = "validation"
    else:
        if config.factor_research is None:
            raise ValueError("explicit output_root requires factor_research settings")
        allowed_start = config.factor_research.data_start
        allowed_end = config.factor_research.analysis_end
        period_name = "factor_research data"
    if start < allowed_start or end > allowed_end:
        raise ValueError(f"build dates must stay inside configured {period_name} period")
    pairs = (
        list(discovered_pairs)
        if discovered_pairs is not None
        else discover_daily_pairs(
            config.paths.raw_unadjusted,
            config.paths.raw_backward_adjusted,
            start,
            end,
        )
    )
    if not pairs:
        raise ValueError("no paired daily files found")
    if pairs != sorted(pairs, key=lambda pair: pair.trading_date) or any(
        pair.trading_date < start or pair.trading_date > end for pair in pairs
    ):
        raise ValueError("discovered daily pairs differ from requested period")

    staging_name = f".{target.name}-{uuid4().hex}.tmp"
    staging = target.parent / staging_name
    quality_records: list[dict[str, object]] = []
    rows = 0
    partitions: list[PartitionManifest] = []
    try:
        if secure_output is None:
            staging.mkdir(parents=True)
        current_year: int | None = None
        year_frames: list[pl.DataFrame] = []
        for pair in pairs:
            if current_year is not None and pair.trading_date.year != current_year:
                partition = (
                    _write_year(staging, current_year, year_frames)
                    if secure_output is None
                    else secure_output.write_year(current_year, year_frames)
                )
                partitions.append(partition)
                rows += partition.rows
                year_frames = []
            current_year = pair.trading_date.year
            frame = read_daily_pair(pair)
            if supported_markets is not None:
                filtered = filter_supported_markets(frame, supported_markets)
                excluded_rows = frame.height - filtered.height
                if excluded_rows:
                    quality_records.append(
                        {
                            "date": pair.trading_date.isoformat(),
                            "severity": "warning",
                            "code": "outside_supported_markets_excluded",
                            "count": excluded_rows,
                            "message": (
                                "rows outside the configured supported market scope "
                                "were excluded"
                            ),
                            "supported_markets": list(supported_markets),
                        }
                    )
                frame = filtered
                if frame.is_empty():
                    raise ValueError(
                        f"no rows remain inside supported markets for "
                        f"{pair.trading_date.isoformat()}"
                    )
            original_issues = validate_daily_panel(frame, pair.trading_date)
            if quarantine_invalid_ohlc:
                raise_on_errors(
                    [issue for issue in original_issues if issue.code != "invalid_ohlc"]
                )
                invalid_rows = frame.filter(invalid_ohlc_expression())
                if invalid_rows.height:
                    quality_records.append(
                        {
                            "date": pair.trading_date.isoformat(),
                            "severity": "warning",
                            "code": "invalid_ohlc_quarantined",
                            "count": invalid_rows.height,
                            "message": (
                                "rows with non-positive or inconsistent raw/adjusted OHLC "
                                "were quarantined"
                            ),
                            "symbols": sorted(invalid_rows["symbol"].unique().to_list()),
                        }
                    )
                    frame = frame.filter(~invalid_ohlc_expression())
                if frame.is_empty():
                    raise ValueError(
                        f"all rows quarantined for {pair.trading_date.isoformat()}"
                    )
            issues = validate_daily_panel(frame, pair.trading_date)
            raise_on_errors(issues)
            quality_records.extend(
                {"date": pair.trading_date.isoformat(), **issue.to_dict()} for issue in issues
            )
            year_frames.append(frame)
        if current_year is not None:
            partition = (
                _write_year(staging, current_year, year_frames)
                if secure_output is None
                else secure_output.write_year(current_year, year_frames)
            )
            partitions.append(partition)
            rows += partition.rows

        if secure_output is None:
            _write_json(staging / "quality_issues.json", quality_records)
            quality_bytes = (staging / "quality_issues.json").read_bytes()
        else:
            quality_bytes = secure_output.write_json(
                "quality_issues.json", quality_records
            )
        quality_path = staging / "quality_issues.json"
        quality_manifest = QualityIssuesManifest(
            relative_path="quality_issues.json",
            records=len(quality_records),
            quarantined_rows=sum(
                int(record["count"])
                for record in quality_records
                if record.get("code") == "invalid_ohlc_quarantined"
            ),
            size_bytes=(
                quality_path.stat().st_size
                if secure_output is None
                else len(quality_bytes)
            ),
            sha256=(
                _file_sha256(quality_path)
                if secure_output is None
                else hashlib.sha256(quality_bytes).hexdigest()
            ),
        )
        manifest = BuildManifest(
            schema_version=SCHEMA_VERSION,
            file_pairs=len(pairs),
            rows=rows,
            min_date=pairs[0].trading_date,
            max_date=pairs[-1].trading_date,
            years=tuple(sorted({pair.trading_date.year for pair in pairs})),
            partitions=tuple(partitions),
            quality_issues=quality_manifest,
        )
        if secure_output is None:
            _write_json(staging / "manifest.json", manifest.to_dict())
            _publish_staging(staging, target)
        else:
            secure_output.write_json("manifest.json", manifest.to_dict())
            secure_output.publish()
        return manifest
    except BaseException:
        if secure_output is None:
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
