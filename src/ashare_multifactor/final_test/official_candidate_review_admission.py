"""Fail-closed admission of provider candidates to official human review."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import unicodedata
from typing import Any

import polars as pl
from pypdf import PdfReader

from ashare_multifactor.final_test.official_announcement_catalog import (
    load_verified_announcement_catalog,
)
from ashare_multifactor.final_test.official_announcement_routing import (
    load_verified_announcement_routing,
)
from ashare_multifactor.final_test.official_candidate_pdf_evidence import (
    date_in_text,
    inspect_candidate_pdf,
)
from ashare_multifactor.final_test.official_document_validation import (
    validate_official_document,
)
from ashare_multifactor.final_test.official_candidate_review_admission_storage import (
    ADMISSION_DIRECTORY,
    ADMISSION_MANIFEST_NAME,
    DECISIONS_NAME,
    UNRESOLVED_NAME,
    CandidateReviewAdmissionError,
    VerifiedCandidateReviewAdmission,
    _UNRESOLVED_SCHEMA,
    parquet_bytes as _parquet_bytes,
    publish_candidate_review_admission as _publish_admission,
)
from ashare_multifactor.final_test.official_evidence_workspace import (
    DOCUMENTS_DIRECTORY,
    EvidenceWorkspace,
    VerifiedReviewQueue,
    load_cached_document,
    load_verified_review_queue,
)
from ashare_multifactor.final_test.official_query_coverage import canonical_json_bytes
from ashare_multifactor.final_test.official_review_submission import (
    VerifiedCandidateSnapshot,
    load_verified_candidate_snapshot,
)


__all__ = [
    "ADMISSION_DIRECTORY",
    "ADMISSION_MANIFEST_NAME",
    "DECISIONS_NAME",
    "UNRESOLVED_NAME",
    "CandidateReviewAdmissionError",
    "CandidateReviewTopology",
    "recompute_candidate_review_topology",
    "VerifiedCandidateReviewAdmission",
    "require_candidate_review_admission",
    "require_historical_candidate_review_admission",
]


DEFAULT_ADMISSION_DATE_RULE_PATH = (
    Path(__file__).resolve().parents[3]
    / "configs/evidence/stage9_candidate_review_admission_date_rule.json"
)

_SCHEMA_VERSION = "1"
_RULE_VERSION = "stage9-shared-routing-v5"
_STRONG_REASON_PREFIX = "corporate_action_strong_implementation:"
_SOURCE_DATE = re.compile(r"/finalpage/(\d{4}-\d{2}-\d{2})/")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TEMPORAL_POLICY = {
    "predicate": "publication_date_lte_candidate_ex_date",
    "publication_date_source": "cninfo_finalpage_path_date",
    "same_day_allowed": True,
    "historical_lag_interval_usage": "diagnostic_only",
    "final_test_gap_distribution_used_to_select_predicate": False,
}
_DECISION_SCHEMA = {
    "candidate_id": pl.String,
    "symbol": pl.String,
    "ex_date": pl.Date,
    "effective_date": pl.Date,
    "status": pl.String,
    "evidence_kind": pl.String,
    "catalog_id": pl.String,
    "source_url": pl.String,
    "document_sha256": pl.String,
    "evidence_manifest_sha256": pl.String,
    "announcement_publication_date": pl.Date,
    "strong_reason": pl.String,
    "date_match": pl.String,
    "historical_lag_interval_hit": pl.Boolean,
}


@dataclass(frozen=True)
class _TemporalAdmissionDecision:
    eligible: bool
    historical_lag_interval_hit: bool


@dataclass(frozen=True)
class CandidateReviewTopology:
    """Current causal result for an exact subset of frozen candidates."""

    unresolved: pl.DataFrame
    decisions: pl.DataFrame
    query_required_candidate_ids: tuple[str, ...]
    rendition_required_candidate_ids: tuple[str, ...]
    unclassified_candidate_ids: tuple[str, ...]
    binding: dict[str, object]


def recompute_candidate_review_topology(
    *,
    workspace: EvidenceWorkspace,
    queue: VerifiedReviewQueue,
    candidates: VerifiedCandidateSnapshot,
    candidate_ids: tuple[str, ...],
    date_rule_path: Path = DEFAULT_ADMISSION_DATE_RULE_PATH,
) -> CandidateReviewTopology:
    """Re-evaluate an exact frozen subset under the current admission rules."""
    verified_queue = load_verified_review_queue(workspace)
    verified_candidates = load_verified_candidate_snapshot(
        candidates.manifest_path,
        workspace=workspace,
    )
    if (
        queue.manifest_sha256 != verified_queue.manifest_sha256
        or not queue.frame.equals(verified_queue.frame)
        or candidates.manifest_sha256 != verified_candidates.manifest_sha256
        or not candidates.candidates.equals(verified_candidates.candidates)
        or not candidate_ids
        or candidate_ids != tuple(sorted(set(candidate_ids)))
    ):
        raise ValueError("candidate review topology inputs are not verified")
    subset = verified_candidates.candidates.filter(
        pl.col("candidate_id").is_in(candidate_ids)
    )
    if (
        subset.height != len(candidate_ids)
        or tuple(subset.sort("candidate_id").get_column("candidate_id"))
        != candidate_ids
    ):
        raise ValueError("candidate review topology candidate set differs")
    date_rule, _ = _load_date_rule(date_rule_path)
    _load_routing(workspace)
    symbols = frozenset(subset.get_column("symbol").to_list())
    evaluated = _evaluate_announcements(
        workspace,
        verified_queue,
        symbols=symbols,
    )
    _extend_with_verified_renditions(
        workspace,
        verified_queue,
        evaluated=evaluated,
    )
    minimum_lag, maximum_lag = _lag_interval(date_rule)
    unresolved, decisions = _candidate_results(
        subset,
        evaluated=evaluated,
        minimum_lag=minimum_lag,
        maximum_lag=maximum_lag,
    )
    query_ids = _failure_candidate_ids(
        unresolved,
        primary="no_same_symbol_announcement",
    )
    rendition_ids = tuple(
        sorted(
            str(row["candidate_id"])
            for row in unresolved.iter_rows(named=True)
            if (
                list(row["failure_codes"])[0]
                == "candidate_date_not_found_in_pdf"
                and int(row["strong_implementation_announcement_count"]) > 0
                and int(row["valid_cached_official_pdf_count"]) > 0
            )
        )
    )
    unresolved_ids = set(
        unresolved.get_column("candidate_id").to_list()
    )
    classified_ids = set(query_ids) | set(rendition_ids)
    unclassified_ids = tuple(sorted(unresolved_ids - classified_ids))
    if (
        set(query_ids).intersection(rendition_ids)
        or classified_ids.intersection(unclassified_ids)
        or unresolved_ids
        != classified_ids | set(unclassified_ids)
    ):
        raise ValueError("candidate review topology partition differs")
    unresolved_bytes = _parquet_bytes(unresolved)
    decisions_bytes = _parquet_bytes(decisions)
    core: dict[str, object] = {
        "schema": "stage9_candidate_review_causal_topology/v1",
        "role": "candidate_review_causal_topology",
        "rule_version": _RULE_VERSION,
        "candidate_ids": list(candidate_ids),
        "candidate_count": len(candidate_ids),
        "admitted_count": decisions.height,
        "unresolved_count": unresolved.height,
        "decisions_sha256": hashlib.sha256(decisions_bytes).hexdigest(),
        "unresolved_sha256": hashlib.sha256(unresolved_bytes).hexdigest(),
        "query_required_candidate_ids": list(query_ids),
        "rendition_required_candidate_ids": list(rendition_ids),
        "unclassified_candidate_ids": list(unclassified_ids),
        "final_test_strategy_outputs_read": False,
    }
    binding = {
        **core,
        "identity_sha256": hashlib.sha256(
            canonical_json_bytes(core)
        ).hexdigest(),
    }
    return CandidateReviewTopology(
        unresolved=unresolved,
        decisions=decisions,
        query_required_candidate_ids=query_ids,
        rendition_required_candidate_ids=rendition_ids,
        unclassified_candidate_ids=unclassified_ids,
        binding=binding,
    )


def _failure_candidate_ids(
    unresolved: pl.DataFrame,
    *,
    primary: str,
) -> tuple[str, ...]:
    return tuple(
        sorted(
            str(row["candidate_id"])
            for row in unresolved.iter_rows(named=True)
            if list(row["failure_codes"])[0] == primary
        )
    )


def require_candidate_review_admission(
    *,
    workspace: EvidenceWorkspace,
    queue: VerifiedReviewQueue,
    candidates: VerifiedCandidateSnapshot,
    date_rule_path: Path,
) -> VerifiedCandidateReviewAdmission:
    """Publish and verify exact candidate closure, raising after blocked output."""
    verified_queue = load_verified_review_queue(workspace)
    verified_candidates = load_verified_candidate_snapshot(
        candidates.manifest_path,
        workspace=workspace,
    )
    if (
        queue.manifest_sha256 != verified_queue.manifest_sha256
        or not queue.frame.equals(verified_queue.frame)
        or candidates.manifest_sha256 != verified_candidates.manifest_sha256
        or not candidates.candidates.equals(verified_candidates.candidates)
    ):
        raise ValueError("candidate review admission inputs are not verified")
    date_rule, date_rule_bytes = _load_date_rule(date_rule_path)
    routing = _load_routing(workspace)
    evaluated = _evaluate_announcements(workspace, verified_queue)
    rendition_identity = _extend_with_verified_renditions(
        workspace,
        verified_queue,
        evaluated=evaluated,
    )
    minimum_lag, maximum_lag = _lag_interval(date_rule)
    unresolved, decisions = _candidate_results(
        verified_candidates.candidates,
        evaluated=evaluated,
        minimum_lag=minimum_lag,
        maximum_lag=maximum_lag,
    )
    unresolved_bytes = _parquet_bytes(unresolved)
    decisions_bytes = _parquet_bytes(decisions)
    routing_identity = {
        "routing_sha256": routing.routing_sha256,
        "rule_version": routing.manifest["rule_version"],
        "rules_sha256": routing.manifest["rules_sha256"],
    }
    derivation = date_rule["derivation"]
    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "role": "candidate_review_admission",
        "status": "ready" if unresolved.is_empty() else "blocked",
        "attempt_id": workspace.root.parent.name,
        "review_session_id": verified_queue.session_id,
        "review_queue": {
            "relative_path": verified_queue.manifest_path.relative_to(
                workspace.root.parent
            ).as_posix(),
            "manifest_sha256": verified_queue.manifest_sha256,
        },
        "candidate_snapshot": {
            "relative_path": verified_candidates.manifest_path.relative_to(
                workspace.root.parent
            ).as_posix(),
            "manifest_sha256": verified_candidates.manifest_sha256,
        },
        "routing": {
            **routing_identity,
            "identity_sha256": hashlib.sha256(
                canonical_json_bytes(routing_identity)
            ).hexdigest(),
        },
        "date_rule": {
            "path": "configs/evidence/"
            "stage9_candidate_review_admission_date_rule.json",
            "sha256": hashlib.sha256(date_rule_bytes).hexdigest(),
            "derivation_manifest_sha256": derivation["manifest_sha256"],
            "derivation_parquet_sha256": derivation["parquet_sha256"],
            "period": date_rule["period"],
            "minimum_calendar_days": minimum_lag,
            "maximum_calendar_days": maximum_lag,
            "interval_closed": True,
            "temporal_policy": date_rule["temporal_policy"],
        },
        "candidate_count": verified_candidates.candidates.height,
        "admitted_count": verified_candidates.candidates.height - unresolved.height,
        "unresolved_count": unresolved.height,
        "candidate_decisions": {
            "path": DECISIONS_NAME,
            "sha256": hashlib.sha256(decisions_bytes).hexdigest(),
            "size_bytes": len(decisions_bytes),
            "row_count": decisions.height,
        },
        "unresolved_candidates": {
            "path": UNRESOLVED_NAME,
            "sha256": hashlib.sha256(unresolved_bytes).hexdigest(),
            "size_bytes": len(unresolved_bytes),
            "row_count": unresolved.height,
        },
        "text_extraction": {
            "engine": "pypdf",
            "normalization": "nfkc_remove_unicode_whitespace_v1",
        },
        "final_test_strategy_outputs_read": False,
    }
    if rendition_identity is not None:
        manifest["evidence_renditions"] = rendition_identity
    admission = _publish_admission(
        workspace.root.parent,
        manifest=manifest,
        unresolved_bytes=unresolved_bytes,
        decisions_bytes=decisions_bytes,
    )
    if not admission.ready:
        raise CandidateReviewAdmissionError(admission)
    return admission


def require_historical_candidate_review_admission(
    *,
    code_root: Path,
    derivation_path: Path,
    derivation_manifest_path: Path,
    discovery_path: Path,
    receipt_index_path: Path,
    existing_inventory_path: Path,
    date_rule_path: Path,
    pdf_cache_root: Path,
    destination: Path,
) -> VerifiedCandidateReviewAdmission:
    """Recompute the frozen 2017-2021 candidate/PDF pairs for rehearsal."""
    from ashare_multifactor.final_test.official_candidate_review_admission_historical import (
        run_historical_candidate_review_admission,
    )

    return run_historical_candidate_review_admission(
        code_root=code_root,
        derivation_path=derivation_path,
        derivation_manifest_path=derivation_manifest_path,
        discovery_path=discovery_path,
        receipt_index_path=receipt_index_path,
        existing_inventory_path=existing_inventory_path,
        date_rule_path=date_rule_path,
        pdf_cache_root=pdf_cache_root,
        destination=destination,
    )


def _load_routing(workspace: EvidenceWorkspace):
    catalog = load_verified_announcement_catalog(workspace.catalog_path)
    routing = load_verified_announcement_routing(
        workspace.routing_path,
        catalog=catalog,
    )
    if routing.manifest.get("rule_version") != _RULE_VERSION:
        raise ValueError("candidate review admission routing version differs")
    return routing


def _load_date_rule(
    path: Path,
    *,
    expected_path: Path | None = None,
) -> tuple[dict[str, Any], bytes]:
    absolute = path.resolve()
    expected = (
        DEFAULT_ADMISSION_DATE_RULE_PATH
        if expected_path is None
        else expected_path
    ).resolve()
    if absolute != expected or absolute.is_symlink() or not absolute.is_file():
        raise ValueError("candidate review admission date rule path is invalid")
    raw = absolute.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("candidate review admission date rule is invalid") from error
    derivation = payload.get("derivation") if isinstance(payload, dict) else None
    lag = payload.get("lag") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or raw != canonical_json_bytes(payload)
        or payload.get("schema")
        != "stage9_candidate_review_admission_date_rule/v2"
        or payload.get("role") != "candidate_review_admission_date_rule"
        or payload.get("period") != ["2017-01-01", "2021-12-31"]
        or payload.get("final_test_strategy_outputs_read") is not False
        or payload.get("final_test_candidate_gaps_read") is not False
        or payload.get("temporal_policy") != _TEMPORAL_POLICY
        or not isinstance(derivation, dict)
        or _SHA256.fullmatch(str(derivation.get("manifest_sha256", ""))) is None
        or _SHA256.fullmatch(str(derivation.get("parquet_sha256", ""))) is None
        or not isinstance(lag, dict)
        or lag.get("definition")
        != "ex_date_minus_announcement_date_in_calendar_days"
        or lag.get("interval_closed") is not True
    ):
        raise ValueError("candidate review admission date rule is invalid")
    _lag_interval(payload)
    return payload, raw


def _lag_interval(date_rule: dict[str, Any]) -> tuple[int, int]:
    lag = date_rule["lag"]
    minimum = lag.get("minimum_calendar_days")
    maximum = lag.get("maximum_calendar_days")
    if (
        not isinstance(minimum, int)
        or isinstance(minimum, bool)
        or not isinstance(maximum, int)
        or isinstance(maximum, bool)
        or minimum < 0
        or maximum < minimum
    ):
        raise ValueError("candidate review admission date interval is invalid")
    return minimum, maximum


def _temporal_admission_decision(
    *,
    candidate_ex_date: date,
    announcement_publication_date: date,
    minimum_lag: int,
    maximum_lag: int,
) -> _TemporalAdmissionDecision:
    lag = (candidate_ex_date - announcement_publication_date).days
    return _TemporalAdmissionDecision(
        eligible=announcement_publication_date <= candidate_ex_date,
        historical_lag_interval_hit=minimum_lag <= lag <= maximum_lag,
    )


def _evaluate_announcements(
    workspace: EvidenceWorkspace,
    queue: VerifiedReviewQueue,
    *,
    symbols: frozenset[str] | None = None,
) -> dict[str, list[dict[str, object]]]:
    by_symbol: dict[str, list[dict[str, object]]] = {}
    documents_root = workspace.root / DOCUMENTS_DIRECTORY
    descriptor = os.open(
        documents_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        for row in queue.frame.sort("catalog_id").iter_rows(named=True):
            if symbols is not None and str(row["symbol"]) not in symbols:
                continue
            corporate_action_candidate = (
                row["route"] == "candidate"
                and row["candidate_type"] == "corporate_action"
            )
            strong = (
                corporate_action_candidate
                and str(row["routing_reason"]).startswith(_STRONG_REASON_PREFIX)
            )
            valid_pdf = False
            normalized_text = ""
            document_sha256 = ""
            strong_reason = (
                str(row["routing_reason"]) if strong else ""
            )
            if corporate_action_candidate and row["document_status"] == "cached":
                try:
                    cached = load_cached_document(
                        descriptor,
                        source_url=str(row["source_url"]),
                    )
                    if cached is not None and _cache_metadata_matches(row, cached):
                        payload = _read_cache_payload(workspace, cached.cache_path)
                        validated = validate_official_document(
                            payload,
                            source_url=cached.source_url,
                        )
                        if (
                            validated.sha256 == cached.sha256
                            and validated.size_bytes == cached.size_bytes
                            and validated.page_count == cached.page_count
                            and validated.media_type == cached.media_type
                        ):
                            valid_pdf = True
                            document_sha256 = validated.sha256
                            evidence = inspect_candidate_pdf(payload)
                            normalized_text = evidence.text
                            if not strong:
                                strong = (
                                    evidence.strong_reason is not None
                                    and evidence.contains_symbol(str(row["symbol"]))
                                )
                                if strong:
                                    strong_reason = str(evidence.strong_reason)
                except (FileNotFoundError, OSError, ValueError):
                    valid_pdf = False
            by_symbol.setdefault(str(row["symbol"]), []).append(
                {
                    "catalog_id": str(row["catalog_id"]),
                    "strong": strong,
                    "valid_pdf": valid_pdf,
                    "normalized_text": normalized_text,
                    "announcement_date": _announcement_date(
                        str(row["source_url"])
                    ),
                    "source_url": str(row["source_url"]),
                    "document_sha256": document_sha256,
                    "evidence_kind": "canonical_cninfo",
                    "evidence_manifest_sha256": "",
                    "strong_reason": strong_reason,
                    "labelled_dates": (),
                }
            )
    finally:
        os.close(descriptor)
    return by_symbol


def derive_unique_candidate_rendition_anchor(
    workspace: EvidenceWorkspace,
    queue: VerifiedReviewQueue,
    *,
    symbol: str,
    related_catalog_ids: tuple[str, ...],
    required_publication_date: date,
) -> dict[str, object]:
    """Return the sole same-date canonical PDF that passes admission semantics."""
    if (
        not related_catalog_ids
        or len(set(related_catalog_ids)) != len(related_catalog_ids)
        or not isinstance(required_publication_date, date)
    ):
        raise ValueError("candidate rendition related catalog IDs differ")
    related = queue.frame.filter(
        pl.col("catalog_id").is_in(related_catalog_ids)
    )
    if (
        related.height != len(related_catalog_ids)
        or set(related.get_column("catalog_id")) != set(related_catalog_ids)
        or set(related.get_column("symbol")) != {symbol}
    ):
        raise ValueError("candidate rendition related catalog IDs differ")
    evaluated = _evaluate_announcements(
        workspace,
        queue,
        symbols=frozenset({symbol}),
    ).get(symbol, [])
    eligible_ids = {
        str(row["catalog_id"])
        for row in evaluated
        if row["catalog_id"] in related_catalog_ids
        and row["evidence_kind"] == "canonical_cninfo"
        and row["strong"]
        and row["valid_pdf"]
        and row["announcement_date"] == required_publication_date
    }
    anchors = related.filter(pl.col("catalog_id").is_in(eligible_ids)).to_dicts()
    if len(anchors) != 1:
        raise ValueError("candidate rendition authorization anchor differs")
    return anchors[0]


def _extend_with_verified_renditions(
    workspace: EvidenceWorkspace,
    queue: VerifiedReviewQueue,
    *,
    evaluated: dict[str, list[dict[str, object]]],
) -> dict[str, object] | None:
    binding = queue.manifest.get("candidate_review_renditions")
    if binding is None:
        return None
    if not isinstance(binding, dict):
        raise ValueError("candidate review rendition binding is invalid")
    from ashare_multifactor.final_test.official_candidate_review_renditions import (
        load_verified_candidate_review_renditions,
    )

    relative = str(binding.get("relative_path", ""))
    manifest_path = workspace.root.parent / relative
    renditions = load_verified_candidate_review_renditions(
        manifest_path,
        workspace=workspace,
        queue=queue,
    )
    if (
        binding.get("manifest_sha256") != renditions.manifest_sha256
        or binding.get("record_count") != len(renditions.records)
    ):
        raise ValueError("candidate review rendition binding differs")
    for record in renditions.records:
        canonical = record["canonical_announcement"]
        document = record["document"]
        symbol = str(canonical["symbol"])
        strong_reason = record.get("strong_reason")
        evaluated.setdefault(symbol, []).append(
            {
                "catalog_id": str(canonical["catalog_id"]),
                "strong": (
                    isinstance(strong_reason, str)
                    and bool(strong_reason)
                ),
                "valid_pdf": True,
                "normalized_text": "",
                "announcement_date": date.fromisoformat(
                    str(record["publication_date"])
                ),
                "source_url": str(record["source_url"]),
                "document_sha256": str(document["sha256"]),
                "evidence_kind": "publisher_rendition",
                "evidence_manifest_sha256": renditions.manifest_sha256,
                "strong_reason": (
                    strong_reason if isinstance(strong_reason, str) else ""
                ),
                "labelled_dates": tuple(
                    date.fromisoformat(str(value))
                    for value in record["labelled_dates"]
                ),
            }
        )
    return {
        "relative_path": relative,
        "manifest_sha256": renditions.manifest_sha256,
        "record_count": len(renditions.records),
    }


def _cache_metadata_matches(row: dict[str, object], cached: object) -> bool:
    return (
        row["document_cache_path"] == cached.cache_path
        and row["document_sha256"] == cached.sha256
        and row["document_size_bytes"] == cached.size_bytes
        and row["document_page_count"] == cached.page_count
        and row["document_media_type"] == cached.media_type
    )


def _read_cache_payload(workspace: EvidenceWorkspace, relative: str) -> bytes:
    path = workspace.root / relative
    if (
        path.is_symlink()
        or not path.is_file()
        or path.resolve().parent.parent.parent != workspace.root.resolve()
    ):
        raise ValueError("candidate review admission cache path is invalid")
    return path.read_bytes()


def _announcement_date(source_url: str) -> date:
    match = _SOURCE_DATE.search(source_url)
    if match is None:
        raise ValueError("candidate review admission announcement date is invalid")
    try:
        return date.fromisoformat(match.group(1))
    except ValueError as error:
        raise ValueError(
            "candidate review admission announcement date is invalid"
        ) from error


def _normalized_pdf_text(payload: bytes) -> str:
    try:
        text = "".join(page.extract_text() or "" for page in PdfReader(BytesIO(payload)).pages)
    except (EOFError, OSError, TypeError, ValueError):
        return ""
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", text)
        if not character.isspace()
    )


def _date_in_text(value: date, text: str) -> bool:
    return date_in_text(value, text)


def _candidate_results(
    candidates: pl.DataFrame,
    *,
    evaluated: dict[str, list[dict[str, object]]],
    minimum_lag: int,
    maximum_lag: int,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    unresolved_rows: list[dict[str, object]] = []
    decision_rows: list[dict[str, object]] = []
    for candidate in candidates.sort("candidate_id").iter_rows(named=True):
        ex_date = candidate["ex_date"]
        if not isinstance(ex_date, date):
            raise ValueError("candidate review admission candidate date is invalid")
        same_symbol = evaluated.get(str(candidate["symbol"]), [])
        strong = [row for row in same_symbol if row["strong"]]
        valid = [row for row in strong if row["valid_pdf"]]
        date_matches = [
            row
            for row in valid
            if (
                ex_date in row["labelled_dates"]
                if row["evidence_kind"] == "publisher_rendition"
                else _date_in_text(ex_date, str(row["normalized_text"]))
            )
        ]
        window_hits = [
            row
            for row in valid
            if _temporal_admission_decision(
                candidate_ex_date=ex_date,
                announcement_publication_date=row["announcement_date"],
                minimum_lag=minimum_lag,
                maximum_lag=maximum_lag,
            ).historical_lag_interval_hit
        ]
        causal_matches = [
            row
            for row in date_matches
            if _temporal_admission_decision(
                candidate_ex_date=ex_date,
                announcement_publication_date=row["announcement_date"],
                minimum_lag=minimum_lag,
                maximum_lag=maximum_lag,
            ).eligible
        ]
        if causal_matches:
            selected = sorted(
                causal_matches,
                key=lambda row: (
                    str(row["evidence_kind"]),
                    str(row["catalog_id"]),
                    str(row["source_url"]),
                ),
            )[0]
            temporal = _temporal_admission_decision(
                candidate_ex_date=ex_date,
                announcement_publication_date=selected["announcement_date"],
                minimum_lag=minimum_lag,
                maximum_lag=maximum_lag,
            )
            decision_rows.append(
                {
                    "candidate_id": str(candidate["candidate_id"]),
                    "symbol": str(candidate["symbol"]),
                    "ex_date": ex_date,
                    "effective_date": candidate["effective_date"],
                    "status": "admitted",
                    "evidence_kind": str(selected["evidence_kind"]),
                    "catalog_id": str(selected["catalog_id"]),
                    "source_url": str(selected["source_url"]),
                    "document_sha256": str(selected["document_sha256"]),
                    "evidence_manifest_sha256": str(
                        selected["evidence_manifest_sha256"]
                    ),
                    "announcement_publication_date": selected[
                        "announcement_date"
                    ],
                    "strong_reason": str(selected["strong_reason"]),
                    "date_match": (
                        "labelled_exact_date_in_bounded_block"
                        if selected["evidence_kind"] == "publisher_rendition"
                        else "exact_date_in_pdf"
                    ),
                    "historical_lag_interval_hit": (
                        temporal.historical_lag_interval_hit
                    ),
                }
            )
            continue
        if not same_symbol:
            primary = "no_same_symbol_announcement"
        elif not strong:
            primary = "no_strong_implementation_route"
        elif not valid:
            primary = "no_valid_cached_official_pdf"
        elif not date_matches:
            primary = "candidate_date_not_found_in_pdf"
        else:
            primary = "announcement_after_candidate_ex_date"
        failure_codes = [
            primary,
            "no_single_announcement_satisfies_all_requirements",
        ]
        unresolved_rows.append(
            {
                "candidate_id": str(candidate["candidate_id"]),
                "symbol": str(candidate["symbol"]),
                "ex_date": ex_date,
                "effective_date": candidate["effective_date"],
                "failure_codes": failure_codes,
                "same_symbol_announcement_count": len(same_symbol),
                "strong_implementation_announcement_count": len(strong),
                "valid_cached_official_pdf_count": len(valid),
                "frozen_date_window_hit_count": len(window_hits),
                "related_catalog_ids": sorted(
                    {
                        str(row["catalog_id"])
                        for row in same_symbol
                    }
                ),
            }
        )
    unresolved = pl.DataFrame(
        unresolved_rows,
        schema=_UNRESOLVED_SCHEMA,
        strict=True,
    ).sort("candidate_id")
    decisions = pl.DataFrame(
        decision_rows,
        schema=_DECISION_SCHEMA,
        strict=True,
    ).sort("candidate_id")
    return unresolved, decisions


def _unresolved_candidates(
    candidates: pl.DataFrame,
    *,
    evaluated: dict[str, list[dict[str, object]]],
    minimum_lag: int,
    maximum_lag: int,
) -> pl.DataFrame:
    """Compatibility seam retained for focused admission tests."""
    return _candidate_results(
        candidates,
        evaluated=evaluated,
        minimum_lag=minimum_lag,
        maximum_lag=maximum_lag,
    )[0]
