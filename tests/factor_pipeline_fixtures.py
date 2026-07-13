from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path

import polars as pl
import yaml


OBSERVATIONS = 420
SYMBOLS = 25
_RAW_COLUMNS = (
    "日期",
    "代码",
    "名称",
    "所属行业",
    "开盘价",
    "最高价",
    "最低价",
    "收盘价",
    "前收盘价",
    "成交量（股）",
    "成交额（元）",
    "换手率",
    "是否ST",
    "是否涨停",
    "总股本（股）",
    "流通股本（股）",
    "总市值（元）",
    "流通市值（元）",
    "滚动市盈率",
    "市净率",
    "滚动市销率",
    "上市时间",
    "退市时间",
    "是否融资融券",
)
_ADJ_COLUMNS = ("日期", "代码", "开盘价", "最高价", "最低价", "收盘价", "前收盘价")


def synthetic_panel() -> pl.DataFrame:
    days = _business_days(date(2004, 1, 2), OBSERVATIONS)
    rows: list[dict[str, object]] = []
    for observation, day in enumerate(days):
        for security in range(SYMBOLS):
            phase = observation * 0.071 + security * 0.23
            close = 20.0 + observation * 0.012 + security * 0.17 + math.sin(phase) * 0.35
            previous_phase = (observation - 1) * 0.071 + security * 0.23
            previous = (
                20.0 + (observation - 1) * 0.012 + security * 0.17 + math.sin(previous_phase) * 0.35
            )
            amount = 2_000_000.0 + security * 50_000.0 + (observation % 17) * 2_000.0
            rows.append(
                {
                    "date": day,
                    "symbol": f"{security + 1:06d}",
                    "industry": f"synthetic-{security % 5}",
                    "open_raw": close * 0.998,
                    "high_raw": close * 1.012,
                    "low_raw": close * 0.988,
                    "close_raw": close,
                    "close_adj": close,
                    "prev_close_adj": previous,
                    "volume": 100_000.0 + security * 1_000.0,
                    "amount": amount,
                    "turnover": 0.008 + ((security + observation) % 11) * 0.0004,
                    "is_st": False,
                    "total_market_cap": (
                        800_000_000.0 + security * security * 7_000_000.0 + observation * 120_000.0
                    ),
                    "pe_ttm": 8.0 + security * 0.31 + math.sin(phase) * 0.2,
                    "pb": 1.1 + security * 0.045 + math.cos(phase) * 0.04,
                    "ps_ttm": 1.7 + security * 0.052 + math.sin(phase * 0.7) * 0.05,
                }
            )
    return pl.DataFrame(rows).sort("date", "symbol")


def write_prepared_daily_panel(tmp_path: Path) -> tuple[Path, Path, pl.DataFrame]:
    frame = synthetic_panel()
    processed = tmp_path / "processed"
    root = processed / "factor_research" / "daily_panel"
    records: list[dict[str, object]] = []
    by_year = frame.with_columns(pl.col("date").dt.year().alias("_year"))
    for year_frame in by_year.partition_by("_year", maintain_order=True):
        year = year_frame.item(0, "date").year
        year_frame = year_frame.drop("_year")
        relative = f"year={year}/part-000.parquet"
        output = root / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        year_frame.write_parquet(output)
        records.append(
            {
                "relative_path": relative,
                "year": year,
                "rows": year_frame.height,
                "min_date": year_frame.get_column("date").min().isoformat(),
                "max_date": year_frame.get_column("date").max().isoformat(),
                "size_bytes": output.stat().st_size,
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            }
        )
    days = frame.get_column("date").unique().sort()
    manifest = {
        "schema_version": "synthetic-1",
        "file_pairs": len(days),
        "rows": frame.height,
        "min_date": days.min().isoformat(),
        "max_date": days.max().isoformat(),
        "years": sorted(frame.get_column("date").dt.year().unique().to_list()),
        "partitions": records,
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    config_path = _write_config(tmp_path, days.min(), days.max())
    return config_path, root, frame


def file_hashes(root: Path, relative_paths: tuple[str, ...]) -> dict[str, str]:
    return {
        relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
        for relative in relative_paths
    }


def write_raw_pair(config_path: Path, day: date) -> None:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw_root = Path(raw["paths"]["raw_unadjusted"])
    adj_root = Path(raw["paths"]["raw_backward_adjusted"])
    raw_root.mkdir(parents=True, exist_ok=True)
    adj_root.mkdir(parents=True, exist_ok=True)
    name = f"{day.isoformat()}_金玥数据.csv"
    raw_row = (
        day.isoformat(),
        "000001",
        "平安银行",
        "银行",
        "10.0",
        "10.5",
        "9.8",
        "10.2",
        "9.9",
        "1000",
        "10000",
        "1.5",
        "否",
        "是",
        "100000",
        "80000",
        "1020000",
        "816000",
        "8.0",
        "1.1",
        "2.0",
        "1991-04-03",
        "-",
        "是",
    )
    adj_row = (day.isoformat(), "000001", "20.0", "21.0", "19.6", "20.4", "19.8")
    _write_csv(raw_root / name, _RAW_COLUMNS, raw_row)
    _write_csv(adj_root / name, _ADJ_COLUMNS, adj_row)


def _write_config(tmp_path: Path, data_start: date, analysis_end: date) -> Path:
    raw = yaml.safe_load(Path("configs/research_protocol.yaml").read_text(encoding="utf-8"))
    raw["paths"] = {
        "raw_unadjusted": str(tmp_path / "raw"),
        "raw_backward_adjusted": str(tmp_path / "adj"),
        "processed": str(tmp_path / "processed"),
        "artifacts": str(tmp_path / "artifacts"),
    }
    raw["factor_research"].update(
        {
            "data_start": data_start.isoformat(),
            "analysis_start": "2005-01-01",
            "analysis_end": analysis_end.isoformat(),
            "universe_size": SYMBOLS,
            "minimum_history": 252,
            "liquidity_lookback": 20,
            "minimum_valid_months": 1,
        }
    )
    config_path = tmp_path / "research_protocol.yaml"
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    evidence = Path("configs/data_field_evidence.yaml").read_text(encoding="utf-8")
    config_path.with_name("data_field_evidence.yaml").write_text(evidence, encoding="utf-8")
    return config_path


def _business_days(start: date, count: int) -> list[date]:
    result = []
    current = start
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


def _write_csv(path: Path, columns: tuple[str, ...], row: tuple[str, ...]) -> None:
    path.write_text(",".join(columns) + "\n" + ",".join(row), encoding="utf-8")
