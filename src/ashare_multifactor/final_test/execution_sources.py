from __future__ import annotations

import os
from pathlib import Path
import shutil
from uuid import uuid4

import polars as pl

from ashare_multifactor.audit.records import file_record
from ashare_multifactor.execution.corporate_actions import normalize_corporate_actions
from ashare_multifactor.final_test.action_source_contract import (
    build_execution_input_manifest,
    load_action_source_contract,
)
from ashare_multifactor.final_test.data_publication import resolve_final_test_data_panel
from ashare_multifactor.final_test.data_inventory import write_json
from ashare_multifactor.final_test.gate import (
    FINAL_TEST_END,
    FINAL_TEST_START,
    FinalTestAuthorization,
)


def build_final_execution_inputs(
    authorization: FinalTestAuthorization,
    *,
    code_root: Path,
    data_root: Path,
    final_root: Path,
) -> dict[str, object]:
    """Generate, then bind, the frozen action/event inputs to this attempt."""
    del data_root
    source_root = final_root / "execution_input_sources"
    execution_root = final_root / "execution_inputs"
    if not source_root.exists():
        generate_final_execution_sources(
            authorization,
            code_root=code_root,
            final_root=final_root,
        )
    source_actions = source_root / "corporate_actions.parquet"
    source_events = source_root / "security_events.parquet"
    if not source_actions.is_file() or not source_events.is_file():
        raise ValueError(
            "authoritative final execution source files must be generated before execution"
        )
    if execution_root.exists():
        raise FileExistsError("final execution inputs are already bound")
    execution_root.mkdir(parents=True)
    files = {}
    for source in (source_actions, source_events):
        destination = execution_root / source.name
        shutil.copy2(source, destination)
        files[source.name] = destination
    manifest = build_execution_input_manifest(
        execution_root / "manifest.json",
        authorization=authorization,
        files=files,
    )
    manifest["source_contract"] = file_record(
        code_root / "configs/final_execution_sources.yaml",
        root=code_root,
        role="final_execution_source_contract",
    ).to_dict()
    manifest["source_files"] = [
        file_record(path, root=source_root, role="final_execution_source").to_dict()
        for path in (source_actions, source_events, source_root / "source_manifest.json")
    ]
    return manifest


