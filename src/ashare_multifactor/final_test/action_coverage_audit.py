from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from string import hexdigits
from typing import Any

import polars as pl

from ashare_multifactor.audit.records import file_record, sha256_file
from ashare_multifactor.final_test.data_inventory import write_json


_VALIDATION_START = date(2017, 1, 1)
_VALIDATION_END = date(2021, 12, 31)
_ACTION_KEYS = (
    "symbol",
    "ex_date",
    "effective_date",
    "cash_per_share",
    "share_ratio",
)
_OFFICIAL_PREFIXES = (
    "https://static.cninfo.com.cn/finalpage/",
    "https://disc.static.szse.cn/download/disc/",
    "https://www.sse.com.cn/disclosure/listedinfo/announcement/",
    "https://www.sse.com.cn/assortment/stock/list/info/profit/",
)


@dataclass(frozen=True)
class ActionCoverageAudit:
    status: str
    summary: dict[str, Any]
    query_coverage: pl.DataFrame
    event_differences: pl.DataFrame
    official_evidence_index: pl.DataFrame


def load_official_action_audit_evidence(
    evidence_path: Path,
    cache_root: Path,
) -> pl.DataFrame:
    evidence = pl.read_csv(
        evidence_path,
        schema_overrides={"symbol": pl.String},
        try_parse_dates=True,
    )
    required = {
        "year",
        "event_type",
        "symbol",
        "ex_date",
        "effective_date",
        "cash_per_share",
        "share_ratio",
        "source_url",
        "cache_file",
        "sha256",
    }
    missing = sorted(required - set(evidence.columns))
    if missing:
        raise ValueError(f"official action audit evidence fields missing: {missing}")
    if evidence.select(pl.struct("year", "event_type").is_duplicated().any()).item():
        raise ValueError("duplicate official action audit sample")
    resolved_root = cache_root.resolve()
    for row in evidence.iter_rows(named=True):
        source_url = str(row["source_url"])
        if not any(source_url.startswith(prefix) for prefix in _OFFICIAL_PREFIXES):
            raise ValueError(f"invalid official action audit URL: {source_url}")
        cached = (cache_root / str(row["cache_file"])).resolve()
        try:
            cached.relative_to(resolved_root)
        except ValueError as error:
            raise ValueError("official action audit cache escapes its root") from error
        if (
            not cached.is_file()
            or cached.is_symlink()
            or sha256_file(cached) != row["sha256"]
        ):
            raise ValueError(f"official action audit hash mismatch: {row['year']}")
    return evidence.drop("cache_file").sort("year", "event_type")


def write_action_coverage_audit(
    root: Path,
    audit: ActionCoverageAudit,
    *,
    inputs: dict[str, dict[str, object]],
) -> dict[str, object]:
    if audit.status != "ready" or audit.summary.get("status") != "ready":
        raise ValueError("only a ready action coverage audit can be written")
    if not inputs or any(
        not _valid_input_identity(identity) for identity in inputs.values()
    ):
        raise ValueError("action coverage audit input identity is incomplete")
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise FileExistsError(f"action coverage audit root is not empty: {root}")
    summary_path = root / "summary.json"
    coverage_path = root / "query_coverage.parquet"
    differences_path = root / "event_differences.parquet"
    evidence_path = root / "official_evidence_index.parquet"
    write_json(summary_path, audit.summary)
    audit.query_coverage.write_parquet(coverage_path)
    audit.event_differences.write_parquet(differences_path)
    audit.official_evidence_index.write_parquet(evidence_path)
    files = [
        file_record(path, root=root, role=role).to_dict()
        for path, role in (
            (summary_path, "action_coverage_summary"),
            (coverage_path, "action_query_coverage"),
            (differences_path, "action_event_differences"),
            (evidence_path, "official_action_evidence_index"),
        )
    ]
    manifest: dict[str, object] = {
        "status": "ready",
        "inputs": inputs,
        "files": files,
    }
    write_json(root / "manifest.json", manifest)
    return manifest


