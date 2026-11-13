"""CSI 300 and CSI 500 price-index benchmarks from BaoStock (descriptive supplement).

The universe benchmarks in the report are built from the project's own data; this
module adds the two public indices readers expect. BaoStock serves index K-lines
from 2006-01-01, so the comparison starts at the last close of 2005, taken from
the ``preclose`` of the first 2006 row. The series are *price* indices: they omit
dividends, which the strategy NAV includes, so relative returns are flattering to
the strategy by roughly the index dividend yield.

Access rules read on 2026-11-13 at https://www.baostock.com/blacklist : at most
50,000 API requests per day and no concurrent connections. One download is a single
login, one sequential query per index and one logout, on one connection; nothing
is fetched after 2021-12-31, so the sealed final-test period is never requested.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from ashare_multifactor.audit.records import sha256_file
from ashare_multifactor.protocol_dates import VALIDATION_END

INDEX_CODES = {"csi300": "sh.000300", "csi500": "sh.000905"}
INDEX_START = date(2006, 1, 1)
INDEX_END = VALIDATION_END
CACHE_PATH = Path("artifacts/v1_supplements_source_cache/baostock_index/index_daily.parquet")
_FIELDS = "date,code,close,preclose"
_SOURCE = "baostock_query_history_k_data_plus"
# BaoStock stores close and preclose at three or four decimals, so a chain may be
# off by one unit in the third decimal (largest gap seen: 0.001 index points).
_CHAIN_TOLERANCE = 0.002

Query = Callable[[str, str, str], tuple[list[str], list[list[str]]]]


def collect_index_rows(query: Query) -> pl.DataFrame:
    """Run one bounded query per index, in order, and validate what comes back."""
    frames = []
    for name, code in INDEX_CODES.items():
        fields, rows = query(code, INDEX_START.isoformat(), INDEX_END.isoformat())
        if not rows:
            raise ValueError(f"BaoStock returned no rows for {code}")
        raw = pl.DataFrame(rows, schema=list(fields), orient="row")
        frame = raw.select(
            pl.col("date").str.to_date("%Y-%m-%d"),
            pl.lit(name).alias("index"),
            pl.col("code"),
            pl.col("close").cast(pl.Float64),
            pl.col("preclose").cast(pl.Float64),
        ).sort("date")
        _validate(frame, code)
        frames.append(frame)
    return pl.concat(frames)


def _validate(frame: pl.DataFrame, code: str) -> None:
    if frame.get_column("code").unique().to_list() != [code]:
        raise ValueError(f"BaoStock returned rows for another security than {code}")
    if frame.get_column("date").max() > INDEX_END:
        raise ValueError(f"{code}: index rows cross the sealed final-test boundary")
    if frame.get_column("date").min() < INDEX_START:
        raise ValueError(f"{code}: index rows start before the requested range")
    if frame.get_column("date").is_duplicated().any():
        raise ValueError(f"{code}: duplicate index dates")
    levels = frame.select("close", "preclose")
    if not levels.select(pl.all_horizontal(pl.all().is_finite() & (pl.all() > 0)).all()).item():
        raise ValueError(f"{code}: index levels must be positive and finite")
    broken = frame.filter((pl.col("preclose") - pl.col("close").shift(1)).abs() > _CHAIN_TOLERANCE)
    if broken.height:
        raise ValueError(
            f"{code}: {broken.height} rows whose previous close differs from the prior close"
        )


def index_levels(frame: pl.DataFrame, *, trading_dates: Sequence[date]) -> pl.DataFrame:
    """Wide index levels on the project's trading calendar, led by the prior close.

    ``trading_dates`` must be the panel's trading days from the last one before the
    first index row through the last index row; any difference from the dates
    BaoStock returned is an error rather than something to fill.
    """
    calendar = sorted(trading_dates)
    wide: pl.DataFrame | None = None
    for name in INDEX_CODES:
        series = frame.filter(pl.col("index") == name).sort("date")
        dates = series.get_column("date").to_list()
        if len(calendar) < 2 or calendar[1:] != dates or calendar[0] >= dates[0]:
            raise ValueError(f"{name}: index dates differ from the project trading calendar")
        column = pl.DataFrame(
            {
                "date": calendar,
                name: [series.item(0, "preclose"), *series.get_column("close").to_list()],
            },
            schema={"date": pl.Date, name: pl.Float64},
        )
        wide = column if wide is None else wide.join(column, on="date", validate="1:1")
    assert wide is not None
    return wide


def write_index_cache(
    frame: pl.DataFrame, output: Path, *, fetched_at: str, requests: int
) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(output)
    metadata = {
        "source": _SOURCE,
        "access_rules_url": "https://www.baostock.com/blacklist",
        "access_rules": "at most 50,000 API requests per day; no concurrent connections",
        "codes": INDEX_CODES,
        "fields": _FIELDS,
        "frequency": "d",
        "query_start": INDEX_START.isoformat(),
        "query_end": INDEX_END.isoformat(),
        "fetched_at": fetched_at,
        "requests_including_login_and_logout": requests,
        "rows": frame.height,
        "min_date": frame.get_column("date").min().isoformat(),
        "max_date": frame.get_column("date").max().isoformat(),
        "sha256": sha256_file(output),
    }
    output.with_suffix(".metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return metadata


def read_index_cache(path: Path) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Load the cache only if its metadata still describes these exact bytes."""
    sidecar = path.with_suffix(".metadata.json")
    if not path.is_file() or not sidecar.is_file():
        raise FileNotFoundError(
            f"index cache not found at {path}; run "
            "`python -m ashare_multifactor.cli.supplements --download-index-benchmarks` once"
        )
    metadata = json.loads(sidecar.read_text(encoding="utf-8"))
    frame = pl.read_parquet(path)
    if (
        metadata.get("source") != _SOURCE
        or metadata.get("codes") != INDEX_CODES
        or metadata.get("query_end") != INDEX_END.isoformat()
        or metadata.get("sha256") != sha256_file(path)
        or metadata.get("rows") != frame.height
        or frame.get_column("date").max() > INDEX_END
    ):
        raise ValueError("index cache does not match its metadata or the frozen query scope")
    return frame, metadata


def download_index_cache(output: Path) -> dict[str, Any]:  # pragma: no cover - network
    """Fetch both indices once, sequentially, on a single BaoStock connection."""
    import baostock as bs

    requests = 1
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login.error_code} {login.error_msg}")

    def query(code: str, start: str, end: str) -> tuple[list[str], list[list[str]]]:
        nonlocal requests
        result = bs.query_history_k_data_plus(
            code, _FIELDS, start_date=start, end_date=end, frequency="d"
        )
        requests += 1
        if result.error_code != "0":
            raise RuntimeError(f"BaoStock index query failed: {code} {result.error_msg}")
        rows: list[list[str]] = []
        while result.next():
            rows.append(result.get_row_data())
        # The client pages 2,000 rows at a time; count the follow-up page requests too.
        requests += max(0, (len(rows) - 1) // 2000)
        return list(result.fields), rows

    try:
        frame = collect_index_rows(query)
    finally:
        bs.logout()
        requests += 1
    return write_index_cache(
        frame, output, fetched_at=datetime.now(UTC).isoformat(timespec="seconds"), requests=requests
    )
