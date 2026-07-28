from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import polars as pl
import pytest

from ashare_multifactor.audit.records import sha256_file
from ashare_multifactor.robustness.change_impact import (
    verify_change_impact_audit,
    verify_evidence_workflow_only_change_impact,
    write_change_impact_audit,
)
from ashare_multifactor.robustness.evidence_workflow_readiness import (
    verify_evidence_workflow_readiness_audit,
    write_evidence_workflow_readiness_audit,
)
from ashare_multifactor.robustness.protocol_identities import identity_payload
from ashare_multifactor.robustness.protocol import load_robustness_protocol
from ashare_multifactor.robustness.test_protocol import seal_test_protocol


def _identities(*, evidence: str) -> dict[str, dict[str, object]]:
    return {
        "research": identity_payload(
            "research_result_identity",
            {"stage7": "same", "stage8_results": "same"},
        ),
        "final_execution": identity_payload(
            "final_execution_identity",
            {"execution": "same"},
        ),
        "evidence_workflow": identity_payload(
            "evidence_workflow_identity",
            {"workflow": evidence},
        ),
    }


def test_change_impact_keeps_research_results_when_only_evidence_changes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "change-impact.json"
    payload = write_change_impact_audit(
        path,
        predecessor_identities=_identities(evidence="v3"),
        current_identities=_identities(evidence="v4"),
    )

    assert payload["changed_domains"] == ["evidence_workflow"]
    assert payload["research_replay_required"] is False
    assert payload["final_execution_rehearsal_required"] is False
    assert payload["evidence_workflow_rehearsal_required"] is True
    assert payload["stage7_stage8_research_results_reusable"] is True
    assert len(verify_change_impact_audit(path)) == 64
    assert (
        verify_evidence_workflow_only_change_impact(
            path,
            expected_current_identities=_identities(evidence="v4"),
        )
        == verify_change_impact_audit(path)
    )


def test_protocol_only_change_impact_rejects_final_execution_change(
    tmp_path: Path,
) -> None:
    path = tmp_path / "change-impact.json"
    current = _identities(evidence="v4")
    current["final_execution"] = identity_payload(
        "final_execution_identity",
        {"execution": "changed"},
    )
    write_change_impact_audit(
        path,
        predecessor_identities=_identities(evidence="v3"),
        current_identities=current,
    )

    with pytest.raises(
        ValueError,
        match="does not permit evidence-workflow-only reseal",
    ):
        verify_evidence_workflow_only_change_impact(
            path,
            expected_current_identities=current,
        )


