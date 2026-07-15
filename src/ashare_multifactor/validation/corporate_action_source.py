from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date
import json
from pathlib import Path

import polars as pl

from ashare_multifactor.audit.records import sha256_file
from ashare_multifactor.validation.corporate_actions import (
    VALIDATION_END,
    VALIDATION_YEARS,
    assert_baostock_query_scope,
)


QueryDividend = Callable[[str, int, str], tuple[list[str], list[list[str]]]]
_DATE_FIELDS = {
    "dividPreNoticeDate",
    "dividAgmPumDate",
    "dividPlanAnnounceDate",
    "dividPlanDate",
    "dividRegistDate",
    "dividOperateDate",
    "dividPayDate",
    "dividStockMarketDate",
}


def collect_baostock_rows(
    symbols: Iterable[str],
    query: QueryDividend,
) -> pl.DataFrame:
    records: list[dict[str, object]] = []
    known_fields: list[str] | None = None
    for symbol in sorted(set(symbols)):
        code = _provider_code(symbol)
        for year in VALIDATION_YEARS:
            assert_baostock_query_scope(year, "operate")
            fields, rows = query(code, year, "operate")
            if known_fields is None:
                known_fields = fields
            elif fields != known_fields:
                raise ValueError("BaoStock dividend response schema changed")
            for values in rows:
                if len(values) != len(fields):
                    raise ValueError("BaoStock dividend response row width changed")
                record = dict(zip(fields, values, strict=True))
                _assert_response_sealed(record)
                record["query_year"] = year
                record["query_year_type"] = "operate"
                records.append(record)
    fields = known_fields or []
    schema = {field: pl.String for field in fields}
    schema.update({"query_year": pl.Int64, "query_year_type": pl.String})
    return pl.DataFrame(records, schema=schema, strict=False)


