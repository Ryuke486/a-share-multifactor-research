from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from ashare_multifactor.config import Period
from ashare_multifactor.data.manifest import validate_panel_source


ALLOWED = Period(date(2003, 1, 1), date(2016, 12, 31))


def _write_source(root: Path, days: list[date] | None = None) -> dict[str, object]:
    days = days or [date(2005, 1, 4)]
    year = days[0].year
    relative_path = f"year={year}/part-000.parquet"
    partition = root / relative_path
    partition.parent.mkdir(parents=True)
    pl.DataFrame({"date": days, "symbol": ["000001"] * len(days)}).write_parquet(partition)
    record = {
        "relative_path": relative_path,
        "year": year,
        "rows": len(days),
        "min_date": min(days).isoformat(),
        "max_date": max(days).isoformat(),
        "size_bytes": partition.stat().st_size,
        "sha256": hashlib.sha256(partition.read_bytes()).hexdigest(),
    }
    manifest: dict[str, object] = {
        "schema_version": "1.0.0",
        "file_pairs": len(days),
        "rows": len(days),
        "min_date": min(days).isoformat(),
        "max_date": max(days).isoformat(),
        "years": [year],
        "partitions": [record],
    }
    _write_manifest(root, manifest)
    return manifest


def _write_manifest(root: Path, manifest: dict[str, object]) -> None:
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _partition_record(manifest: dict[str, object]) -> dict[str, object]:
    partitions = manifest["partitions"]
    assert isinstance(partitions, list)
    record = partitions[0]
    assert isinstance(record, dict)
    return record


def _refresh_partition_digest(root: Path, record: dict[str, object]) -> None:
    path = root / str(record["relative_path"])
    record["size_bytes"] = path.stat().st_size
    record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()


def _write_year_source(root: Path, years: tuple[int, ...]) -> dict[str, object]:
    records: list[dict[str, object]] = []
    for year in years:
        day = date(year, 1, 2)
        relative_path = f"year={year}/part-000.parquet"
        partition = root / relative_path
        partition.parent.mkdir(parents=True)
        pl.DataFrame({"date": [day], "symbol": ["000001"]}).write_parquet(partition)
        records.append(
            {
                "relative_path": relative_path,
                "year": year,
                "rows": 1,
                "min_date": day.isoformat(),
                "max_date": day.isoformat(),
                "size_bytes": partition.stat().st_size,
                "sha256": hashlib.sha256(partition.read_bytes()).hexdigest(),
            }
        )
    manifest: dict[str, object] = {
        "schema_version": "1.0.0",
        "file_pairs": len(years),
        "rows": len(years),
        "min_date": records[0]["min_date"],
        "max_date": records[-1]["max_date"],
        "years": list(years),
        "partitions": records,
    }
    _write_manifest(root, manifest)
    return manifest


def test_validate_panel_source_returns_audited_partition_paths(tmp_path: Path) -> None:
    root = tmp_path / "daily_panel"
    manifest = _write_source(root)

    source = validate_panel_source(root, ALLOWED)

    assert source.manifest == manifest
    assert source.files == (root / "year=2005/part-000.parquet",)


def test_validate_panel_source_rejects_missing_partition_summary(tmp_path: Path) -> None:
    root = tmp_path / "daily_panel"
    manifest = _write_source(root)
    manifest["partitions"] = []
    _write_manifest(root, manifest)

    with pytest.raises(ValueError, match="must contain partition summaries"):
        validate_panel_source(root, ALLOWED)


def test_validate_panel_source_rejects_missing_partition_file(tmp_path: Path) -> None:
    root = tmp_path / "daily_panel"
    _write_source(root)
    (root / "year=2005/part-000.parquet").unlink()

    with pytest.raises(ValueError, match="partition file list mismatch"):
        validate_panel_source(root, ALLOWED)


def test_validate_panel_source_rejects_unlisted_extra_partition(tmp_path: Path) -> None:
    root = tmp_path / "daily_panel"
    _write_source(root)
    extra = root / "year=2005/part-001.parquet"
    pl.DataFrame({"date": [date(2005, 1, 5)], "symbol": ["000002"]}).write_parquet(extra)

    with pytest.raises(ValueError, match="partition file list mismatch"):
        validate_panel_source(root, ALLOWED)


def test_validate_panel_source_rejects_path_traversal(tmp_path: Path) -> None:
    root = tmp_path / "daily_panel"
    manifest = _write_source(root)
    _partition_record(manifest)["relative_path"] = "../outside.parquet"
    _write_manifest(root, manifest)

    with pytest.raises(ValueError, match="invalid daily panel partition path"):
        validate_panel_source(root, ALLOWED)


def test_validate_panel_source_rejects_absolute_partition_path(tmp_path: Path) -> None:
    root = tmp_path / "daily_panel"
    manifest = _write_source(root)
    _partition_record(manifest)["relative_path"] = str(
        (tmp_path / "outside.parquet").resolve()
    )
    _write_manifest(root, manifest)

    with pytest.raises(ValueError, match="invalid daily panel partition path"):
        validate_panel_source(root, ALLOWED)


