"""Fail-closed admission of provider candidates to official human review."""

from __future__ import annotations

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
    minimum_lag, maximum_lag = _lag_interval(date_rule)
    unresolved = _unresolved_candidates(
        verified_candidates.candidates,
        evaluated=evaluated,
        minimum_lag=minimum_lag,
        maximum_lag=maximum_lag,
    )
    unresolved_bytes = _parquet_bytes(unresolved)
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
        },
        "candidate_count": verified_candidates.candidates.height,
        "admitted_count": verified_candidates.candidates.height - unresolved.height,
        "unresolved_count": unresolved.height,
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
    admission = _publish_admission(
        workspace.root.parent,
        manifest=manifest,
        unresolved_bytes=unresolved_bytes,
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
        != "stage9_candidate_review_admission_date_rule/v1"
        or payload.get("role") != "candidate_review_admission_date_rule"
        or payload.get("period") != ["2017-01-01", "2021-12-31"]
        or payload.get("final_test_strategy_outputs_read") is not False
        or payload.get("final_test_candidate_gaps_read") is not False
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


def _evaluate_announcements(
    workspace: EvidenceWorkspace,
    queue: VerifiedReviewQueue,
) -> dict[str, list[dict[str, object]]]:
    by_symbol: dict[str, list[dict[str, object]]] = {}
    documents_root = workspace.root / DOCUMENTS_DIRECTORY
    descriptor = os.open(
        documents_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        for row in queue.frame.sort("catalog_id").iter_rows(named=True):
            strong = (
                row["route"] == "candidate"
                and row["candidate_type"] == "corporate_action"
                and str(row["routing_reason"]).startswith(_STRONG_REASON_PREFIX)
            )
            valid_pdf = False
            normalized_text = ""
            if strong and row["document_status"] == "cached":
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
                            normalized_text = _normalized_pdf_text(payload)
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
                }
            )
    finally:
        os.close(descriptor)
    return by_symbol


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
    year, month, day = value.year, value.month, value.day
    variants = {
        value.isoformat(),
        f"{year}/{month}/{day}",
        f"{year}/{month:02d}/{day:02d}",
        f"{year}.{month}.{day}",
        f"{year}.{month:02d}.{day:02d}",
        f"{year}年{month}月{day}日",
        f"{year}年{month:02d}月{day:02d}日",
    }
    return any(variant in text for variant in variants)


def _unresolved_candidates(
    candidates: pl.DataFrame,
    *,
    evaluated: dict[str, list[dict[str, object]]],
    minimum_lag: int,
    maximum_lag: int,
) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
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
            if _date_in_text(ex_date, str(row["normalized_text"]))
        ]
        window_hits = [
            row
            for row in valid
            if minimum_lag
            <= (ex_date - row["announcement_date"]).days
            <= maximum_lag
        ]
        admitted = [
            row
            for row in date_matches
            if minimum_lag
            <= (ex_date - row["announcement_date"]).days
            <= maximum_lag
        ]
        if admitted:
            continue
        if not same_symbol:
            primary = "no_same_symbol_announcement"
        elif not strong:
            primary = "no_strong_implementation_route"
        elif not valid:
            primary = "no_valid_cached_official_pdf"
        elif not date_matches:
            primary = "candidate_date_not_found_in_pdf"
        elif not window_hits:
            primary = "outside_frozen_date_window"
        else:
            primary = None
        failure_codes = (
            [primary, "no_single_announcement_satisfies_all_requirements"]
            if primary is not None
            else ["no_single_announcement_satisfies_all_requirements"]
        )
        rows.append(
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
                    str(row["catalog_id"]) for row in same_symbol
                ),
            }
        )
    return pl.DataFrame(rows, schema=_UNRESOLVED_SCHEMA, strict=True).sort(
        "candidate_id"
    )
