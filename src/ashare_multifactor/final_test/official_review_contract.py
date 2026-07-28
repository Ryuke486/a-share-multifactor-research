"""Field-level contract for official-evidence human review."""

from __future__ import annotations

import polars as pl

from ashare_multifactor.final_test.execution_contracts import (
    normalize_corporate_action_rows,
    normalize_security_event_rows,
)
from ashare_multifactor.final_test.gate import FINAL_TEST_END
from ashare_multifactor.final_test.official_evidence_workspace import (
    VerifiedReviewQueue,
)


CANDIDATE_FIELDS = (
    "symbol",
    "ex_date",
    "effective_date",
    "cash_per_share",
    "share_ratio",
)
ANNOUNCEMENT_COLUMNS = ("catalog_id", "status", "explanation")
DISPOSITION_COLUMNS = (
    "candidate_id",
    "status",
    "catalog_id",
    "explanation",
    "original_symbol",
    "corrected_symbol",
    "original_ex_date",
    "corrected_ex_date",
    "original_effective_date",
    "corrected_effective_date",
    "original_cash_per_share",
    "corrected_cash_per_share",
    "original_share_ratio",
    "corrected_share_ratio",
    "correction_reason",
)
CORPORATE_FACT_COLUMNS = (
    "catalog_id",
    "candidate_id",
    "announcement_date",
    "symbol",
    "ex_date",
    "effective_date",
    "cash_per_share",
    "share_ratio",
)
SECURITY_FACT_COLUMNS = (
    "catalog_id",
    "effective_date",
    "source_symbol",
    "event_type",
    "target_symbol",
    "ratio",
    "cash_per_share",
    "explanation",
)