def generate_final_execution_sources(
    authorization: FinalTestAuthorization,
    *,
    code_root: Path,
    final_root: Path,
) -> Path:
    """Acquire every frozen BaoStock query and create canonical execution inputs."""
    _assert_authorization(authorization)
    contract = load_action_source_contract(
        code_root / "configs/final_execution_sources.yaml"
    )
    resolution = resolve_final_test_data_panel(final_root)
    if resolution.claim_status != "published" or resolution.requires_recovery:
        raise ValueError("final-test data publication requires recovery")
    files = tuple(sorted(resolution.root.glob("year=*/part-*.parquet")))
    if not files:
        raise ValueError("verified final-test daily panel is empty")
    symbols = sorted(
        pl.scan_parquet(files)
        .select(pl.col("symbol").cast(pl.String).str.zfill(6))
        .unique()
        .collect()
        .get_column("symbol")
    )
    if not symbols:
        raise ValueError("final execution input acquisition requires symbols")
    raw, coverage = _download_final_dividends(
        symbols,
        years=contract.query_years,
        year_type=contract.query_year_type,
    )
    actions = _normalize_final_dividends(raw, symbols=set(symbols))
    events = pl.DataFrame(
        schema={
            "effective_date": pl.Date,
            "source_symbol": pl.String,
            "event_type": pl.String,
            "target_symbol": pl.String,
            "ratio": pl.Float64,
            "cash_per_share": pl.Float64,
            "source": pl.String,
        }
    )
    destination = final_root / "execution_input_sources"
    temporary = final_root / f".execution-input-sources-{uuid4().hex}.tmp"
    temporary.mkdir(parents=True)
    try:
        raw.write_parquet(temporary / "baostock_dividends.parquet")
        coverage.write_parquet(temporary / "baostock_query_coverage.parquet")
        actions.write_parquet(temporary / "corporate_actions.parquet")
        events.write_parquet(temporary / "security_events.parquet")
        records = [
            file_record(path, root=temporary, role="final_execution_source").to_dict()
            for path in sorted(temporary.iterdir())
        ]
        write_json(
            temporary / "source_manifest.json",
            {
                "attempt_id": authorization.attempt_id,
                "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
                "period": [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()],
                "provider": contract.provider,
                "query_year_type": contract.query_year_type,
                "query_years": list(contract.query_years),
                "symbol_count": len(symbols),
                "successful_query_count": coverage.height,
                "security_event_source": "explicit_no_events_identified",
                "files": records,
            },
        )
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("final execution source inputs already exist")
        os.replace(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination


def _download_final_dividends(
    symbols: list[str],
    *,
    years: tuple[int, ...],
    year_type: str,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    try:
        import baostock as bs
    except ImportError as error:  # pragma: no cover - environment failure
        raise RuntimeError("baostock is required for final execution inputs") from error
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login.error_msg}")
    rows: list[dict[str, object]] = []
    coverage: list[dict[str, object]] = []
    known_fields: list[str] | None = None
    try:
        for symbol in symbols:
            prefix = "sh" if symbol.startswith(("5", "6", "9")) else "sz"
            for year in years:
                result = bs.query_dividend_data(
                    code=f"{prefix}.{symbol}",
                    year=str(year),
                    yearType=year_type,
                )
                if result.error_code != "0":
                    raise RuntimeError(
                        f"BaoStock dividend query failed: {symbol} {year} "
                        f"{result.error_msg}"
                    )
                fields = list(result.fields)
                if known_fields is None:
                    known_fields = fields
                elif fields != known_fields:
                    raise ValueError("BaoStock dividend response schema changed")
                count = 0
                while result.next():
                    values = result.get_row_data()
                    if len(values) != len(fields):
                        raise ValueError("BaoStock dividend response row width changed")
                    rows.append(
                        {
                            **dict(zip(fields, values, strict=True)),
                            "query_year": year,
                            "query_year_type": year_type,
                        }
                    )
                    count += 1
                coverage.append(
                    {"symbol": symbol, "year": year, "status": "ok", "row_count": count}
                )
    finally:
        bs.logout()
    schema = {field: pl.String for field in (known_fields or [])}
    schema.update({"query_year": pl.Int64, "query_year_type": pl.String})
    return (
        pl.DataFrame(rows, schema=schema, strict=False),
        pl.DataFrame(
            coverage,
            schema={
                "symbol": pl.String,
                "year": pl.Int64,
                "status": pl.String,
                "row_count": pl.Int64,
            },
        ).sort("symbol", "year"),
    )


def _normalize_final_dividends(
    raw: pl.DataFrame,
    *,
    symbols: set[str],
) -> pl.DataFrame:
    required = {
        "code",
        "query_year",
        "query_year_type",
        "dividOperateDate",
        "dividPayDate",
        "dividStockMarketDate",
        "dividCashPsBeforeTax",
        "dividStocksPs",
        "dividReserveToStockPs",
    }
    missing = sorted(required - set(raw.columns))
    if missing:
        raise ValueError(f"missing BaoStock final dividend fields: {missing}")
    if raw.filter(
        (pl.col("query_year_type") != "operate")
        | ~pl.col("query_year").is_between(2022, 2025)
    ).height:
        raise ValueError("sealed final-test query metadata is invalid")
    parsed = raw.with_columns(
        pl.col("code").cast(pl.String).str.split(".").list.last().alias("symbol"),
        *(
            pl.col(name)
            .cast(pl.String)
            .replace("", None)
            .str.to_date(strict=False)
            .alias(name)
            for name in ("dividOperateDate", "dividPayDate", "dividStockMarketDate")
        ),
        pl.col("dividCashPsBeforeTax")
        .cast(pl.Float64, strict=False)
        .fill_null(0.0)
        .alias("cash_per_share"),
        (
            pl.col("dividStocksPs").cast(pl.Float64, strict=False).fill_null(0.0)
            + pl.col("dividReserveToStockPs")
            .cast(pl.Float64, strict=False)
            .fill_null(0.0)
        ).alias("share_ratio"),
    )
    if parsed.filter(
        pl.col("dividOperateDate").is_null()
        | (pl.col("dividOperateDate").dt.year() != pl.col("query_year"))
        | ~pl.col("dividOperateDate").is_between(FINAL_TEST_START, FINAL_TEST_END)
    ).height:
        raise ValueError("BaoStock final operate-year response contract failed")
    parsed = parsed.filter(pl.col("symbol").is_in(symbols))
    cash = parsed.filter(pl.col("cash_per_share") > 0)
    if cash.filter(pl.col("dividPayDate").is_null()).height:
        raise ValueError("final cash dividend is missing payment date")
    shares = parsed.filter(pl.col("share_ratio") > 0)
    candidates = pl.concat(
        (
            cash.select(
                "symbol",
                pl.col("dividOperateDate").alias("ex_date"),
                pl.col("dividPayDate").alias("effective_date"),
                "cash_per_share",
                pl.lit(0.0).alias("share_ratio"),
                pl.lit("baostock_dividend_operate_year").alias("source"),
            ),
            shares.select(
                "symbol",
                pl.col("dividOperateDate").alias("ex_date"),
                pl.coalesce("dividStockMarketDate", "dividOperateDate").alias(
                    "effective_date"
                ),
                pl.lit(0.0).alias("cash_per_share"),
                "share_ratio",
                pl.lit("baostock_dividend_operate_year").alias("source"),
            ),
        ),
        how="vertical_relaxed",
    )
    if candidates.filter(
        (pl.col("effective_date") < pl.col("ex_date"))
        | ~pl.col("effective_date").is_between(FINAL_TEST_START, FINAL_TEST_END)
    ).height:
        raise ValueError("final corporate action effective date is invalid")
    return normalize_corporate_actions(candidates, maximum_date=FINAL_TEST_END)


def _assert_authorization(authorization: FinalTestAuthorization) -> None:
    if not isinstance(authorization, FinalTestAuthorization):
        raise TypeError("authorization must be a FinalTestAuthorization")
    if authorization.test_period != (FINAL_TEST_START, FINAL_TEST_END):
        raise ValueError("authorization differs from the exact final-test period")
