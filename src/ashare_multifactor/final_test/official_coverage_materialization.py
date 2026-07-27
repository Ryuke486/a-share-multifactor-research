"""Pure materialization of paired official execution coverages."""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path

import polars as pl

from ashare_multifactor.data.security import market_for_symbol
from ashare_multifactor.final_test.gate import (
    FINAL_TEST_END,
    FINAL_TEST_START,
    FinalTestAuthorization,
)
from ashare_multifactor.final_test.official_evidence_workspace import (
    EvidenceWorkspace,
    load_verified_review_queue,
)
from ashare_multifactor.final_test.official_query_coverage import canonical_json_bytes
from ashare_multifactor.final_test.official_review_submission import (
    VerifiedReviewSubmission,
)
from ashare_multifactor.final_test.preparation import FinalTestPreparation


COVERAGE_SCHEMA_VERSION = "3"
CORPORATE_MANIFEST = "corporate/coverage.json"
SECURITY_MANIFEST = "security/coverage.json"
_SOURCE_BY_MARKET = {"sh": "sse", "sz": "szse"}


def materialize_execution_coverage_files(
    *,
    symbols: list[str],
    output_root: Path,
    query_index_path: Path,
    query_index_sha256: str,
    workspace: EvidenceWorkspace,
    submission: VerifiedReviewSubmission,
    candidates: pl.DataFrame,
    publication_id: str,
    authorization: FinalTestAuthorization,
    preparation: FinalTestPreparation,
    candidate_manifest_sha256: str,
) -> dict[str, bytes]:
    """Create both manifests and data trees without writing either one."""
    queue = load_verified_review_queue(workspace).frame
    evidence_rows = (
        queue.join(submission.announcement_decisions, on="catalog_id", how="inner")
        .filter(
            (pl.col("status") == "relevant")
            & (pl.col("document_status") == "cached")
        )
        .with_columns(
            pl.struct(
                "source",
                "market",
                "source_url",
                "document_sha256",
            )
            .map_elements(
                lambda row: hashlib.sha256(
                    "|".join(str(row[field]) for field in row).encode()
                ).hexdigest()[:24],
                return_dtype=pl.String,
            )
            .alias("evidence_id"),
            (
                pl.lit(f"{workspace.root.name}/")
                + pl.col("document_cache_path")
            ).alias("cache_file"),
        )
        .select(
            "catalog_id",
            "evidence_id",
            "source",
            "market",
            "source_url",
            "cache_file",
            pl.col("document_sha256").alias("sha256"),
        )
    )
    corporate_facts = submission.corporate_action_facts.join(
        evidence_rows,
        on="catalog_id",
        how="inner",
    )
    dispositions = submission.corporate_action_dispositions.join(
        evidence_rows.select("catalog_id", "evidence_id"),
        on="catalog_id",
        how="inner",
    )
    security_facts = submission.security_event_facts.join(
        evidence_rows,
        on="catalog_id",
        how="inner",
    )
    corporate_evidence = _evidence_index(
        pl.concat(
            (
                corporate_facts.select(
                    "evidence_id",
                    "source",
                    "market",
                    "source_url",
                    "cache_file",
                    "sha256",
                ),
                dispositions.join(
                    evidence_rows.select(
                        "catalog_id",
                        "source",
                        "market",
                        "source_url",
                        "cache_file",
                        "sha256",
                    ),
                    on="catalog_id",
                    how="inner",
                ).select(
                    "evidence_id",
                    "source",
                    "market",
                    "source_url",
                    "cache_file",
                    "sha256",
                ),
            ),
            how="vertical_relaxed",
        )
    )
    security_evidence = _evidence_index(
        security_facts.select(
            "evidence_id",
            "source",
            "market",
            "source_url",
            "cache_file",
            "sha256",
        )
    )
    official_actions = corporate_facts.select(
        "symbol",
        "announcement_date",
        "ex_date",
        "effective_date",
        "cash_per_share",
        "share_ratio",
        "market",
        "source",
        "evidence_id",
        "candidate_id",
    )
    candidate_diff = dispositions.drop("catalog_id").select(
        "candidate_id",
        "status",
        "evidence_id",
        "explanation",
        "original_ex_date",
        "corrected_ex_date",
        "correction_reason",
        "original_symbol",
        "corrected_symbol",
        "original_effective_date",
        "corrected_effective_date",
        "original_cash_per_share",
        "corrected_cash_per_share",
        "original_share_ratio",
        "corrected_share_ratio",
    )
    corporate_coverage = _query_coverage(
        symbols,
        event_counts=official_actions.group_by("symbol").len().rename(
            {"len": "event_count"}
        ),
    )
    events = security_facts.select(
        "effective_date",
        "source_symbol",
        "event_type",
        "target_symbol",
        "ratio",
        "cash_per_share",
        "source",
        "evidence_id",
    )
    security_coverage = _query_coverage(
        symbols,
        event_counts=events.group_by("source_symbol").len().rename(
            {"source_symbol": "symbol", "len": "event_count"}
        ),
    )
    corporate_data = {
        "candidates.parquet": _parquet_bytes(candidates),
        "query_coverage.parquet": _parquet_bytes(corporate_coverage),
        "evidence_index.parquet": _parquet_bytes(corporate_evidence),
        "official_actions.parquet": _parquet_bytes(official_actions),
        "candidate_diff.parquet": _parquet_bytes(candidate_diff),
    }
    security_data = {
        "query_coverage.parquet": _parquet_bytes(security_coverage),
        "evidence_index.parquet": _parquet_bytes(security_evidence),
    }
    if events.height:
        security_data["events.parquet"] = _parquet_bytes(events)
    query_record = {
        "path": query_index_path.relative_to(output_root).as_posix(),
        "role": "official_query_coverage",
        "sha256": query_index_sha256,
        "size_bytes": query_index_path.stat().st_size,
        "root": "attempt_evidence",
    }
    common = {
        "schema_version": COVERAGE_SCHEMA_VERSION,
        "status": "ready",
        "period": [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()],
        "scope": "all_final_execution_symbols",
        "symbol_count": len(symbols),
        "symbols_sha256": preparation.symbols_sha256,
        "publication_id": publication_id,
        "attempt_id": authorization.attempt_id,
        "review_submission_sha256": submission.manifest_sha256,
        "candidate_manifest_sha256": candidate_manifest_sha256,
        "official_query_index_sha256": query_index_sha256,
    }
    corporate_manifest = {
        **common,
        "candidate_file": _record(
            "candidates.parquet",
            corporate_data["candidates.parquet"],
            "baostock_corporate_action_candidates",
        ),
        "query_coverage": _record(
            "query_coverage.parquet",
            corporate_data["query_coverage.parquet"],
            "official_corporate_action_coverage",
        ),
        "evidence_index": _record(
            "evidence_index.parquet",
            corporate_data["evidence_index.parquet"],
            "official_corporate_action_evidence_index",
        ),
        "official_actions": _record(
            "official_actions.parquet",
            corporate_data["official_actions.parquet"],
            "official_corporate_actions",
        ),
        "candidate_diff": _record(
            "candidate_diff.parquet",
            corporate_data["candidate_diff.parquet"],
            "corporate_action_candidate_diff",
        ),
        "official_query_coverage": query_record,
    }
    security_manifest = {
        **common,
        "event_rows": events.height,
        "evidence_index": _record(
            "evidence_index.parquet",
            security_data["evidence_index.parquet"],
            "official_security_event_evidence_index",
        ),
        "coverage": [
            _record(
                "query_coverage.parquet",
                security_data["query_coverage.parquet"],
                "official_security_event_coverage",
            )
        ],
        "official_query_coverage": query_record,
    }
    if events.height:
        security_manifest["events_file"] = _record(
            "events.parquet",
            security_data["events.parquet"],
            "official_security_events",
        )
    return {
        **{f"corporate/{name}": payload for name, payload in corporate_data.items()},
        CORPORATE_MANIFEST: canonical_json_bytes(corporate_manifest),
        **{f"security/{name}": payload for name, payload in security_data.items()},
        SECURITY_MANIFEST: canonical_json_bytes(security_manifest),
    }