def test_evidence_readiness_requires_the_full_pre_resume_workflow(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    identity = _identities(evidence="v4")["evidence_workflow"]
    (source / "focused_tests.xml").write_text(
        '<testsuites><testsuite errors="0" failures="0" skipped="0" tests="1">'
        '<testcase classname="tests.test_evidence" name="test_ready"/>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    pl.DataFrame(
        {
            "catalog_id": ["a"],
            "route": ["excluded"],
            "candidate_type": [""],
            "reason": ["convertible_bond_conversion_pause"],
            "rule_version": ["stage9-shared-routing-v4"],
        }
    ).write_parquet(source / "historical_routing.parquet")
    common = {
        "evidence_workflow_identity_sha256": identity["sha256"],
        "focused_tests_sha256": sha256_file(source / "focused_tests.xml"),
        "test_cases": ["tests.test_evidence::test_ready"],
    }
    reports = {
        "document_validation.json": {
            **common,
            "status": "passed",
            "valid_pdf_count": 1,
            "non_pdf_rejected": True,
            "encrypted_pdf_rejected": True,
            "unreadable_pdf_rejected": True,
        },
        "candidate_collection.json": {
            **common,
            "status": "passed",
            "interrupted_resume_verified": True,
            "exact_query_coverage": True,
            "out_of_scope_rejected": True,
        },
        "review_submission.json": {
            **common,
            "status": "passed",
            "exact_queue_coverage": True,
            "exact_candidate_coverage": True,
            "unsupported_document_rejected": True,
            "resumable_batch_review": True,
            "incomplete_batch_finalization_rejected": True,
            "conflicting_batch_rejected": True,
            "frozen_batch_input_drift_rejected": True,
            "cross_symbol_evidence_rejected": True,
        },
        "coverage_publication.json": {
            **common,
            "status": "passed",
            "atomic_pair_publication": True,
            "zero_event_without_fake_pdf": True,
            "shared_evidence_drift_detected": True,
        },
        "evidence_import.json": {
            **common,
            "status": "passed",
            "query_packages_reused": True,
            "valid_documents_reused": 1,
            "invalid_documents_refetch_only": True,
        },
        "historical_rehearsal.json": {
            **common,
            "status": "passed",
            "period": ["2017-01-01", "2021-12-31"],
            "final_test_row_count": 0,
            "symbol_count": 100,
            "announcement_count": 1,
            "rehearsal_mode": (
                "real_catalog_routing_plus_synthetic_field_publication_fixtures"
            ),
            "historical_catalog_rerouted": True,
            "coverage_pair_fixture_ready": True,
            "convertible_bond_exclusion_count": 1,
            "collector_readiness_manifest_sha256": "a" * 64,
            "historical_routing_sha256": sha256_file(
                source / "historical_routing.parquet"
            ),
        },
    }
    for name, payload in reports.items():
        (source / name).write_text(json.dumps(payload), encoding="utf-8")

    destination = tmp_path / "audit"
    manifest = write_evidence_workflow_readiness_audit(
        source,
        destination,
        evidence_workflow_identity=identity,
    )

    assert manifest["status"] == "ready"
    assert len(verify_evidence_workflow_readiness_audit(destination)) == 64


def test_protocol_v4_binds_split_identities_and_new_readiness_gates(
    tmp_path: Path,
) -> None:
    sealed = seal_test_protocol(
        tmp_path / "sealed.json",
        protocol=load_robustness_protocol(Path("configs/robustness_protocol.yaml")),
        code_identity={
            "commit": "a" * 40,
            "tree": "b" * 40,
            "dirty": False,
            "sources": [],
            "python": "3.14.6",
            "dependencies": {},
        },
        validation_pointer={
            "run_id": "65b19e1_stage7_validation_controlled_collector_successor",
            "manifest_sha256": "c" * 64,
        },
        market_rules_sha256="d" * 64,
        report_template_sha256="e" * 64,
        opening_ledger_root=tmp_path / "ledger",
        gate={"sealed_test_protocol_allowed": True},
        action_source_contract_sha256="1" * 64,
        action_coverage_audit_sha256="2" * 64,
        collector_readiness_audit_sha256="3" * 64,
        predecessor={
            "run_id": "old",
            "manifest_sha256": "4" * 64,
            "reason": "evidence workflow successor",
            "status": "superseded_for_final_execution",
        },
        protocol_identities=_identities(evidence="v4"),
        change_impact_audit_sha256="5" * 64,
        evidence_workflow_readiness_audit_sha256="6" * 64,
    )

    assert sealed["protocol_version"] == 4
    assert sealed["protocol_identities"]["research"]["role"] == (
        "research_result_identity"
    )
    assert sealed["change_impact_audit_sha256"] == "5" * 64
    assert sealed["evidence_workflow_readiness_audit_sha256"] == "6" * 64


def test_rehearsal_binds_focused_tests_and_reroutes_real_historical_catalog(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from ashare_multifactor.robustness import evidence_workflow_rehearsal as rehearsal

    collector = tmp_path / "collector"
    collector.mkdir()
    symbols = [f"{index:06d}" for index in range(1, 101)]
    pl.DataFrame(
        {
            "symbol": symbols,
            "latest_date": [date(2021, 12, 31)] * 100,
        }
    ).write_parquet(collector / "sample.parquet")
    pl.DataFrame(
        {
            "catalog_id": ["a", "b"],
            "symbol": symbols[:2],
            "announcement_title": [
                "关于实施2020年度权益分派时某转债停止转股的提示性公告",
                "关于2020年度权益分派调整可转债转股价格的公告",
            ],
            "announcement_time_raw": ["1577836800000", "1609455600000"],
        }
    ).write_parquet(collector / "catalog.parquet")
    monkeypatch.setattr(
        rehearsal,
        "verify_collector_readiness_audit",
        lambda _root: "a" * 64,
    )
    suites = ET.Element("testsuites")
    suite = ET.SubElement(
        suites,
        "testsuite",
        errors="0",
        failures="0",
        skipped="0",
    )
    required = sorted(
        {
            test
            for tests in rehearsal._TEST_GROUPS.values()
            for test in tests
        }
    )
    suite.set("tests", str(len(required)))
    for node_id in required:
        classname, name = node_id.split("::", maxsplit=1)
        ET.SubElement(
            suite,
            "testcase",
            classname=classname,
            name=name,
        )
    junit = tmp_path / "focused.xml"
    ET.ElementTree(suites).write(junit, encoding="utf-8", xml_declaration=True)

    audit = rehearsal.build_evidence_workflow_rehearsal(
        code_root=Path.cwd(),
        collector_readiness_root=collector,
        junit_path=junit,
        output_root=tmp_path / "output",
    )

    assert len(verify_evidence_workflow_readiness_audit(audit)) == 64
    historical = json.loads(
        (audit / "historical_rehearsal.json").read_text(encoding="utf-8")
    )
    assert historical["final_test_row_count"] == 0
    assert historical["symbol_count"] == 100
    assert historical["convertible_bond_exclusion_count"] == 2
