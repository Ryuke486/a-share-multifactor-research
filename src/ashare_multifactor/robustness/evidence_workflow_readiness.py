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
}
_FILES = {**_REPORTS, **_ATTACHMENTS}


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
    junit_sha256 = sha256_file(root / "focused_tests.xml")
    routing_sha256 = sha256_file(root / "historical_routing.parquet")
    reports = (documents, candidates, review, coverage, imported, historical)
    passed_tests = _passed_test_cases(root / "focused_tests.xml")
    try:
        routing = pl.read_parquet(root / "historical_routing.parquet")
    except pl.exceptions.PolarsError as error:
        raise ValueError("historical evidence routing is invalid") from error
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
        or historical.get("symbol_count", 0) < 100
        or historical.get("rehearsal_mode")
        != "real_catalog_routing_plus_synthetic_field_publication_fixtures"
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
            pl.col("rule_version") != "stage9-shared-routing-v4"
        ).height
        or routing.filter(
            pl.col("reason").str.starts_with("convertible_bond_")
        ).height
        != historical.get("convertible_bond_exclusion_count")
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
