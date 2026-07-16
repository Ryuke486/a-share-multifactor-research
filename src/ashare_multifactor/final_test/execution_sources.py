from __future__ import annotations

import os
from pathlib import Path
import shutil
from uuid import uuid4

import polars as pl

import json
import hashlib

from ashare_multifactor.audit.records import file_record, sha256_file, verify_file_record
from ashare_multifactor.data.security import market_for_symbol
from ashare_multifactor.execution.corporate_actions import normalize_corporate_actions
from ashare_multifactor.final_test.action_source_contract import (
    OFFICIAL_MARKET_SOURCES,
    build_execution_input_manifest,
    evidence_url_matches_source,
    load_action_source_contract,
)
from ashare_multifactor.final_test.corporate_action_coverage import (
    validate_corporate_action_coverage,
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
    security_event_coverage_path: Path,
    corporate_action_coverage_root: Path,
) -> dict[str, object]:
    """Generate, then bind, the frozen action/event inputs to this attempt."""
    del data_root
    source_root = final_root / "execution_input_sources"
    execution_root = final_root / "attempt_inputs" / authorization.attempt_id
    if (
        source_root.is_symlink()
        or execution_root.is_symlink()
        or source_root.resolve() != final_root.resolve() / "execution_input_sources"
        or execution_root.resolve()
        != final_root.resolve() / "attempt_inputs" / authorization.attempt_id
    ):
        raise ValueError("final execution input path uses a symlink or escapes root")
    if not source_root.exists():
        generate_final_execution_sources(
            authorization,
            code_root=code_root,
            final_root=final_root,
            security_event_coverage_path=security_event_coverage_path,
            corporate_action_coverage_root=corporate_action_coverage_root,
        )
    else:
        _verify_reusable_source_root(
            source_root,
            authorization=authorization,
            security_event_coverage_path=security_event_coverage_path,
            corporate_action_coverage_root=corporate_action_coverage_root,
            final_root=final_root,
        )
    source_actions = source_root / "corporate_actions.parquet"
    source_events = source_root / "security_events.parquet"
    if not source_actions.is_file() or not source_events.is_file():
        raise ValueError(
            "authoritative final execution source files must be generated before execution"
        )
    if execution_root.exists() or execution_root.is_symlink():
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
        for path in sorted(item for item in source_root.rglob("*") if item.is_file())
    ]
    return manifest


