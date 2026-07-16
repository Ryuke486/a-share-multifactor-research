from __future__ import annotations

import hashlib
import json
from pathlib import Path

import polars as pl

from ashare_multifactor.audit.records import sha256_file, verify_file_record
from ashare_multifactor.data.security import market_for_symbol
from ashare_multifactor.execution.corporate_actions import normalize_corporate_actions
from ashare_multifactor.final_test.action_source_contract import (
    OFFICIAL_MARKET_SOURCES,
    evidence_url_matches_source,
)
from ashare_multifactor.final_test.gate import FINAL_TEST_END, FINAL_TEST_START

def validate_corporate_action_coverage(
    root: Path, *, symbols: list[str] | None = None
) -> dict[str, object]:
    """Validate exact official exchange coverage and candidate reconciliation."""
    manifest_path, coverage_root = _resolve_corporate_action_root(root)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError("invalid official corporate-action coverage") from error
    required_records = {
        "candidate_file": "baostock_corporate_action_candidates",
        "query_coverage": "official_corporate_action_coverage",
        "evidence_index": "official_corporate_action_evidence_index",
        "official_actions": "official_corporate_actions",
        "candidate_diff": "corporate_action_candidate_diff",
    }
    if (
        not isinstance(payload, dict)
        or payload.get("status") != "ready"
        or payload.get("period")
        != [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()]
        or payload.get("scope") != "all_final_execution_symbols"
        or not isinstance(payload.get("symbol_count"), int)
        or payload["symbol_count"] <= 0
        or not isinstance(payload.get("symbols_sha256"), str)
        or len(payload["symbols_sha256"]) != 64
    ):
        raise ValueError("official corporate-action coverage is not ready")
    paths: dict[str, Path] = {}
    for field, role in required_records.items():
        record = payload.get(field)
        if not isinstance(record, dict) or record.get("role") != role:
            raise ValueError(f"official corporate-action {field} record is invalid")
        try:
            paths[field] = _verify_coverage_file(record, root=coverage_root)
        except (FileNotFoundError, TypeError, ValueError) as error:
            raise ValueError(f"official corporate-action {field} changed") from error

    evidence = pl.read_parquet(paths["evidence_index"])
    evidence_required = {
        "evidence_id", "source", "market", "source_url", "cache_file", "sha256"
    }
    if (
        not evidence_required.issubset(evidence.columns)
        or evidence.select(pl.col("evidence_id").is_duplicated().any()).item()
    ):
        raise ValueError("official corporate-action evidence index is invalid")
    evidence_paths: list[Path] = []
    official_sources = dict(OFFICIAL_MARKET_SOURCES)
    for row in evidence.iter_rows(named=True):
        if (
            official_sources.get(str(row["market"])) != row["source"]
            or not evidence_url_matches_source(str(row["source"]), str(row["source_url"]))
        ):
            raise ValueError("official corporate-action evidence source is invalid")
        cached_input = coverage_root / str(row["cache_file"])
        _assert_no_symlink_path(cached_input, root=coverage_root)
        cached = cached_input.resolve()
        if (
            not cached.is_relative_to(coverage_root)
            or not cached.is_file()
            or sha256_file(cached) != row["sha256"]
        ):
            raise ValueError("official corporate-action evidence hash changed")
        evidence_paths.append(cached)

    coverage = pl.read_parquet(paths["query_coverage"]).with_columns(
        pl.col("symbol").cast(pl.String).str.zfill(6),
        pl.col("query_start").cast(pl.Date),
        pl.col("query_end").cast(pl.Date),
    )
    coverage_required = {
        "symbol", "source", "market", "query_start", "query_end", "status",
        "event_count", "evidence_id",
    }
    if not coverage_required.issubset(coverage.columns):
        raise ValueError("official corporate-action query coverage schema is invalid")
    normalized = (
        sorted({str(symbol).zfill(6) for symbol in symbols})
        if symbols is not None
        else sorted(coverage.get_column("symbol").unique())
    )
    digest = hashlib.sha256(("\n".join(normalized) + "\n").encode()).hexdigest()
    if payload["symbol_count"] != len(normalized) or payload["symbols_sha256"] != digest:
        raise ValueError("official corporate-action coverage symbol scope changed")
    if coverage.select(pl.col("symbol").is_duplicated().any()).item():
        raise ValueError("official corporate-action query coverage has duplicates")
    expected = pl.DataFrame({"symbol": normalized})
    if (
        coverage.join(expected, on="symbol", how="anti").height
        or expected.join(coverage, on="symbol", how="anti").height
    ):
        raise ValueError("official corporate-action query coverage is not exact")
    coverage_market = coverage.rename({"symbol": "source_symbol"})
    _validate_coverage_markets(coverage_market)
    if coverage.filter(
        (pl.col("status") != "ok")
        | (pl.col("query_start") != FINAL_TEST_START)
        | (pl.col("query_end") != FINAL_TEST_END)
        | (pl.col("event_count") < 0)
    ).height or coverage.join(
        evidence.select("evidence_id", "source", "market"),
        on=["evidence_id", "source", "market"],
        how="anti",
    ).height:
        raise ValueError("official corporate-action query coverage is invalid")

    official = pl.read_parquet(paths["official_actions"])
    official_required = {
        "symbol", "announcement_date", "ex_date", "effective_date", "cash_per_share",
        "share_ratio", "market", "source", "evidence_id", "candidate_id",
    }
    if not official_required.issubset(official.columns):
        raise ValueError("official corporate-action rows schema is invalid")
    official = official.with_columns(
        pl.col("symbol").cast(pl.String).str.zfill(6),
        pl.col("announcement_date").cast(pl.Date),
        pl.col("ex_date").cast(pl.Date),
        pl.col("effective_date").cast(pl.Date),
    )
    action_key = [
        "symbol", "ex_date", "effective_date", "cash_per_share", "share_ratio", "source"
    ]
    if official.select(pl.struct(action_key).is_duplicated().any()).item():
        raise ValueError("official corporate-action rows contain duplicate actions")
    official_join = official.with_columns(
        pl.col("symbol")
        .map_elements(market_for_symbol, return_dtype=pl.String)
        .alias("expected_market")
    )
    if official.filter(
        ~pl.col("symbol").is_in(normalized)
        | pl.col("announcement_date").is_null()
        | (pl.col("announcement_date") > pl.col("ex_date"))
        | (pl.col("announcement_date") > pl.col("effective_date"))
        | ~pl.col("ex_date").is_between(FINAL_TEST_START, FINAL_TEST_END)
        | ~pl.col("effective_date").is_between(FINAL_TEST_START, FINAL_TEST_END)
    ).height or official_join.filter(
        pl.col("market") != pl.col("expected_market")
    ).height or official_join.join(
        evidence.select("evidence_id", "source", "market"),
        on=["evidence_id", "source", "market"],
        how="anti",
    ).height:
        raise ValueError("official corporate-action row lacks official evidence")
    actual_counts = official.group_by("symbol").len().rename({"len": "actual_count"})
    if coverage.join(actual_counts, on="symbol", how="left").filter(
        pl.col("event_count") != pl.col("actual_count").fill_null(0)
    ).height:
        raise ValueError("official corporate-action event counts do not match coverage")

    candidates = pl.read_parquet(paths["candidate_file"])
    candidate_fields = {
        "candidate_id", "symbol", "ex_date", "effective_date", "cash_per_share",
        "share_ratio",
    }
    if not candidate_fields.issubset(candidates.columns) or candidates.select(
        pl.col("candidate_id").is_duplicated().any()
    ).item():
        raise ValueError("BaoStock corporate-action candidates are invalid")
    diff = pl.read_parquet(paths["candidate_diff"])
    diff_required = {
        "candidate_id", "status", "evidence_id", "explanation", "original_ex_date",
        "corrected_ex_date", "correction_reason",
    }
    if not diff_required.issubset(diff.columns) or diff.select(
        pl.col("candidate_id").is_duplicated().any()
    ).item():
        raise ValueError("corporate-action candidate differences are invalid")
    candidate_ids = candidates.select(pl.col("candidate_id").cast(pl.String))
    diff_ids = diff.select(pl.col("candidate_id").cast(pl.String))
    if (
        candidate_ids.join(diff_ids, on="candidate_id", how="anti").height
        or diff_ids.join(candidate_ids, on="candidate_id", how="anti").height
        or diff.join(evidence.select("evidence_id"), on="evidence_id", how="anti").height
    ):
        raise ValueError("BaoStock candidate lacks official evidence")
    if diff.filter(
        ~pl.col("status").is_in(("accepted", "corrected", "rejected"))
        | pl.col("explanation").cast(pl.String).str.strip_chars().eq("")
    ).height:
        raise ValueError("corporate-action candidate difference is unexplained")
    if diff.filter(
        (pl.col("status") == "corrected")
        & (
            pl.col("original_ex_date").is_null()
            | pl.col("corrected_ex_date").is_null()
            | pl.col("correction_reason").is_null()
            | (pl.col("correction_reason").cast(pl.String).str.strip_chars() == "")
        )
    ).height:
        raise ValueError("corrected corporate-action candidate lacks date reconciliation")
    linked = official.filter(pl.col("candidate_id").is_not_null()).select(
        pl.col("candidate_id").cast(pl.String)
    )
    if linked.join(candidate_ids, on="candidate_id", how="anti").height:
        raise ValueError("official corporate-action row references unknown candidate")
    publishable_candidates = diff.filter(
        pl.col("status").is_in(("accepted", "corrected"))
    ).select(pl.col("candidate_id").cast(pl.String))
    if (
        publishable_candidates.join(linked, on="candidate_id", how="anti").height
        or linked.join(publishable_candidates, on="candidate_id", how="anti").height
    ):
        raise ValueError("candidate disposition does not match official action rows")
    linked_counts = linked.group_by("candidate_id").len()
    if linked_counts.filter(pl.col("len") != 1).height:
        raise ValueError("candidate maps to multiple official action rows")
    candidate_rows = {
        str(row["candidate_id"]): row for row in candidates.iter_rows(named=True)
    }
    official_rows = {
        str(row["candidate_id"]): row
        for row in official.filter(pl.col("candidate_id").is_not_null()).iter_rows(named=True)
    }
    comparison_fields = (
        "symbol", "ex_date", "effective_date", "cash_per_share", "share_ratio"
    )
    for disposition in diff.iter_rows(named=True):
        candidate_id = str(disposition["candidate_id"])
        status = str(disposition["status"])
        official_row = official_rows.get(candidate_id)
        if status == "rejected":
            if official_row is not None:
                raise ValueError("rejected candidate has an official action row")
            continue
        if official_row is None:
            raise ValueError("publishable candidate lacks official action row")
        candidate_row = candidate_rows[candidate_id]
        for field in comparison_fields:
            expected = candidate_row[field]
            if status == "corrected":
                corrected_field = f"corrected_{field}"
                original_field = f"original_{field}"
                if corrected_field not in diff.columns or original_field not in diff.columns:
                    raise ValueError("corrected candidate lacks explicit field reconciliation")
                original = disposition[original_field]
                corrected = disposition[corrected_field]
                if original is not None and original != candidate_row[field]:
                    raise ValueError("corrected candidate original value is false")
                if corrected is not None:
                    expected = corrected
            if official_row[field] != expected:
                raise ValueError(f"candidate official {field} mismatch")

    actions = normalize_corporate_actions(
        official.select(
            "symbol", "ex_date", "effective_date", "cash_per_share", "share_ratio", "source"
        ),
        maximum_date=FINAL_TEST_END,
    )
    result = dict(payload)
    result.update(
        {
            "coverage_manifest_path": manifest_path,
            "coverage_root": coverage_root,
            "candidate_file": paths["candidate_file"],
            "coverage_file": paths["query_coverage"],
            "evidence_index_file": paths["evidence_index"],
            "official_actions_file": paths["official_actions"],
            "diff_file": paths["candidate_diff"],
            "evidence_paths": evidence_paths,
            "coverage": coverage,
            "actions": actions,
        }
    )
    return result


