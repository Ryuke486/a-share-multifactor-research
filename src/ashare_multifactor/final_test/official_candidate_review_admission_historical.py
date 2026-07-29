"""Historical-input adapter for the candidate review admission deep module."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ashare_multifactor.final_test import official_announcement_routing as routing
from ashare_multifactor.final_test.official_candidate_review_admission import (
    _RULE_VERSION,
    _lag_interval,
    _load_date_rule,
)
from ashare_multifactor.final_test.official_candidate_review_admission_historical_evaluation import (
    evaluate_historical_candidate_pairs,
)
from ashare_multifactor.final_test.official_candidate_review_admission_historical_inputs import (
    HISTORICAL_PERIOD,
    historical_cache_record,
    historical_file_record,
    load_verified_historical_admission_inputs,
)
from ashare_multifactor.final_test.official_candidate_review_admission_storage import (
    DECISIONS_NAME,
    CandidateReviewAdmissionError,
    VerifiedCandidateReviewAdmission,
    parquet_bytes,
    publish_candidate_review_admission,
)
from ashare_multifactor.final_test.official_query_coverage import (
    canonical_json_bytes,
)


def run_historical_candidate_review_admission(
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
    """Revalidate each frozen historical pair without using final-test rows."""
    expected_rule = (
        code_root.resolve()
        / "configs/evidence/stage9_candidate_review_admission_date_rule.json"
    )
    date_rule, date_rule_bytes = _load_date_rule(
        date_rule_path,
        expected_path=expected_rule,
    )
    verified = load_verified_historical_admission_inputs(
        date_rule=date_rule,
        derivation_path=derivation_path,
        derivation_manifest_path=derivation_manifest_path,
        discovery_path=discovery_path,
        receipt_index_path=receipt_index_path,
        existing_inventory_path=existing_inventory_path,
        pdf_cache_root=pdf_cache_root,
    )
    decisions, unresolved = evaluate_historical_candidate_pairs(
        verified.derivation,
        discovery=verified.discovery,
        pdfs=verified.pdfs,
        date_rule=date_rule,
    )
    decisions_bytes = parquet_bytes(decisions)
    unresolved_bytes = parquet_bytes(unresolved)
    minimum_lag, maximum_lag = _lag_interval(date_rule)
    inputs = [
        historical_file_record(
            derivation_path,
            verified.derivation_bytes,
            role="historical_candidate_derivation",
            row_count=verified.derivation.height,
        ),
        historical_file_record(
            derivation_manifest_path,
            verified.derivation_manifest_bytes,
            role="historical_candidate_derivation_manifest",
            row_count=1,
        ),
        historical_file_record(
            discovery_path,
            verified.discovery_bytes,
            role="historical_pdf_discovery",
            row_count=verified.discovery.height,
        ),
        historical_file_record(
            receipt_index_path,
            verified.receipt_index_bytes,
            role="historical_pdf_receipt_index",
            row_count=len(verified.receipt_index["receipts"]),
        ),
        historical_file_record(
            existing_inventory_path,
            verified.existing_inventory_bytes,
            role="historical_pdf_existing_inventory",
            row_count=len(verified.existing_inventory["files"]),
        ),
        historical_cache_record(pdf_cache_root, verified.pdfs),
        historical_file_record(
            date_rule_path,
            date_rule_bytes,
            role="candidate_review_admission_date_rule",
            row_count=1,
        ),
    ]
    routing_identity = _routing_identity(decisions)
    manifest = {
        "schema_version": "1",
        "role": "candidate_review_admission",
        "status": "ready" if unresolved.is_empty() else "blocked",
        "attempt_id": "stage8-historical-rehearsal",
        "review_session_id": hashlib.sha256(
            canonical_json_bytes(inputs)
        ).hexdigest(),
        "review_queue": {
            "relative_path": str(discovery_path),
            "manifest_sha256": hashlib.sha256(
                verified.discovery_bytes
            ).hexdigest(),
        },
        "candidate_snapshot": {
            "relative_path": str(derivation_path),
            "manifest_sha256": hashlib.sha256(
                verified.derivation_manifest_bytes
            ).hexdigest(),
        },
        "routing": {
            **routing_identity,
            "identity_sha256": hashlib.sha256(
                canonical_json_bytes(routing_identity)
            ).hexdigest(),
        },
        "date_rule": {
            "path": str(date_rule_path),
            "sha256": hashlib.sha256(date_rule_bytes).hexdigest(),
            "derivation_manifest_sha256": date_rule["derivation"][
                "manifest_sha256"
            ],
            "derivation_parquet_sha256": date_rule["derivation"][
                "parquet_sha256"
            ],
            "period": HISTORICAL_PERIOD,
            "minimum_calendar_days": minimum_lag,
            "maximum_calendar_days": maximum_lag,
            "interval_closed": True,
        },
        "inputs": inputs,
        "candidate_count": verified.derivation.height,
        "admitted_count": verified.derivation.height - unresolved.height,
        "unresolved_count": unresolved.height,
        "candidate_decisions": {
            "path": DECISIONS_NAME,
            "sha256": hashlib.sha256(decisions_bytes).hexdigest(),
            "size_bytes": len(decisions_bytes),
            "row_count": decisions.height,
        },
        "unresolved_candidates": {
            "path": "unresolved_candidates.parquet",
            "sha256": hashlib.sha256(unresolved_bytes).hexdigest(),
            "size_bytes": len(unresolved_bytes),
            "row_count": unresolved.height,
        },
        "text_extraction": {
            "engine": "pypdf",
            "normalization": "nfkc_remove_unicode_whitespace_v1",
        },
        "historical_rehearsal": True,
        "final_test_strategy_outputs_read": False,
    }
    admission = publish_candidate_review_admission(
        destination,
        manifest=manifest,
        unresolved_bytes=unresolved_bytes,
        decisions_bytes=decisions_bytes,
    )
    if not admission.ready:
        raise CandidateReviewAdmissionError(admission)
    return admission


def _routing_identity(decisions) -> dict[str, str]:
    identity = [
        {
            "pair_id": row["pair_id"],
            "route": row["route"],
            "candidate_type": row["candidate_type"],
            "reason": row["routing_reason"],
        }
        for row in decisions.iter_rows(named=True)
    ]
    return {
        "routing_sha256": hashlib.sha256(
            canonical_json_bytes(identity)
        ).hexdigest(),
        "rule_version": _RULE_VERSION,
        "rules_sha256": routing._rules_sha256(),
    }