def test_validate_panel_source_rejects_duplicate_partition_declaration(
    tmp_path: Path,
) -> None:
    root = tmp_path / "daily_panel"
    manifest = _write_source(root)
    partitions = manifest["partitions"]
    assert isinstance(partitions, list)
    partitions.append(dict(_partition_record(manifest)))
    manifest["rows"] = 2
    _write_manifest(root, manifest)

    with pytest.raises(ValueError, match="duplicate daily panel partition path"):
        validate_panel_source(root, ALLOWED)


def test_validate_panel_source_rejects_symlink_partition_escape(tmp_path: Path) -> None:
    root = tmp_path / "daily_panel"
    manifest = _write_source(root)
    partition = root / str(_partition_record(manifest)["relative_path"])
    outside = tmp_path / "outside.parquet"
    partition.replace(outside)
    partition.symlink_to(outside)

    with pytest.raises(ValueError, match="invalid daily panel partition path"):
        validate_panel_source(root, ALLOWED)


def test_validate_panel_source_rejects_partition_year_path_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "daily_panel"
    manifest = _write_source(root)
    _partition_record(manifest)["year"] = 2004
    _write_manifest(root, manifest)

    with pytest.raises(ValueError, match="partition year/path mismatch"):
        validate_panel_source(root, ALLOWED)


def test_validate_panel_source_rejects_declared_rows_that_disagree_with_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / "daily_panel"
    manifest = _write_source(root)
    _partition_record(manifest)["rows"] = 2
    manifest["rows"] = 2
    _write_manifest(root, manifest)

    with pytest.raises(ValueError, match="partition rows do not match parquet"):
        validate_panel_source(root, ALLOWED)


def test_validate_panel_source_rejects_partition_size_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "daily_panel"
    manifest = _write_source(root)
    record = _partition_record(manifest)
    size = record["size_bytes"]
    assert isinstance(size, int)
    record["size_bytes"] = size + 1
    _write_manifest(root, manifest)

    with pytest.raises(ValueError, match="partition size mismatch"):
        validate_panel_source(root, ALLOWED)


def test_validate_panel_source_rejects_partition_hash_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "daily_panel"
    manifest = _write_source(root)
    _partition_record(manifest)["sha256"] = "0" * 64
    _write_manifest(root, manifest)

    with pytest.raises(ValueError, match="partition digest mismatch"):
        validate_panel_source(root, ALLOWED)


def test_validate_panel_source_rejects_manifest_dates_outside_allowed_period(
    tmp_path: Path,
) -> None:
    root = tmp_path / "daily_panel"
    manifest = _write_source(root)
    manifest["min_date"] = "2002-12-31"
    _write_manifest(root, manifest)

    with pytest.raises(ValueError, match="manifest min_date predates allowed period"):
        validate_panel_source(root, ALLOWED)


def test_validate_panel_source_rejects_concealed_2017_parquet_date(tmp_path: Path) -> None:
    root = tmp_path / "daily_panel"
    manifest = _write_source(root, [date(2016, 12, 30)])
    record = _partition_record(manifest)
    partition = root / str(record["relative_path"])
    pl.DataFrame({"date": [date(2017, 1, 3)], "symbol": ["000001"]}).write_parquet(partition)
    _refresh_partition_digest(root, record)
    _write_manifest(root, manifest)

    with pytest.raises(ValueError, match="partition dates outside allowed period"):
        validate_panel_source(root, ALLOWED)


def test_validate_panel_source_rejects_two_declared_partitions_for_one_year(
    tmp_path: Path,
) -> None:
    root = tmp_path / "daily_panel"
    manifest = _write_source(root)
    extra = root / "year=2005/part-001.parquet"
    pl.DataFrame({"date": [date(2005, 1, 5)], "symbol": ["000002"]}).write_parquet(extra)
    partitions = manifest["partitions"]
    assert isinstance(partitions, list)
    partitions.append(
        {
            "relative_path": "year=2005/part-001.parquet",
            "year": 2005,
            "rows": 1,
            "min_date": "2005-01-05",
            "max_date": "2005-01-05",
            "size_bytes": extra.stat().st_size,
            "sha256": hashlib.sha256(extra.read_bytes()).hexdigest(),
        }
    )
    manifest["rows"] = 2
    manifest["max_date"] = "2005-01-05"
    _write_manifest(root, manifest)

    with pytest.raises(ValueError, match="exactly one part-000 partition per year"):
        validate_panel_source(root, ALLOWED)


def test_validate_panel_source_rejects_self_consistent_missing_middle_year(
    tmp_path: Path,
) -> None:
    root = tmp_path / "daily_panel"
    _write_year_source(root, (2003, 2005))

    with pytest.raises(ValueError, match="continuous min/max year range"):
        validate_panel_source(root, ALLOWED)
