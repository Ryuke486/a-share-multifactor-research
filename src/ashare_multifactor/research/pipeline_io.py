"""Atomic serialization and bounded frame reads for the MVP pipeline."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import polars as pl

from ashare_multifactor.config import ResearchConfig


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def read_json_value(path: Path) -> object:
    if not path.exists():
        raise FileNotFoundError(f"required intermediate result not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def read_json(path: Path) -> dict[str, object]:
    payload = read_json_value(path)
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def write_frame(frame: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if path.suffix == ".parquet":
        frame.write_parquet(temporary)
    elif path.suffix == ".csv":
        frame.write_csv(temporary)
    else:
        raise ValueError(f"unsupported frame output: {path}")
    os.replace(temporary, path)


def date_range(frame: pl.DataFrame) -> tuple[str | None, str | None]:
    values: list[date] = []
    for column in frame.columns:
        if column == "date" or column.endswith("_date"):
            values.extend(frame.get_column(column).drop_nulls().to_list())
    if not values:
        return None, None
    return min(values).isoformat(), max(values).isoformat()


def frame_stats(frame: pl.DataFrame) -> dict[str, int | str | None]:
    minimum, maximum = date_range(frame)
    return {"rows": frame.height, "min_date": minimum, "max_date": maximum}


def manual_stats(
    rows: int | None,
    minimum: date | None = None,
    maximum: date | None = None,
) -> dict[str, int | str | None]:
    return {
        "rows": rows,
        "min_date": minimum.isoformat() if minimum is not None else None,
        "max_date": maximum.isoformat() if maximum is not None else None,
    }


def read_daily_panel(
    config: ResearchConfig,
    files: list[Path],
    columns: list[str] | None = None,
) -> pl.DataFrame:
    if not files:
        raise FileNotFoundError(
            f"no daily panel partitions found under {config.paths.processed / 'daily_panel'}"
        )
    scan = pl.scan_parquet(files)
    if columns is not None:
        scan = scan.select(columns)
    frame = scan.filter(
        pl.col("date").is_between(
            config.smoke_data.start,
            config.smoke_data.end,
            closed="both",
        )
    ).collect()
    sort_columns = ["date"]
    if "symbol" in frame.columns:
        sort_columns.append("symbol")
    frame = frame.sort(sort_columns)
    if frame.is_empty():
        raise ValueError("daily panel is empty inside configured smoke_data period")
    return frame


def read_intermediate(
    path: Path,
    minimum_date: date,
    maximum_date: date,
) -> pl.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"required intermediate result not found: {path}")
    frame = pl.read_parquet(path)
    minimum, maximum = date_range(frame)
    if minimum is not None and date.fromisoformat(minimum) < minimum_date:
        raise ValueError(f"intermediate result predates configured smoke_analysis: {path}")
    if maximum is not None and date.fromisoformat(maximum) > maximum_date:
        raise ValueError(f"intermediate result exceeds configured smoke_analysis: {path}")
    return frame