def _resolve_corporate_action_root(root: Path) -> tuple[Path, Path]:
    absolute = (root if root.is_absolute() else Path.cwd() / root).absolute()
    for candidate in (absolute, *absolute.parents):
        if candidate.is_symlink():
            raise ValueError("official corporate-action coverage uses a symlink")
    try:
        resolved = absolute.resolve(strict=True)
    except FileNotFoundError as error:
        raise ValueError("invalid official corporate-action coverage root") from error
    if not resolved.is_dir():
        raise ValueError("invalid official corporate-action coverage root")
    return resolved / "coverage.json", resolved




def _validate_coverage_markets(coverage: pl.DataFrame) -> None:
    expected = coverage.with_columns(
        pl.col("source_symbol")
        .map_elements(market_for_symbol, return_dtype=pl.String)
        .alias("expected_market")
    )
    if expected.filter(pl.col("market") != pl.col("expected_market")).height:
        raise ValueError("official corporate-action symbol market is invalid")
    allowed = [
        {"market": market, "source": source}
        for market, source in OFFICIAL_MARKET_SOURCES
    ]
    if expected.filter(~pl.struct("market", "source").is_in(allowed)).height:
        raise ValueError("official corporate-action market source is invalid")


def _assert_no_symlink_path(path: Path, *, root: Path) -> None:
    try:
        relative = path.absolute().relative_to(root)
    except ValueError as error:
        raise ValueError("official corporate-action support file escapes coverage root") from error
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("official corporate-action support file uses a symlink")


def _verify_coverage_file(record: dict[str, object], *, root: Path) -> Path:
    recorded_path = record.get("path")
    if not isinstance(recorded_path, str):
        raise ValueError("official corporate-action support file path is invalid")
    _assert_no_symlink_path(root / recorded_path, root=root)
    return verify_file_record(record, root=root)