def _valid_input_identity(identity: dict[str, object]) -> bool:
    digest = str(identity.get("sha256", ""))
    size = identity.get("size")
    return (
        len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
        and isinstance(size, int)
        and size >= 0
    )


def audit_validation_action_coverage(
    *,
    symbols: list[str],
    query_coverage: pl.DataFrame,
    candidate_actions: pl.DataFrame,
    published_actions: pl.DataFrame,
    official_evidence: pl.DataFrame,
) -> ActionCoverageAudit:
    """Fail closed unless validation action acquisition is complete and exact."""
    normalized_symbols = sorted({str(symbol).zfill(6) for symbol in symbols})
    if not normalized_symbols:
        raise ValueError("validation action audit requires symbols")
    coverage = _validate_query_coverage(query_coverage, normalized_symbols)
    candidate = _validate_actions(candidate_actions, "candidate")
    published = _validate_actions(published_actions, "published")
    differences = _event_differences(candidate, published)
    if differences.height:
        raise ValueError("validation corporate-action differences are not zero")
    sample_gaps = _validate_official_samples(candidate, official_evidence)
    if sample_gaps:
        raise ValueError("validation official sample coverage is incomplete")
    summary = {
        "status": "ready",
        "period": [_VALIDATION_START.isoformat(), _VALIDATION_END.isoformat()],
        "symbol_count": len(normalized_symbols),
        "expected_queries": len(normalized_symbols) * 5,
        "successful_queries": coverage.height,
        "zero_event_queries": coverage.filter(pl.col("row_count") == 0).height,
        "candidate_event_rows": candidate.height,
        "published_event_rows": published.height,
        "event_difference_rows": differences.height,
        "official_sample_rows": official_evidence.height,
        "official_sample_gaps": 0,
    }
    return ActionCoverageAudit(
        status="ready",
        summary=summary,
        query_coverage=coverage,
        event_differences=differences,
        official_evidence_index=official_evidence.sort("year", "event_type", "symbol"),
    )


def _validate_query_coverage(
    coverage: pl.DataFrame,
    symbols: list[str],
) -> pl.DataFrame:
    required = {"symbol", "year", "status", "row_count"}
    missing = sorted(required - set(coverage.columns))
    if missing:
        raise ValueError(f"validation action query coverage fields missing: {missing}")
    normalized = coverage.select(
        pl.col("symbol").cast(pl.String).str.zfill(6),
        pl.col("year").cast(pl.Int64),
        pl.col("status").cast(pl.String),
        pl.col("row_count").cast(pl.Int64),
    )
    expected = pl.DataFrame(
        {
            "symbol": [symbol for symbol in symbols for _ in range(5)],
            "year": list(range(2017, 2022)) * len(symbols),
        }
    )
    if (
        normalized.select(pl.struct("symbol", "year").is_duplicated().any()).item()
        or normalized.filter(
            (pl.col("status") != "ok") | (pl.col("row_count") < 0)
        ).height
        or normalized.join(expected, on=["symbol", "year"], how="anti").height
        or expected.join(normalized, on=["symbol", "year"], how="anti").height
    ):
        raise ValueError("validation action query coverage is incomplete")
    return normalized.sort("symbol", "year")


