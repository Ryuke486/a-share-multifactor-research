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
    frame, _ = _collect_baostock_rows_with_coverage(symbols, query)
    return frame


def _collect_baostock_rows_with_coverage(
    symbols: Iterable[str],
    query: QueryDividend,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    records: list[dict[str, object]] = []
    coverage: list[dict[str, object]] = []
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
            coverage.append(
                {
                    "symbol": symbol.zfill(6),
                    "year": year,
                    "status": "ok",
                    "row_count": len(rows),
                }
            )
    fields = known_fields or []
    schema = {field: pl.String for field in fields}
    schema.update({"query_year": pl.Int64, "query_year_type": pl.String})
    return (
        pl.DataFrame(records, schema=schema, strict=False),
        pl.DataFrame(
            coverage,
            schema={
                "symbol": pl.String,
                "year": pl.Int64,
                "status": pl.String,
                "row_count": pl.Int64,
            },
        ),
    )


def write_baostock_cache(
    symbols: Iterable[str],
    output: Path,
    *,
    query: QueryDividend,
) -> pl.DataFrame:
    unique_symbols = sorted(set(symbols))
    frame, coverage = _collect_baostock_rows_with_coverage(unique_symbols, query)
    output.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(output)
    coverage_path = output.with_suffix(".coverage.parquet")
    coverage.write_parquet(coverage_path)
    metadata = {
        "source": "baostock_query_dividend_data",
        "query_year_type": "operate",
        "query_years": list(VALIDATION_YEARS),
        "symbol_count": len(unique_symbols),
        "symbols": unique_symbols,
        "row_count": frame.height,
        "maximum_allowed_date": VALIDATION_END.isoformat(),
        "sha256": sha256_file(output),
        "coverage_path": coverage_path.name,
        "coverage_sha256": sha256_file(coverage_path),
        "successful_query_count": coverage.height,
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
    coverages = []
    for path in paths:
        metadata_path = path.with_suffix(".metadata.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if (
            metadata.get("query_year_type") != "operate"
            or metadata.get("query_years") != list(VALIDATION_YEARS)
            or metadata.get("sha256") != sha256_file(path)
        ):
            raise ValueError(f"invalid BaoStock cache metadata: {path}")
        coverage_path = path.with_suffix(".coverage.parquet")
        if (
            metadata.get("coverage_path") != coverage_path.name
            or metadata.get("coverage_sha256") != sha256_file(coverage_path)
        ):
            raise ValueError(f"invalid BaoStock coverage metadata: {path}")
        covered.extend(metadata.get("symbols", []))
        frames.append(pl.read_parquet(path))
        coverages.append(pl.read_parquet(coverage_path))
    if sorted(covered) != expected or len(covered) != len(set(covered)):
        raise ValueError("BaoStock cache symbol coverage mismatch")
    frame = pl.concat(frames, how="vertical_relaxed")
    coverage = pl.concat(coverages, how="vertical_relaxed").sort("symbol", "year")
    if (
        coverage.height != len(expected) * len(VALIDATION_YEARS)
        or coverage.select(pl.struct("symbol", "year").is_duplicated().any()).item()
    ):
        raise ValueError("BaoStock query coverage mismatch")
    output.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(output)
    coverage_path = output.with_suffix(".coverage.parquet")
    coverage.write_parquet(coverage_path)
    metadata = {
        "source": "baostock_query_dividend_data",
        "query_year_type": "operate",
        "query_years": list(VALIDATION_YEARS),
        "symbol_count": len(expected),
        "symbols": expected,
        "row_count": frame.height,
        "maximum_allowed_date": VALIDATION_END.isoformat(),
        "sha256": sha256_file(output),
        "coverage_path": coverage_path.name,
        "coverage_sha256": sha256_file(coverage_path),
        "successful_query_count": coverage.height,
        "shards": [path.name for path in paths],
    }
    output.with_suffix(".metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return frame


def migrate_legacy_query_coverage(
    cache_path: Path,
    *,
    destination: Path | None = None,
) -> pl.DataFrame:
    """Derive explicit zero-response coverage without mutating a legacy cache."""
    metadata_path = cache_path.with_suffix(".metadata.json")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError("invalid legacy BaoStock cache metadata") from error
    symbols = sorted(str(value).zfill(6) for value in metadata.get("symbols", []))
    if (
        metadata.get("source") != "baostock_query_dividend_data"
        or metadata.get("query_year_type") != "operate"
        or metadata.get("query_years") != list(VALIDATION_YEARS)
        or metadata.get("symbol_count") != len(symbols)
        or len(symbols) != len(set(symbols))
        or metadata.get("sha256") != sha256_file(cache_path)
    ):
        raise ValueError("legacy BaoStock cache identity is incomplete")
    raw = pl.read_parquet(cache_path)
    required = {"code", "query_year", "query_year_type"}
    if not required.issubset(raw.columns) or raw.height != metadata.get("row_count"):
        raise ValueError("legacy BaoStock cache rows differ from metadata")
    observed = raw.select(
        pl.col("code").cast(pl.String).str.split(".").list.last().alias("symbol"),
        pl.col("query_year").cast(pl.Int64).alias("year"),
        pl.col("query_year_type").cast(pl.String),
    )
    if observed.filter(
        ~pl.col("symbol").is_in(symbols)
        | ~pl.col("year").is_in(list(VALIDATION_YEARS))
        | (pl.col("query_year_type") != "operate")
    ).height:
        raise ValueError("legacy BaoStock cache contains an out-of-scope query")
    counts = observed.group_by("symbol", "year").len().rename({"len": "row_count"})
    expected = pl.DataFrame(
        {
            "symbol": [symbol for symbol in symbols for _ in VALIDATION_YEARS],
            "year": list(VALIDATION_YEARS) * len(symbols),
        }
    )
    coverage = (
        expected.join(counts, on=["symbol", "year"], how="left", validate="1:1")
        .with_columns(
            pl.lit("ok").alias("status"),
            pl.col("row_count").fill_null(0).cast(pl.Int64),
        )
        .select("symbol", "year", "status", "row_count")
        .sort("symbol", "year")
    )
    if destination is not None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        coverage.write_parquet(destination)
    return coverage


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


def load_official_action_corrections(
    evidence_path: Path,
    cache_root: Path,
) -> pl.DataFrame:
    """Load narrow official corrections for erroneous vendor action dates."""
    evidence = pl.read_csv(
        evidence_path,
        schema_overrides={"symbol": pl.String},
        try_parse_dates=True,
    )
    required = {
        "symbol",
        "source_ex_date",
        "corrected_ex_date",
        "payment_date",
        "source_url",
        "cache_file",
        "sha256",
    }
    missing = sorted(required - set(evidence.columns))
    if missing:
        raise ValueError(f"official action correction missing fields: {missing}")
    key = ["symbol", "source_ex_date"]
    if evidence.select(pl.struct(*key).is_duplicated().any()).item():
        raise ValueError("duplicate official action correction key")
    invalid = evidence.filter(
        (pl.col("source_ex_date") > VALIDATION_END)
        | (pl.col("corrected_ex_date") > VALIDATION_END)
        | (pl.col("payment_date") > VALIDATION_END)
        | (pl.col("payment_date") < pl.col("corrected_ex_date"))
    )
    if invalid.height:
        raise ValueError("invalid or sealed final test date in official action correction")
    for row in evidence.iter_rows(named=True):
        source_url = str(row["source_url"])
        if not source_url.startswith("https://static.cninfo.com.cn/finalpage/"):
            raise ValueError(f"invalid official action correction URL: {source_url}")
        cached = cache_root / row["cache_file"]
        if not cached.is_file() or sha256_file(cached) != row["sha256"]:
            raise ValueError(
                f"official action correction hash mismatch: {row['symbol']}"
            )
    return evidence.select(
        "symbol",
        "source_ex_date",
        "corrected_ex_date",
        "payment_date",
        "source_url",
    )


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
