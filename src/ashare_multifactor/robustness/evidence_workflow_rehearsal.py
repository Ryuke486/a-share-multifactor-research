"""Build Stage-8 evidence-workflow reports from tests and historical evidence."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

import polars as pl

from ashare_multifactor.audit.records import sha256_file
from ashare_multifactor.final_test.official_announcement_routing import (
    announcement_routing_frame,
)
from ashare_multifactor.robustness.collector_readiness import (
    verify_collector_readiness_audit,
)
from ashare_multifactor.robustness.evidence_workflow_readiness import (
    write_evidence_workflow_readiness_audit,
)
from ashare_multifactor.robustness.protocol_identities import (
    build_evidence_workflow_identity,
)


_DOCUMENT_TESTS = (
    "tests.test_final_test_official_document_validation::"
    "test_html_error_page_is_rejected_before_document_publication",
    "tests.test_final_test_official_document_validation::"
    "test_readable_pdf_returns_structural_identity",
    "tests.test_final_test_official_document_validation::"
    "test_pdf_signature_without_readable_structure_is_rejected",
    "tests.test_final_test_official_document_validation::"
    "test_encrypted_pdf_is_rejected",
    "tests.test_final_test_official_document_validation::"
    "test_zero_page_pdf_is_rejected",
    "tests.test_final_test_official_evidence_workspace::"
    "test_non_pdf_response_is_quarantined_and_never_enters_document_cache",
)
_CANDIDATE_TESTS = (
    "tests.test_final_test_corporate_action_candidates::"
    "test_candidate_collection_publishes_exact_resumable_snapshot",
    "tests.test_final_test_corporate_action_candidates::"
    "test_candidate_collection_rejects_response_outside_exact_query_scope",
    "tests.test_final_test_corporate_action_candidates::"
    "test_candidate_collection_rejects_changed_provider_schema",
)
_REVIEW_TESTS = (
    "tests.test_final_test_official_review_submission::"
    "test_review_submission_exactly_covers_queue_and_provider_candidates",
    "tests.test_final_test_official_review_submission::"
    "test_review_submission_rejects_incomplete_queue_decisions",
    "tests.test_final_test_official_review_submission::"
    "test_review_submission_rejects_fact_without_relevant_cached_document",
    "tests.test_final_test_official_review_batches::"
    "test_review_batches_resume_then_finalize_exact_submission",
    "tests.test_final_test_official_review_batches::"
    "test_review_batch_rejects_conflicting_resubmission",
    "tests.test_final_test_official_review_batches::"
    "test_review_batch_workspace_rejects_frozen_input_drift",
    "tests.test_final_test_official_review_batches::"
    "test_review_batch_rejects_cross_symbol_official_evidence",
)
_COVERAGE_TESTS = (
    "tests.test_final_test_official_coverage_publisher::"
    "test_coverage_publisher_atomically_builds_both_ready_coverages_without_pdf_copies",
    "tests.test_final_test_official_coverage_publisher::"
    "test_ready_coverage_rechecks_shared_document_bytes",
)
_IMPORT_TESTS = (
    "tests.test_final_test_official_evidence_import::"
    "test_evidence_import_reuses_complete_queries_and_only_valid_legacy_documents",
    "tests.test_final_test_official_evidence_import::"
    "test_source_attempt_can_be_verified_after_code_head_moves",
    "tests.test_final_test_data_reuse::"
    "test_data_reuse_receipt_binds_source_claim_panel_and_v4_protocol",
)
_TEST_GROUPS = {
    "document_validation.json": _DOCUMENT_TESTS,
    "candidate_collection.json": _CANDIDATE_TESTS,
    "review_submission.json": _REVIEW_TESTS,
    "coverage_publication.json": _COVERAGE_TESTS,
    "evidence_import.json": _IMPORT_TESTS,
}
_PERIOD = ["2017-01-01", "2021-12-31"]


def build_evidence_workflow_rehearsal(
    *,
    code_root: Path,
    collector_readiness_root: Path,
    junit_path: Path,
    output_root: Path,
) -> Path:
    """Freeze focused seam results plus a rerouted 2017-2021 real catalog."""
    if output_root.exists():
        if not output_root.is_dir() or any(output_root.iterdir()):
            raise FileExistsError("evidence workflow rehearsal output is not empty")
    else:
        output_root.mkdir(parents=True)
    source = output_root / "source"
    source.mkdir()
    passed = _passed_test_cases(junit_path)
    matched = {
        name: _require_tests(passed, required)
        for name, required in _TEST_GROUPS.items()
    }
    identity = build_evidence_workflow_identity(code_root)
    identity_sha256 = str(identity["sha256"])
    shutil.copy2(junit_path, source / "focused_tests.xml")
    junit_sha256 = sha256_file(source / "focused_tests.xml")
    common = {
        "status": "passed",
        "evidence_workflow_identity_sha256": identity_sha256,
        "focused_tests_sha256": junit_sha256,
    }
    _write_json(
        source / "document_validation.json",
        {
            **common,
            "test_cases": matched["document_validation.json"],
            "valid_pdf_count": 1,
            "non_pdf_rejected": True,
            "encrypted_pdf_rejected": True,
            "unreadable_pdf_rejected": True,
        },
    )
    _write_json(
        source / "candidate_collection.json",
        {
            **common,
            "test_cases": matched["candidate_collection.json"],
            "interrupted_resume_verified": True,
            "exact_query_coverage": True,
            "out_of_scope_rejected": True,
        },
    )
    _write_json(
        source / "review_submission.json",
        {
            **common,
            "test_cases": matched["review_submission.json"],
            "exact_queue_coverage": True,
            "exact_candidate_coverage": True,
            "unsupported_document_rejected": True,
            "resumable_batch_review": True,
            "incomplete_batch_finalization_rejected": True,
            "conflicting_batch_rejected": True,
            "frozen_batch_input_drift_rejected": True,
            "cross_symbol_evidence_rejected": True,
        },
    )
    _write_json(
        source / "coverage_publication.json",
        {
            **common,
            "test_cases": matched["coverage_publication.json"],
            "atomic_pair_publication": True,
            "zero_event_without_fake_pdf": True,
            "shared_evidence_drift_detected": True,
        },
    )
    _write_json(
        source / "evidence_import.json",
        {
            **common,
            "test_cases": matched["evidence_import.json"],
            "query_packages_reused": True,
            "valid_documents_reused": 1,
            "invalid_documents_refetch_only": True,
        },
    )
    historical = _historical_report(
        collector_readiness_root,
        source=source,
        common=common,
        coverage_tests=matched["coverage_publication.json"],
    )
    _write_json(source / "historical_rehearsal.json", historical)
    audit = output_root / "audit"
    write_evidence_workflow_readiness_audit(
        source,
        audit,
        evidence_workflow_identity=identity,
    )
    return audit


def _historical_report(
    collector_root: Path,
    *,
    source: Path,
    common: dict[str, object],
    coverage_tests: list[str],
) -> dict[str, object]:
    collector_sha256 = verify_collector_readiness_audit(collector_root)
    sample = pl.read_parquet(collector_root / "sample.parquet")
    catalog = pl.read_parquet(collector_root / "catalog.parquet")
    required_sample = {"symbol", "latest_date"}
    required_catalog = {
        "catalog_id",
        "symbol",
        "announcement_title",
        "announcement_time_raw",
    }
    escaped_symbols = catalog.select("symbol").unique().join(
        sample.select("symbol"),
        on="symbol",
        how="anti",
    )
    if (
        not required_sample.issubset(sample.columns)
        or not required_catalog.issubset(catalog.columns)
        or sample.height < 100
        or sample.get_column("symbol").n_unique() != sample.height
        or sample.get_column("latest_date").max() > date(2021, 12, 31)
        or catalog.height < 1
        or catalog.get_column("catalog_id").n_unique() != catalog.height
        or escaped_symbols.height
    ):
        raise ValueError("historical evidence workflow scope is invalid")
    timestamps = catalog.select(
        pl.from_epoch(
            pl.col("announcement_time_raw").cast(pl.Int64, strict=True),
            time_unit="ms",
        ).alias("announcement_time")
    )
    final_test_rows = timestamps.filter(
        pl.col("announcement_time").dt.date() >= date(2022, 1, 1)
    ).height
    routing = announcement_routing_frame(catalog)
    if (
        routing.height != catalog.height
        or routing.get_column("catalog_id").to_list()
        != catalog.sort("catalog_id").get_column("catalog_id").to_list()
    ):
        raise ValueError("historical catalog routing is incomplete")
    routing_path = source / "historical_routing.parquet"
    routing.write_parquet(routing_path, compression="zstd")
    exclusions = routing.filter(
        pl.col("reason").str.starts_with("convertible_bond_")
    ).height
    if final_test_rows or exclusions < 1:
        raise ValueError("historical evidence workflow rehearsal is incomplete")
    return {
        **common,
        "test_cases": coverage_tests,
        "period": _PERIOD,
        "final_test_row_count": final_test_rows,
        "symbol_count": sample.height,
        "announcement_count": catalog.height,
        "rehearsal_mode": (
            "real_catalog_routing_plus_synthetic_field_publication_fixtures"
        ),
        "historical_catalog_rerouted": True,
        "coverage_pair_fixture_ready": True,
        "convertible_bond_exclusion_count": exclusions,
        "collector_readiness_manifest_sha256": collector_sha256,
        "sample_sha256": sha256_file(collector_root / "sample.parquet"),
        "catalog_sha256": sha256_file(collector_root / "catalog.parquet"),
        "historical_routing_sha256": sha256_file(routing_path),
    }


def _passed_test_cases(path: Path) -> set[str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("focused evidence workflow test report is missing")
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as error:
        raise ValueError("focused evidence workflow test report is invalid") from error
    suites = (
        list(root.findall("testsuite"))
        if root.tag == "testsuites"
        else [root]
    )
    if not suites or any(
        suite.tag != "testsuite"
        or int(suite.get("errors", "0"))
        or int(suite.get("failures", "0"))
        or int(suite.get("skipped", "0"))
        for suite in suites
    ):
        raise ValueError("focused evidence workflow tests did not all pass")
    passed: set[str] = set()
    for case in root.iter("testcase"):
        if any(case.find(tag) is not None for tag in ("error", "failure", "skipped")):
            continue
        classname = case.get("classname")
        name = case.get("name")
        if classname and name:
            passed.add(f"{classname}::{name}")
    return passed


def _require_tests(passed: set[str], required: tuple[str, ...]) -> list[str]:
    missing = [test for test in required if test not in passed]
    if missing:
        raise ValueError("focused evidence workflow tests are missing: " + ", ".join(missing))
    return sorted(required)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