def validate_review_frames(
    queue: VerifiedReviewQueue,
    candidates: pl.DataFrame,
    *,
    announcement_decisions: pl.DataFrame,
    corporate_action_dispositions: pl.DataFrame,
    corporate_action_facts: pl.DataFrame,
    security_event_facts: pl.DataFrame,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Reconcile every queue row and candidate to canonical reviewer output."""
    decisions = _announcement_decisions(announcement_decisions, queue.frame)
    relevant = (
        queue.frame.join(decisions, on="catalog_id", how="inner")
        .filter(
            (pl.col("status") == "relevant")
            & (pl.col("document_status") == "cached")
        )
        .select(
            "catalog_id",
            "symbol",
            "source",
            "market",
            "candidate_type",
            "source_url",
            "document_cache_path",
            "document_sha256",
        )
    )
    dispositions = _corporate_dispositions(
        corporate_action_dispositions,
        candidates=candidates,
        relevant=relevant,
    )
    corporate = _corporate_facts(
        corporate_action_facts,
        candidates=candidates,
        dispositions=dispositions,
        relevant=relevant,
    )
    security = _security_facts(security_event_facts, relevant=relevant)
    return decisions, dispositions, corporate, security


def _announcement_decisions(frame: pl.DataFrame, queue: pl.DataFrame) -> pl.DataFrame:
    _require_columns(frame, ANNOUNCEMENT_COLUMNS, label="announcement decisions")
    decisions = frame.select(
        pl.col("catalog_id").cast(pl.String),
        pl.col("status").cast(pl.String),
        pl.col("explanation").cast(pl.String),
    ).sort("catalog_id")
    expected = queue.select("catalog_id")
    if (
        decisions.select(pl.col("catalog_id").is_duplicated().any()).item()
        or decisions.join(expected, on="catalog_id", how="anti").height
        or expected.join(decisions, on="catalog_id", how="anti").height
    ):
        raise ValueError("review queue coverage is not exact")
    if decisions.filter(
        ~pl.col("status").is_in(("relevant", "irrelevant", "unusable"))
        | pl.col("explanation").is_null()
        | pl.col("explanation").str.strip_chars().eq("")
    ).height:
        raise ValueError("announcement review decision is invalid")
    joined = queue.select("catalog_id", "document_status").join(
        decisions,
        on="catalog_id",
        how="inner",
    )
    if joined.filter(
        (pl.col("status").is_in(("relevant", "irrelevant")))
        & (pl.col("document_status") != "cached")
    ).height:
        raise ValueError("non-cached document cannot receive a substantive review")
    return decisions


def _corporate_dispositions(
    frame: pl.DataFrame,
    *,
    candidates: pl.DataFrame,
    relevant: pl.DataFrame,
) -> pl.DataFrame:
    _require_columns(frame, DISPOSITION_COLUMNS, label="corporate dispositions")
    dispositions = frame.select(
        pl.col("candidate_id").cast(pl.String),
        pl.col("status").cast(pl.String),
        pl.col("catalog_id").cast(pl.String),
        pl.col("explanation").cast(pl.String),
        pl.col("original_symbol").cast(pl.String),
        pl.col("corrected_symbol").cast(pl.String),
        *(
            pl.col(name).cast(pl.Date, strict=False)
            for name in (
                "original_ex_date",
                "corrected_ex_date",
                "original_effective_date",
                "corrected_effective_date",
            )
        ),
        *(
            pl.col(name).cast(pl.Float64, strict=False)
            for name in (
                "original_cash_per_share",
                "corrected_cash_per_share",
                "original_share_ratio",
                "corrected_share_ratio",
            )
        ),
        pl.col("correction_reason").cast(pl.String),
    ).sort("candidate_id")
    candidate_ids = candidates.select("candidate_id")
    if (
        dispositions.select(pl.col("candidate_id").is_duplicated().any()).item()
        or dispositions.join(candidate_ids, on="candidate_id", how="anti").height
        or candidate_ids.join(dispositions, on="candidate_id", how="anti").height
    ):
        raise ValueError("corporate-action candidate coverage is not exact")
    if dispositions.filter(
        ~pl.col("status").is_in(("accepted", "corrected", "rejected"))
        | pl.col("explanation").is_null()
        | pl.col("explanation").str.strip_chars().eq("")
    ).height:
        raise ValueError("corporate-action disposition is invalid")
    if dispositions.join(
        relevant.filter(pl.col("candidate_type") == "corporate_action").select(
            "catalog_id"
        ),
        on="catalog_id",
        how="anti",
    ).height:
        raise ValueError("corporate-action disposition lacks a relevant cached document")
    joined = dispositions.join(
        candidates.select(
            "candidate_id",
            *(
                pl.col(name).alias(f"candidate_{name}")
                for name in CANDIDATE_FIELDS
            ),
        ),
        on="candidate_id",
        how="inner",
    ).join(
        relevant.filter(pl.col("candidate_type") == "corporate_action").select(
            "catalog_id",
            pl.col("symbol").alias("evidence_symbol"),
        ),
        on="catalog_id",
        how="inner",
    )
    if joined.filter(
        pl.coalesce("corrected_symbol", "candidate_symbol")
        != pl.col("evidence_symbol")
    ).height:
        raise ValueError("corporate-action disposition evidence symbol differs")
    if joined.filter(
        pl.col("original_symbol").ne_missing(pl.col("candidate_symbol"))
        | pl.col("original_ex_date").ne_missing(pl.col("candidate_ex_date"))
        | pl.col("original_effective_date").ne_missing(
            pl.col("candidate_effective_date")
        )
        | pl.col("original_cash_per_share").ne_missing(
            pl.col("candidate_cash_per_share")
        )
        | pl.col("original_share_ratio").ne_missing(
            pl.col("candidate_share_ratio")
        )
    ).height:
        raise ValueError("corporate-action disposition original fields are false")
    corrected_columns = [
        name for name in DISPOSITION_COLUMNS if name.startswith("corrected_")
    ]
    noncorrected = joined.filter(pl.col("status") != "corrected")
    if noncorrected.filter(
        pl.any_horizontal(pl.col(name).is_not_null() for name in corrected_columns)
        | pl.col("correction_reason").is_not_null()
    ).height:
        raise ValueError("uncorrected candidate contains correction fields")
    corrected = joined.filter(pl.col("status") == "corrected")
    if corrected.filter(
        ~pl.any_horizontal(
            pl.col(name).is_not_null() for name in corrected_columns
        )
        | pl.col("correction_reason").is_null()
        | pl.col("correction_reason").str.strip_chars().eq("")
    ).height:
        raise ValueError("corrected candidate lacks explicit field reconciliation")
    if joined.filter(
        pl.col("candidate_effective_date").is_null()
        & (
            (pl.col("status") == "accepted")
            | (
                (pl.col("status") == "corrected")
                & pl.col("corrected_effective_date").is_null()
            )
        )
    ).height:
        raise ValueError(
            "candidate missing effective date requires correction or rejection"
        )
    return dispositions


def _corporate_facts(
    frame: pl.DataFrame,
    *,
    candidates: pl.DataFrame,
    dispositions: pl.DataFrame,
    relevant: pl.DataFrame,
) -> pl.DataFrame:
    _require_columns(frame, CORPORATE_FACT_COLUMNS, label="corporate-action facts")
    facts = frame.select(
        pl.col("catalog_id").cast(pl.String),
        pl.col("candidate_id").cast(pl.String),
        pl.col("announcement_date").cast(pl.Date, strict=False),
        pl.col("symbol").cast(pl.String).str.zfill(6),
        pl.col("ex_date").cast(pl.Date, strict=False),
        pl.col("effective_date").cast(pl.Date, strict=False),
        pl.col("cash_per_share").cast(pl.Float64, strict=False),
        pl.col("share_ratio").cast(pl.Float64, strict=False),
    ).sort("effective_date", "symbol", "candidate_id")
    evidence = relevant.filter(pl.col("candidate_type") == "corporate_action")
    joined = facts.join(
        evidence.select(
            "catalog_id",
            "source",
            pl.col("symbol").alias("evidence_symbol"),
        ),
        on="catalog_id",
        how="left",
    )
    if joined.filter(pl.col("source").is_null()).height:
        raise ValueError("corporate-action fact lacks a relevant cached document")
    if joined.filter(pl.col("symbol") != pl.col("evidence_symbol")).height:
        raise ValueError("corporate-action fact evidence symbol differs")
    if joined.filter(
        pl.col("announcement_date").is_null()
        | (pl.col("announcement_date") > pl.col("ex_date"))
        | (pl.col("announcement_date") > pl.col("effective_date"))
    ).height:
        raise ValueError("corporate-action announcement date is invalid")
    normalize_corporate_action_rows(
        joined.select(
            "symbol",
            "ex_date",
            "effective_date",
            "cash_per_share",
            "share_ratio",
            "source",
        ),
        maximum_date=FINAL_TEST_END,
    )
    linked = facts.filter(pl.col("candidate_id").is_not_null())
    if linked.select(pl.col("candidate_id").is_duplicated().any()).item():
        raise ValueError("candidate maps to multiple corporate-action facts")
    candidate_ids = candidates.select("candidate_id")
    if linked.join(candidate_ids, on="candidate_id", how="anti").height:
        raise ValueError("corporate-action fact references an unknown candidate")
    publishable = dispositions.filter(
        pl.col("status").is_in(("accepted", "corrected"))
    )
    rejected = dispositions.filter(pl.col("status") == "rejected").select(
        "candidate_id"
    )
    linked_ids = linked.select("candidate_id")
    if (
        publishable.select("candidate_id").join(
            linked_ids, on="candidate_id", how="anti"
        ).height
        or linked_ids.join(
            publishable.select("candidate_id"), on="candidate_id", how="anti"
        ).height
        or linked_ids.join(rejected, on="candidate_id", how="inner").height
    ):
        raise ValueError("candidate disposition does not match corporate-action facts")
    expected = publishable.join(candidates, on="candidate_id", how="inner")
    actual = linked.select(
        "candidate_id",
        *(
            pl.col(name).alias(f"fact_{name}")
            for name in CANDIDATE_FIELDS
        ),
    )
    comparison = expected.join(actual, on="candidate_id", how="inner")
    mismatch = pl.lit(False)
    for field in CANDIDATE_FIELDS:
        corrected = f"corrected_{field}"
        expected_value = (
            pl.coalesce(corrected, field)
            if corrected in comparison.columns
            else pl.col(field)
        )
        mismatch = mismatch | expected_value.ne_missing(pl.col(f"fact_{field}"))
    if comparison.filter(mismatch).height:
        raise ValueError("candidate disposition fields differ from corporate-action fact")
    return facts


def _security_facts(frame: pl.DataFrame, *, relevant: pl.DataFrame) -> pl.DataFrame:
    _require_columns(frame, SECURITY_FACT_COLUMNS, label="security-event facts")
    facts = frame.select(
        pl.col("catalog_id").cast(pl.String),
        pl.col("effective_date").cast(pl.Date, strict=False),
        pl.col("source_symbol").cast(pl.String).str.zfill(6),
        pl.col("event_type").cast(pl.String),
        pl.col("target_symbol").cast(pl.String).str.zfill(6),
        pl.col("ratio").cast(pl.Float64, strict=False),
        pl.col("cash_per_share").cast(pl.Float64, strict=False),
        pl.col("explanation").cast(pl.String),
    ).sort("effective_date", "source_symbol")
    evidence = relevant.filter(
        pl.col("candidate_type").is_in(("stock_merger", "security_event"))
    )
    joined = facts.join(
        evidence.select(
            "catalog_id",
            pl.col("symbol").alias("review_symbol"),
            "source",
        ),
        on="catalog_id",
        how="left",
    )
    if joined.filter(
        pl.col("source").is_null()
        | (pl.col("source_symbol") != pl.col("review_symbol"))
        | pl.col("explanation").is_null()
        | pl.col("explanation").str.strip_chars().eq("")
    ).height:
        raise ValueError("security-event fact lacks a relevant cached document")
    normalize_security_event_rows(
        joined.select(
            "effective_date",
            "source_symbol",
            "event_type",
            "target_symbol",
            "ratio",
            "cash_per_share",
            "source",
            pl.col("catalog_id").alias("evidence_id"),
        )
    )
    if facts.select(pl.col("catalog_id").is_duplicated().any()).item():
        raise ValueError("security-event announcement maps to multiple facts")
    return facts


def _require_columns(
    frame: pl.DataFrame,
    columns: tuple[str, ...],
    *,
    label: str,
) -> None:
    if not isinstance(frame, pl.DataFrame) or tuple(frame.columns) != columns:
        raise ValueError(f"{label} schema is invalid")