def _validate_actions(frame: pl.DataFrame, label: str) -> pl.DataFrame:
    required = {"action_id", "source", *_ACTION_KEYS}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{label} corporate-action fields missing: {missing}")
    normalized = frame.select(
        pl.col("action_id").cast(pl.String),
        pl.col("symbol").cast(pl.String).str.zfill(6),
        pl.col("ex_date").cast(pl.Date),
        pl.col("effective_date").cast(pl.Date),
        pl.col("cash_per_share").cast(pl.Float64),
        pl.col("share_ratio").cast(pl.Float64),
        pl.col("source").cast(pl.String),
    ).filter(pl.col("ex_date").is_between(_VALIDATION_START, _VALIDATION_END))
    invalid = normalized.filter(
        pl.col("action_id").is_null()
        | (pl.col("action_id") == "")
        | pl.col("effective_date").is_null()
        | (pl.col("effective_date") < pl.col("ex_date"))
        | (pl.col("effective_date") > _VALIDATION_END)
        | ~pl.col("cash_per_share").is_finite()
        | ~pl.col("share_ratio").is_finite()
        | (pl.col("cash_per_share") < 0)
        | (pl.col("share_ratio") < 0)
        | (
            (pl.col("cash_per_share") == 0)
            & (pl.col("share_ratio") == 0)
        )
    )
    if (
        invalid.height
        or normalized.select(pl.col("action_id").is_duplicated().any()).item()
        or normalized.select(pl.struct(*_ACTION_KEYS).is_duplicated().any()).item()
    ):
        raise ValueError(f"invalid or duplicate {label} corporate actions")
    return normalized.sort(*_ACTION_KEYS)


def _event_differences(candidate: pl.DataFrame, published: pl.DataFrame) -> pl.DataFrame:
    candidate_keys = candidate.select(*_ACTION_KEYS).unique()
    published_keys = published.select(*_ACTION_KEYS).unique()
    only_candidate = candidate_keys.join(
        published_keys, on=list(_ACTION_KEYS), how="anti"
    ).with_columns(pl.lit("candidate_only").alias("difference"))
    only_published = published_keys.join(
        candidate_keys, on=list(_ACTION_KEYS), how="anti"
    ).with_columns(pl.lit("published_only").alias("difference"))
    return pl.concat((only_candidate, only_published), how="vertical_relaxed").sort(
        "symbol", "ex_date", "difference"
    )


def _validate_official_samples(
    candidate: pl.DataFrame,
    evidence: pl.DataFrame,
) -> list[tuple[int, str]]:
    required = {
        "year",
        "event_type",
        "symbol",
        "ex_date",
        "effective_date",
        "cash_per_share",
        "share_ratio",
        "source_url",
        "sha256",
    }
    missing = sorted(required - set(evidence.columns))
    if missing:
        raise ValueError(f"official action evidence fields missing: {missing}")
    indexed = candidate.with_columns(
        pl.col("ex_date").dt.year().alias("year"),
        pl.col("cash_per_share").round(12),
        pl.col("share_ratio").round(12),
        pl.when(
            (pl.col("cash_per_share") > 0) & (pl.col("share_ratio") > 0)
        )
        .then(pl.lit("cash_and_shares"))
        .when(pl.col("cash_per_share") > 0)
        .then(pl.lit("cash"))
        .otherwise(pl.lit("shares"))
        .alias("event_type"),
    )
    samples = evidence.select(
        pl.col("year").cast(pl.Int64),
        pl.col("event_type").cast(pl.String),
        pl.col("symbol").cast(pl.String).str.zfill(6),
        pl.col("ex_date").cast(pl.Date),
        pl.col("effective_date").cast(pl.Date),
        pl.col("cash_per_share").cast(pl.Float64).round(12),
        pl.col("share_ratio").cast(pl.Float64).round(12),
        pl.col("source_url").cast(pl.String),
        pl.col("sha256").cast(pl.String),
    )
    for row in samples.iter_rows(named=True):
        digest = str(row["sha256"])
        if (
            not any(str(row["source_url"]).startswith(prefix) for prefix in _OFFICIAL_PREFIXES)
            or len(digest) != 64
            or any(character not in hexdigits for character in digest)
        ):
            raise ValueError("invalid official action evidence identity")
    match_keys = ["year", "event_type", *_ACTION_KEYS]
    if samples.join(indexed, on=match_keys, how="anti").height:
        raise ValueError("official action evidence differs from candidate event")
    expected_groups = set(
        indexed.select("year", "event_type").unique().iter_rows()
    )
    sampled_groups = set(samples.select("year", "event_type").unique().iter_rows())
    return sorted(expected_groups - sampled_groups)
