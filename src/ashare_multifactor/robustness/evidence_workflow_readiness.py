"""Stage-8 gate for the complete pre-resume evidence workflow."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
from typing import Any
import xml.etree.ElementTree as ET

import polars as pl

from ashare_multifactor.audit.records import (
    file_record,
    sha256_file,
    verify_file_record,
)
from ashare_multifactor.robustness.protocol_identities import (
    validate_domain_identity,
)


_REPORTS = {
    "document_validation.json": "evidence_document_validation",
    "candidate_collection.json": "evidence_candidate_collection",
    "review_submission.json": "evidence_review_submission",
    "coverage_publication.json": "evidence_coverage_publication",
    "evidence_import.json": "evidence_import_rehearsal",
    "historical_rehearsal.json": "evidence_historical_rehearsal",
}
_ATTACHMENTS = {
    "focused_tests.xml": "evidence_workflow_focused_tests",
    "historical_routing.parquet": "evidence_historical_routing",
    "candidate_review_admission.json": (
        "evidence_candidate_review_admission"
    ),
    "unresolved_candidates.parquet": (
        "evidence_candidate_review_admission_unresolved"
    ),
    "candidate_admission_decisions.parquet": (
        "evidence_candidate_review_admission_decisions"
    ),
}
_FILES = {**_REPORTS, **_ATTACHMENTS}
_REQUIRED_TEMPORAL_POLICY = {
    "predicate": "publication_date_lte_candidate_ex_date",
    "publication_date_source": "cninfo_finalpage_path_date",
    "same_day_allowed": True,
    "historical_lag_interval_usage": "diagnostic_only",
    "final_test_gap_distribution_used_to_select_predicate": False,
}


def write_evidence_workflow_readiness_audit(
    source_root: Path,
    destination: Path,
    *,
    evidence_workflow_identity: dict[str, object],
) -> dict[str, object]:
    """Freeze the six reports needed before a new Stage-9 authorization."""
    identity = validate_domain_identity(
        evidence_workflow_identity,
        role="evidence_workflow_identity",
    )
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise FileExistsError("evidence workflow readiness destination is not empty")
    else:
        destination.mkdir(parents=True)
    for name in _FILES:
        source = source_root / name
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"evidence workflow readiness report is missing: {name}")
        shutil.copy2(source, destination / name)
    _validate_reports(
        destination,
        evidence_workflow_identity_sha256=str(identity["sha256"]),
    )
    manifest = {
        "schema_version": "1",
        "role": "stage8_evidence_workflow_readiness",
        "status": "ready",
        "period": ["2017-01-01", "2021-12-31"],
        "evidence_workflow_identity": identity,
        "files": [
            file_record(
                destination / name,
                root=destination,
                role=role,
            ).to_dict()
            for name, role in _FILES.items()
        ],
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    verify_evidence_workflow_readiness_audit(destination)
    return manifest


def verify_evidence_workflow_readiness_audit(root: Path) -> str:
    """Reject a partial rehearsal even when every individual file is present."""
    manifest_path = root / "manifest.json"
    try:
        manifest = _read_json(manifest_path)
    except ValueError as error:
        raise ValueError("evidence workflow readiness manifest is invalid") from error
    records = manifest.get("files")
    identity = validate_domain_identity(
        manifest.get("evidence_workflow_identity"),
        role="evidence_workflow_identity",
    )
    if (
        manifest.get("schema_version") != "1"
        or manifest.get("role") != "stage8_evidence_workflow_readiness"
        or manifest.get("status") != "ready"
        or manifest.get("period") != ["2017-01-01", "2021-12-31"]
        or not isinstance(records, list)
        or len(records) != len(_FILES)
        or {
            record.get("role")
            for record in records
            if isinstance(record, dict)
        }
        != set(_FILES.values())
    ):
        raise ValueError("evidence workflow readiness is incomplete")
    for record in records:
        try:
            verify_file_record(record, root=root)
        except (FileNotFoundError, TypeError, ValueError) as error:
            raise ValueError("evidence workflow readiness file changed") from error
    _validate_reports(
        root,
        evidence_workflow_identity_sha256=str(identity["sha256"]),
    )
    return sha256_file(manifest_path)


def _validate_reports(
    root: Path,
    *,
    evidence_workflow_identity_sha256: str,
) -> None:
    documents = _read_json(root / "document_validation.json")
    candidates = _read_json(root / "candidate_collection.json")
    review = _read_json(root / "review_submission.json")
    coverage = _read_json(root / "coverage_publication.json")
    imported = _read_json(root / "evidence_import.json")
    historical = _read_json(root / "historical_rehearsal.json")
    admission_path = root / "candidate_review_admission.json"
    admission = _read_json(admission_path)
    junit_sha256 = sha256_file(root / "focused_tests.xml")
    routing_sha256 = sha256_file(root / "historical_routing.parquet")
    reports = (documents, candidates, review, coverage, imported, historical)
    passed_tests = _passed_test_cases(root / "focused_tests.xml")
    try:
        routing = pl.read_parquet(root / "historical_routing.parquet")
        unresolved = pl.read_parquet(root / "unresolved_candidates.parquet")
        decisions = pl.read_parquet(
            root / "candidate_admission_decisions.parquet"
        )
    except pl.exceptions.PolarsError as error:
        raise ValueError("historical evidence closure is invalid") from error
    unresolved_record = admission.get("unresolved_candidates")
    decisions_record = admission.get("candidate_decisions")
    admission_routing = admission.get("routing")
    date_rule = admission.get("date_rule")
    admission_inputs = admission.get("inputs")
    if (
        any(
            report.get("evidence_workflow_identity_sha256")
            != evidence_workflow_identity_sha256
            or report.get("focused_tests_sha256") != junit_sha256
            or not isinstance(report.get("test_cases"), list)
            or not report["test_cases"]
            or not set(report["test_cases"]).issubset(passed_tests)
            for report in reports
        )
        or documents.get("status") != "passed"
        or documents.get("valid_pdf_count", 0) < 1
        or documents.get("non_pdf_rejected") is not True
        or documents.get("encrypted_pdf_rejected") is not True
        or documents.get("unreadable_pdf_rejected") is not True
        or candidates.get("status") != "passed"
        or candidates.get("interrupted_resume_verified") is not True
        or candidates.get("peer_eof_recovery_verified") is not True
        or candidates.get("exact_query_coverage") is not True
        or candidates.get("out_of_scope_rejected") is not True
        or candidates.get("missing_effective_date_preserved") is not True
        or review.get("status") != "passed"
        or review.get("exact_queue_coverage") is not True
        or review.get("exact_candidate_coverage") is not True
        or review.get("unsupported_document_rejected") is not True
        or review.get("resumable_batch_review") is not True
        or review.get("incomplete_batch_finalization_rejected") is not True
        or review.get("conflicting_batch_rejected") is not True
        or review.get("frozen_batch_input_drift_rejected") is not True
        or review.get("cross_symbol_disposition_rejected") is not True
        or review.get("cross_symbol_fact_rejected") is not True
        or review.get("missing_effective_date_requires_reconciliation") is not True
        or review.get("null_original_comparison_safe") is not True
        or coverage.get("status") != "passed"
        or coverage.get("atomic_pair_publication") is not True
        or coverage.get("direct_review_without_batch_provenance_rejected")
        is not True
        or coverage.get("zero_event_without_fake_pdf") is not True
        or coverage.get("shared_evidence_drift_detected") is not True
        or imported.get("status") != "passed"
        or imported.get("query_packages_reused") is not True
        or imported.get("valid_documents_reused", 0) < 1
        or imported.get("invalid_documents_refetch_only") is not True
        or historical.get("status") != "passed"
        or historical.get("period") != ["2017-01-01", "2021-12-31"]
        or historical.get("final_test_row_count") != 0
        or historical.get("announcement_time_epoch_unit") != "ms"
        or historical.get("announcement_time_source_timezone") != "UTC"
        or historical.get("announcement_publication_timezone")
        != "Asia/Shanghai"
        or historical.get("symbol_count", 0) < 100
        or historical.get("rehearsal_mode")
        != (
            "real_catalog_routing_plus_verified_candidate_admission_and_"
            "synthetic_field_publication_fixtures"
        )
        or historical.get("historical_catalog_rerouted") is not True
        or historical.get("coverage_pair_fixture_ready") is not True
        or historical.get("historical_routing_sha256") != routing_sha256
        or not _valid_sha256(
            historical.get("collector_readiness_manifest_sha256")
        )
        or historical.get("convertible_bond_exclusion_count", 0) < 1
        or not {
            "catalog_id",
            "route",
            "candidate_type",
            "reason",
            "rule_version",
        }.issubset(routing.columns)
        or routing.height != historical.get("announcement_count")
        or routing.get_column("catalog_id").n_unique() != routing.height
        or routing.filter(
            pl.col("rule_version") != "stage9-shared-routing-v5"
        ).height
        or routing.filter(
            pl.col("reason").str.starts_with("convertible_bond_")
        ).height
        != historical.get("convertible_bond_exclusion_count")
        or admission_path.read_bytes() != _canonical_json_bytes(admission)
        or admission.get("schema_version") != "1"
        or admission.get("role") != "candidate_review_admission"
        or admission.get("status") != "ready"
        or admission.get("final_test_strategy_outputs_read") is not False
        or not isinstance(admission_routing, dict)
        or admission_routing.get("rule_version")
        != "stage9-shared-routing-v5"
        or not isinstance(date_rule, dict)
        or date_rule.get("period") != ["2017-01-01", "2021-12-31"]
        or not _valid_sha256(date_rule.get("sha256"))
        or date_rule.get("temporal_policy") != _REQUIRED_TEMPORAL_POLICY
        or not isinstance(unresolved_record, dict)
        or unresolved_record.get("path") != "unresolved_candidates.parquet"
        or unresolved_record.get("sha256")
        != sha256_file(root / "unresolved_candidates.parquet")
        or unresolved_record.get("size_bytes")
        != (root / "unresolved_candidates.parquet").stat().st_size
        or unresolved_record.get("row_count") != unresolved.height
        or admission.get("candidate_count", 0) < 1
        or admission.get("admitted_count") != admission.get("candidate_count")
        or admission.get("unresolved_count") != 0
        or not unresolved.is_empty()
        or not isinstance(decisions_record, dict)
        or decisions_record.get("path")
        != "candidate_admission_decisions.parquet"
        or decisions_record.get("sha256")
        != sha256_file(root / "candidate_admission_decisions.parquet")
        or decisions_record.get("size_bytes")
        != (root / "candidate_admission_decisions.parquet").stat().st_size
        or decisions_record.get("row_count") != decisions.height
        or decisions.height != admission.get("candidate_count")
        or not {
            "pair_id",
            "status",
            "failure_codes",
            "historical_lag_interval_hit",
        }
        <= set(decisions.columns)
        or decisions.schema["historical_lag_interval_hit"] != pl.Boolean
        or decisions.get_column("pair_id").n_unique() != decisions.height
        or decisions.filter(pl.col("status") != "admitted").height
        or not isinstance(admission_inputs, list)
        or {
            record.get("role")
            for record in admission_inputs
            if isinstance(record, dict)
        }
        != {
            "historical_candidate_derivation",
            "historical_candidate_derivation_manifest",
            "historical_pdf_discovery",
            "historical_pdf_receipt_index",
            "historical_pdf_existing_inventory",
            "historical_pdf_cache",
            "candidate_review_admission_date_rule",
        }
        or any(
            not isinstance(record, dict)
            or not isinstance(record.get("path"), str)
            or not _valid_sha256(record.get("sha256"))
            or not isinstance(record.get("size_bytes"), int)
            or record.get("size_bytes", -1) < 0
            or not isinstance(record.get("row_count"), int)
            or record.get("row_count", 0) < 1
            for record in admission_inputs
        )
        or historical.get("candidate_admission_closure_recomputed") is not True
        or historical.get("candidate_admission_candidate_count")
        != admission.get("candidate_count")
        or historical.get("candidate_admission_unresolved_count") != 0
        or historical.get("candidate_admission_manifest_sha256")
        != sha256_file(admission_path)
        or historical.get(
            "candidate_admission_unresolved_manifest_sha256"
        )
        != sha256_file(root / "unresolved_candidates.parquet")
        or historical.get("candidate_admission_decisions_sha256")
        != sha256_file(root / "candidate_admission_decisions.parquet")
        or historical.get(
            "candidate_admission_input_identity_sha256"
        )
        != admission.get("review_session_id")
        or historical.get("admission_date_rule_sha256")
        != date_rule.get("sha256")
        or historical.get("candidate_admission_temporal_policy")
        != _REQUIRED_TEMPORAL_POLICY
    ):
        raise ValueError("complete evidence workflow rehearsal is not ready")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid evidence workflow report: {path.name}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"invalid evidence workflow report: {path.name}")
    return payload


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _passed_test_cases(path: Path) -> set[str]:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as error:
        raise ValueError("focused evidence workflow test report is invalid") from error
    suites = list(root.findall("testsuite")) if root.tag == "testsuites" else [root]
    if not suites or any(
        suite.tag != "testsuite"
        or int(suite.get("errors", "0"))
        or int(suite.get("failures", "0"))
        or int(suite.get("skipped", "0"))
        for suite in suites
    ):
        raise ValueError("focused evidence workflow tests did not all pass")
    return {
        f"{case.get('classname')}::{case.get('name')}"
        for case in root.iter("testcase")
        if case.get("classname")
        and case.get("name")
        and not any(
            case.find(tag) is not None for tag in ("error", "failure", "skipped")
        )
    }