def _query_coverage(
    symbols: list[str],
    *,
    event_counts: pl.DataFrame,
) -> pl.DataFrame:
    base = pl.DataFrame({"symbol": symbols}).with_columns(
        pl.col("symbol")
        .map_elements(market_for_symbol, return_dtype=pl.String)
        .alias("market")
    )
    return (
        base.with_columns(
            pl.col("market").replace_strict(_SOURCE_BY_MARKET).alias("source"),
            pl.lit(FINAL_TEST_START).alias("query_start"),
            pl.lit(FINAL_TEST_END).alias("query_end"),
            pl.lit("ok").alias("status"),
        )
        .join(event_counts, on="symbol", how="left")
        .with_columns(pl.col("event_count").fill_null(0).cast(pl.Int64))
        .select(
            "symbol",
            "source",
            "market",
            "query_start",
            "query_end",
            "status",
            "event_count",
        )
        .sort("symbol")
    )


def _evidence_index(frame: pl.DataFrame) -> pl.DataFrame:
    schema = {
        "evidence_id": pl.String,
        "source": pl.String,
        "market": pl.String,
        "source_url": pl.String,
        "cache_file": pl.String,
        "sha256": pl.String,
    }
    if not frame.height:
        return pl.DataFrame(schema=schema)
    result = frame.select(
        pl.col(name).cast(dtype) for name, dtype in schema.items()
    ).unique(subset=["evidence_id"], maintain_order=True)
    if result.select(pl.col("evidence_id").is_duplicated().any()).item():
        raise ValueError("official evidence identity is duplicated")
    return result.sort("evidence_id")


def _record(path: str, payload: bytes, role: str) -> dict[str, object]:
    return {
        "path": path,
        "role": role,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }


def _parquet_bytes(frame: pl.DataFrame) -> bytes:
    stream = BytesIO()
    frame.write_parquet(stream, compression="zstd")
    return stream.getvalue()