def generate_final_execution_sources(
    authorization: FinalTestAuthorization,
    *,
    code_root: Path,
    final_root: Path,
    security_event_coverage_path: Path,
    corporate_action_coverage_root: Path,
) -> Path:
    """Bind verified official action/event evidence to canonical execution inputs."""
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
    symbols = [
        symbol for symbol in symbols
        if market_for_symbol(symbol) in contract.supported_markets
    ]
    if not symbols:
        raise ValueError("final execution input acquisition requires symbols")
    security_coverage = validate_security_event_coverage(
        security_event_coverage_path,
        symbols=symbols,
    )
    coverage_root = security_coverage["coverage_root"]
    coverage_manifest_path = security_coverage["coverage_manifest_path"]
    corporate_coverage = validate_corporate_action_coverage(
        corporate_action_coverage_root,
        symbols=symbols,
    )
    actions = corporate_coverage["actions"]
    event_file = security_coverage.get("events_file")
    events = pl.read_parquet(event_file) if isinstance(event_file, Path) else pl.DataFrame(
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
    if events.height != security_coverage["event_rows"]:
        raise ValueError("official security-event row count changed")
    destination = final_root / "execution_input_sources"
    temporary = final_root / f".execution-input-sources-{uuid4().hex}.tmp"
    temporary.mkdir(parents=True)
    try:
        actions.write_parquet(temporary / "corporate_actions.parquet")
        if isinstance(event_file, Path):
            shutil.copy2(event_file, temporary / "security_events.parquet")
        else:
            events.write_parquet(temporary / "security_events.parquet")
        shutil.copy2(coverage_manifest_path, temporary / "security_event_coverage.json")
        corporate_destination = temporary / "corporate_action_coverage"
        corporate_destination.mkdir()
        corporate_support = {
            corporate_coverage["coverage_manifest_path"],
            corporate_coverage["candidate_file"],
            corporate_coverage["coverage_file"],
            corporate_coverage["evidence_index_file"],
            corporate_coverage["official_actions_file"],
            corporate_coverage["diff_file"],
            *corporate_coverage["evidence_paths"],
        }
        for support in sorted(corporate_support):
            relative = support.relative_to(corporate_coverage["coverage_root"])
            destination_support = corporate_destination / relative
            destination_support.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(support, destination_support)
        support_paths = set(security_coverage["evidence_paths"]) | set(
            security_coverage["coverage_paths"]
        )
        if isinstance(security_coverage.get("events_file"), Path):
            support_paths.add(security_coverage["events_file"])
        for support in sorted(support_paths):
            relative = support.relative_to(coverage_root)
            destination_support = temporary / relative
            destination_support.parent.mkdir(parents=True, exist_ok=True)
            if destination_support.exists():
                if sha256_file(destination_support) == sha256_file(support):
                    continue
                raise ValueError("security-event support file path collision")
            shutil.copy2(support, destination_support)
        records = [
            file_record(path, root=temporary, role="final_execution_source").to_dict()
            for path in sorted(item for item in temporary.rglob("*") if item.is_file())
        ]
        write_json(
            temporary / "source_manifest.json",
            {
                "attempt_id": authorization.attempt_id,
                "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
                "git_commit": authorization.git_commit,
                "git_tree": authorization.git_tree,
                "data_manifest_sha256": resolution.data_manifest_sha256,
                "security_event_coverage_sha256": sha256_file(
                    coverage_manifest_path
                ),
                "corporate_action_coverage_sha256": sha256_file(
                    corporate_coverage["coverage_manifest_path"]
                ),
                "period": [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()],
                "provider": contract.provider,
                "query_year_type": contract.query_year_type,
                "query_years": list(contract.query_years),
                "symbol_count": len(symbols),
                "successful_query_count": corporate_coverage["coverage"].height,
                "security_event_source": (
                    "official_events" if events.height else "official_zero_event_coverage"
                ),
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


def _verify_reusable_source_root(
    root: Path,
    *,
    authorization: FinalTestAuthorization,
    security_event_coverage_path: Path,
    corporate_action_coverage_root: Path,
    final_root: Path,
) -> None:
    if root.is_symlink() or root.resolve() != final_root.resolve() / "execution_input_sources":
        raise ValueError("reusable final execution source path is unsafe")
    try:
        manifest = json.loads((root / "source_manifest.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError("invalid reusable final execution source manifest") from error
    resolution = resolve_final_test_data_panel(final_root)
    security_coverage = validate_security_event_coverage(security_event_coverage_path)
    corporate_coverage = validate_corporate_action_coverage(
        corporate_action_coverage_root
    )
    expected = {
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "git_commit": authorization.git_commit,
        "git_tree": authorization.git_tree,
        "data_manifest_sha256": resolution.data_manifest_sha256,
        "security_event_coverage_sha256": sha256_file(
            security_coverage["coverage_manifest_path"]
        ),
        "corporate_action_coverage_sha256": sha256_file(
            corporate_coverage["coverage_manifest_path"]
        ),
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise ValueError("final execution sources cannot be reused across sealed inputs")
    for record in manifest.get("files", []):
        verify_file_record(record, root=root)


def validate_security_event_coverage(
    path: Path, *, symbols: list[str] | None = None
) -> dict[str, object]:
    path, coverage_root = _resolve_coverage_manifest(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError("invalid official security-event coverage") from error
    if (
        not isinstance(payload, dict)
        or payload.get("status") != "ready"
        or payload.get("period") != [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()]
        or payload.get("scope") != "all_final_execution_symbols"
        or not isinstance(payload.get("event_rows"), int)
        or payload["event_rows"] < 0
        or not isinstance(payload.get("symbol_count"), int)
        or payload["symbol_count"] <= 0
        or not isinstance(payload.get("symbols_sha256"), str)
        or len(payload.get("symbols_sha256", "")) != 64
        or not isinstance(payload.get("evidence_index"), dict)
        or not isinstance(payload.get("coverage"), list)
        or not payload["coverage"]
    ):
        raise ValueError("official security-event coverage is not ready")
    evidence_record = payload["evidence_index"]
    if evidence_record.get("role") != "official_security_event_evidence_index":
        raise ValueError("official security-event evidence index role is invalid")
    evidence_index_path = _verify_coverage_file(evidence_record, root=coverage_root)
    evidence_index = pl.read_parquet(evidence_index_path)
    evidence_columns = {
        "evidence_id", "source", "market", "source_url", "cache_file", "sha256"
    }
    if not evidence_columns.issubset(evidence_index.columns) or evidence_index.select(
        pl.col("evidence_id").is_duplicated().any()
    ).item():
        raise ValueError("official security-event evidence index is invalid")
    evidence_paths = [evidence_index_path]
    official_sources = dict(OFFICIAL_MARKET_SOURCES)
    for row in evidence_index.iter_rows(named=True):
        if (
            official_sources.get(str(row["market"])) != row["source"]
            or not evidence_url_matches_source(
                str(row["source"]), str(row["source_url"])
            )
        ):
            raise ValueError("official security-event evidence source is invalid")
        cached_input = coverage_root / str(row["cache_file"])
        _assert_no_symlink_path(cached_input, root=coverage_root)
        cached = cached_input.resolve()
        if (
            not cached.is_relative_to(coverage_root)
            or not cached.is_file()
            or sha256_file(cached) != row["sha256"]
        ):
            raise ValueError("official security-event evidence changed")
        evidence_paths.append(cached)
    coverage_paths = []
    for record in payload["coverage"]:
        if (
            not isinstance(record, dict)
            or record.get("role") != "official_security_event_coverage"
        ):
            raise ValueError("official security-event query coverage role is invalid")
        try:
            coverage_paths.append(_verify_coverage_file(record, root=coverage_root))
        except (FileNotFoundError, TypeError, ValueError) as error:
            raise ValueError("official security-event query coverage changed") from error
    coverage = pl.concat([pl.read_parquet(item) for item in coverage_paths])
    required = {
        "symbol", "source", "market", "query_start", "query_end", "status",
        "event_count", "evidence_id",
    }
    if not required.issubset(coverage.columns):
        raise ValueError("official security-event query coverage schema is invalid")
    normalized_coverage = coverage.select(
        pl.col("symbol").cast(pl.String).str.zfill(6).alias("source_symbol"),
        pl.col("source").cast(pl.String),
        pl.col("market").cast(pl.String),
        pl.col("query_start").cast(pl.Date),
        pl.col("query_end").cast(pl.Date),
        pl.col("status").cast(pl.String),
        pl.col("event_count").cast(pl.Int64),
        pl.col("evidence_id").cast(pl.String),
    )
    _validate_coverage_markets(normalized_coverage)
    normalized = (
        sorted({str(symbol).zfill(6) for symbol in symbols})
        if symbols is not None
        else sorted(normalized_coverage.get_column("source_symbol").unique())
    )
    digest = hashlib.sha256(("\n".join(normalized) + "\n").encode()).hexdigest()
    if payload["symbol_count"] != len(normalized) or payload["symbols_sha256"] != digest:
        raise ValueError("official security-event coverage symbol scope changed")
    coverage_keys = ["source_symbol", "market", "source"]
    if normalized_coverage.select(pl.struct(coverage_keys).is_duplicated().any()).item():
        raise ValueError("official security-event query coverage has duplicates")
    if symbols is not None:
        expected = pl.DataFrame({"source_symbol": normalized})
        if (
            normalized_coverage.join(expected, on="source_symbol", how="anti").height
            or expected.join(normalized_coverage, on="source_symbol", how="anti").height
        ):
            raise ValueError("official security-event query coverage is not exact")
    invalid = normalized_coverage.filter(
        (pl.col("status") != "ok")
        | (pl.col("query_start") != FINAL_TEST_START)
        | (pl.col("query_end") != FINAL_TEST_END)
        | (pl.col("event_count") < 0)
    )
    if invalid.height or normalized_coverage.join(
        evidence_index.select("evidence_id", "source", "market"),
        on=["evidence_id", "source", "market"],
        how="anti",
    ).height or normalized_coverage.get_column("event_count").sum() != payload["event_rows"]:
        raise ValueError("official security-event query coverage is invalid")
    result = dict(payload)
    result["evidence_paths"] = evidence_paths
    result["coverage_paths"] = coverage_paths
    result["evidence_index"] = evidence_index
    result["coverage_root"] = coverage_root
    result["coverage_manifest_path"] = path
    event_record = payload.get("events_file")
    normalized_events = pl.DataFrame(
        schema={
            "source_symbol": pl.String,
            "market": pl.String,
            "source": pl.String,
            "evidence_id": pl.String,
        }
    )
    if payload["event_rows"]:
        if not isinstance(event_record, dict):
            raise ValueError("official security-event coverage lacks event file")
        events_path = _verify_coverage_file(event_record, root=coverage_root)
        events = pl.read_parquet(events_path)
        event_required = {"source_symbol", "effective_date", "source", "evidence_id"}
        if not event_required.issubset(events.columns) or events.height != payload["event_rows"]:
            raise ValueError("official security events schema or count is invalid")
        normalized_events = events.with_columns(
            pl.col("source_symbol").cast(pl.String).str.zfill(6),
            pl.col("source").cast(pl.String),
            pl.col("evidence_id").cast(pl.String),
            pl.col("source_symbol")
            .cast(pl.String)
            .str.zfill(6)
            .map_elements(market_for_symbol, return_dtype=pl.String)
            .alias("market"),
        )
        for symbol_column in ("source_symbol", "target_symbol"):
            if symbol_column in events.columns and normalized_events.filter(
                pl.col(symbol_column).is_not_null()
                & ~pl.col(symbol_column)
                .cast(pl.String)
                .str.zfill(6)
                .map_elements(market_for_symbol, return_dtype=pl.String)
                .is_in(("sh", "sz"))
            ).height:
                raise ValueError("official security events escape supported market scope")
        evidence_mismatch = normalized_events.join(
            evidence_index, on=["evidence_id", "source", "market"], how="anti"
        )
        coverage_mismatch = normalized_events.join(
            normalized_coverage.select(*coverage_keys, "evidence_id"),
            on=[*coverage_keys, "evidence_id"],
            how="anti",
        )
        if evidence_mismatch.height or coverage_mismatch.height or events.filter(
            ~pl.col("effective_date").is_between(FINAL_TEST_START, FINAL_TEST_END)
        ).height:
            raise ValueError("official security event evidence join failed")
        result["events_file"] = events_path
    _validate_event_counts(normalized_coverage, normalized_events)
    return result


def _validate_event_counts(coverage: pl.DataFrame, events: pl.DataFrame) -> None:
    """Require exact event counts for every official source/market/security key."""
    keys = ["source_symbol", "market", "source"]
    if "source_symbol" not in coverage.columns and "symbol" in coverage.columns:
        coverage = coverage.rename({"symbol": "source_symbol"})
    expected = coverage.select(*keys, pl.col("event_count").cast(pl.Int64))
    actual = events.group_by(keys).len().rename({"len": "actual_event_count"})
    reconciled = expected.join(actual, on=keys, how="full", coalesce=True).filter(
        pl.col("event_count").is_null()
        | (pl.col("event_count") != pl.col("actual_event_count").fill_null(0))
    )
    if reconciled.height:
        raise ValueError("official security-event event counts do not match coverage")


def _validate_coverage_markets(coverage: pl.DataFrame) -> None:
    expected = coverage.with_columns(
        pl.col("source_symbol")
        .map_elements(market_for_symbol, return_dtype=pl.String)
        .alias("expected_market")
    )
    if expected.filter(pl.col("market") != pl.col("expected_market")).height:
        raise ValueError("official security-event symbol market is invalid")
    allowed = [
        {"market": market, "source": source}
        for market, source in OFFICIAL_MARKET_SOURCES
    ]
    if expected.filter(~pl.struct("market", "source").is_in(allowed)).height:
        raise ValueError("official security-event market source is invalid")


def _resolve_coverage_manifest(path: Path) -> tuple[Path, Path]:
    absolute = path if path.is_absolute() else Path.cwd() / path
    absolute = absolute.absolute()
    for candidate in (absolute, *absolute.parents):
        if candidate.is_symlink():
            raise ValueError("official security-event coverage uses a symlink")
    try:
        resolved = absolute.resolve(strict=True)
    except FileNotFoundError as error:
        raise ValueError("invalid official security-event coverage") from error
    return resolved, resolved.parent


def _assert_no_symlink_path(path: Path, *, root: Path) -> None:
    try:
        relative = path.absolute().relative_to(root)
    except ValueError as error:
        raise ValueError("official security-event support file escapes coverage root") from error
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("official security-event support file uses a symlink")


def _verify_coverage_file(record: dict[str, object], *, root: Path) -> Path:
    recorded_path = record.get("path")
    if not isinstance(recorded_path, str):
        raise ValueError("official security-event support file path is invalid")
    _assert_no_symlink_path(root / recorded_path, root=root)
    return verify_file_record(record, root=root)


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
            prefix = market_for_symbol(symbol)
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