def write_baostock_cache(
    symbols: Iterable[str],
    output: Path,
    *,
    query: QueryDividend,
) -> pl.DataFrame:
    unique_symbols = sorted(set(symbols))
    frame = collect_baostock_rows(unique_symbols, query)
    output.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(output)
    metadata = {
        "source": "baostock_query_dividend_data",
        "query_year_type": "operate",
        "query_years": list(VALIDATION_YEARS),
        "symbol_count": len(unique_symbols),
        "symbols": unique_symbols,
        "row_count": frame.height,
        "maximum_allowed_date": VALIDATION_END.isoformat(),
        "sha256": sha256_file(output),
    }
    output.with_suffix(".metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return frame


def merge_baostock_caches(
    inputs: Iterable[Path],
    output: Path,
    *,
    symbols: Iterable[str],
) -> pl.DataFrame:
    paths = sorted(inputs)
    expected = sorted(set(symbols))
    covered: list[str] = []
    frames = []
    for path in paths:
        metadata_path = path.with_suffix(".metadata.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if (
            metadata.get("query_year_type") != "operate"
            or metadata.get("query_years") != list(VALIDATION_YEARS)
            or metadata.get("sha256") != sha256_file(path)
        ):
            raise ValueError(f"invalid BaoStock cache metadata: {path}")
        covered.extend(metadata.get("symbols", []))
        frames.append(pl.read_parquet(path))
    if sorted(covered) != expected or len(covered) != len(set(covered)):
        raise ValueError("BaoStock cache symbol coverage mismatch")
    frame = pl.concat(frames, how="vertical_relaxed")
    output.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(output)
    metadata = {
        "source": "baostock_query_dividend_data",
        "query_year_type": "operate",
        "query_years": list(VALIDATION_YEARS),
        "symbol_count": len(expected),
        "symbols": expected,
        "row_count": frame.height,
        "maximum_allowed_date": VALIDATION_END.isoformat(),
        "sha256": sha256_file(output),
        "shards": [path.name for path in paths],
    }
    output.with_suffix(".metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return frame


def download_baostock_cache(symbols: Iterable[str], output: Path) -> pl.DataFrame:
    try:
        import baostock as bs
    except ImportError as exc:  # pragma: no cover - environment error
        raise RuntimeError("baostock is required to download dividend data") from exc
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login.error_msg}")

    def query(code: str, year: int, year_type: str) -> tuple[list[str], list[list[str]]]:
        result = bs.query_dividend_data(
            code=code,
            year=str(year),
            yearType=year_type,
        )
        if result.error_code != "0":
            raise RuntimeError(
                f"BaoStock dividend query failed: {code} {year} {result.error_msg}"
            )
        rows = []
        while result.next():
            rows.append(result.get_row_data())
        return list(result.fields), rows

    try:
        return write_baostock_cache(symbols, output, query=query)
    finally:
        bs.logout()


def load_official_payment_date_overrides(
    evidence_path: Path,
    cache_root: Path,
) -> pl.DataFrame:
    """Load narrowly scoped official payment-date evidence with hash checks."""
    evidence = pl.read_csv(
        evidence_path,
        schema_overrides={"symbol": pl.String},
        try_parse_dates=True,
    )
    required = {
        "symbol",
        "ex_date",
        "payment_date",
        "source_url",
        "cache_file",
        "sha256",
    }
    missing = sorted(required - set(evidence.columns))
    if missing:
        raise ValueError(f"official payment evidence missing fields: {missing}")
    if evidence.select(pl.struct("symbol", "ex_date").is_duplicated().any()).item():
        raise ValueError("duplicate official payment evidence key")
    if evidence.filter(
        (pl.col("ex_date") > VALIDATION_END)
        | (pl.col("payment_date") > VALIDATION_END)
    ).height:
        raise ValueError("sealed final test date in official payment evidence")
    for row in evidence.iter_rows(named=True):
        source_url = str(row["source_url"])
        if not source_url.startswith("https://static.cninfo.com.cn/finalpage/"):
            raise ValueError(f"invalid official payment evidence URL: {source_url}")
        cached = cache_root / row["cache_file"]
        if not cached.is_file() or sha256_file(cached) != row["sha256"]:
            raise ValueError(f"official payment evidence hash mismatch: {row['symbol']}")
    return evidence.select("symbol", "ex_date", "payment_date", "source_url")


def load_official_security_events(
    evidence_path: Path,
    cache_root: Path,
) -> pl.DataFrame:
    """Load validation-period merger and conservative write-off events."""
    events = pl.read_csv(
        evidence_path,
        schema_overrides={
            "source_symbol": pl.String,
            "target_symbol": pl.String,
        },
        try_parse_dates=True,
    )
    required = {
        "effective_date",
        "source_symbol",
        "event_type",
        "target_symbol",
        "ratio",
        "cash_per_share",
        "source_url",
        "cache_file",
        "sha256",
    }
    missing = sorted(required - set(events.columns))
    if missing:
        raise ValueError(f"official security evidence missing fields: {missing}")
    if events.select(
        pl.struct("effective_date", "source_symbol").is_duplicated().any()
    ).item():
        raise ValueError("duplicate official security event key")
    if events.filter(pl.col("effective_date") > VALIDATION_END).height:
        raise ValueError("sealed final test date in official security evidence")
    invalid = events.filter(
        (
            (pl.col("event_type") == "stock_merger")
            & (
                pl.col("target_symbol").is_null()
                | (pl.col("ratio") <= 0)
                | (pl.col("cash_per_share") != 0)
            )
        )
        | (
            (pl.col("event_type") == "write_off")
            & (
                pl.col("target_symbol").is_not_null()
                | (pl.col("ratio") != 0)
                | (pl.col("cash_per_share") != 0)
            )
        )
        | ~pl.col("event_type").is_in(["stock_merger", "write_off"])
    )
    if invalid.height:
        raise ValueError("invalid official security event contract")
    allowed_prefixes = (
        "https://static.cninfo.com.cn/finalpage/",
        "https://disc.static.szse.cn/download/disc/",
    )
    for row in events.iter_rows(named=True):
        source_url = str(row["source_url"])
        if not source_url.startswith(allowed_prefixes):
            raise ValueError(f"invalid official security evidence URL: {source_url}")
        cached = cache_root / row["cache_file"]
        if not cached.is_file() or sha256_file(cached) != row["sha256"]:
            raise ValueError(f"official security evidence hash mismatch: {row['source_symbol']}")
    return events.select(
        "effective_date",
        "source_symbol",
        "event_type",
        "target_symbol",
        "ratio",
        "cash_per_share",
        pl.col("source_url").alias("source"),
    )


def _provider_code(symbol: str) -> str:
    normalized = symbol.zfill(6)
    if normalized.startswith("6"):
        return f"sh.{normalized}"
    if normalized.startswith(("0", "3")):
        return f"sz.{normalized}"
    raise ValueError(f"unsupported validation security: {symbol}")


def _assert_response_sealed(record: dict[str, object]) -> None:
    for field in _DATE_FIELDS:
        value = str(record.get(field) or "").strip()
        if value and date.fromisoformat(value[:10]) > VALIDATION_END:
            raise ValueError(f"sealed final test date in BaoStock response: {field}")
