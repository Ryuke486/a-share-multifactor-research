from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import polars as pl
import pytest

from ashare_multifactor.audit.records import sha256_file
from ashare_multifactor.robustness import (
    evidence_workflow_successor_release as successor_release,
)
from ashare_multifactor.robustness.change_impact import (
    verify_change_impact_audit,
    verify_evidence_workflow_only_change_impact,
    write_change_impact_audit,
)
from ashare_multifactor.robustness.evidence_workflow_readiness import (
    verify_evidence_workflow_readiness_audit,
    write_evidence_workflow_readiness_audit,
)
from ashare_multifactor.robustness.protocol_identities import (
    build_final_execution_identity,
    build_final_execution_identity_at_revision,
    identity_payload,
)
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
            expected_predecessor_final_execution_identity=_identities(
                evidence="v3"
            )["final_execution"],
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
            expected_predecessor_final_execution_identity=_identities(
                evidence="v3"
            )["final_execution"],
        )


def test_protocol_only_change_impact_rejects_forged_predecessor_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "change-impact.json"
    predecessor = _identities(evidence="v3")
    current = _identities(evidence="v4")
    forged_final_execution = identity_payload(
        "final_execution_identity",
        {"execution": "forged"},
    )
    predecessor["final_execution"] = forged_final_execution
    current["final_execution"] = forged_final_execution
    write_change_impact_audit(
        path,
        predecessor_identities=predecessor,
        current_identities=current,
    )

    with pytest.raises(
        ValueError,
        match="does not permit evidence-workflow-only reseal",
    ):
        verify_evidence_workflow_only_change_impact(
            path,
            expected_current_identities=current,
            expected_predecessor_final_execution_identity=_identities(
                evidence="v3"
            )["final_execution"],
        )


def test_final_execution_identity_can_be_rebuilt_from_a_sealed_git_tree() -> None:
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tree = subprocess.run(
        ("git", "rev-parse", f"{commit}^{{tree}}"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert build_final_execution_identity_at_revision(
        Path.cwd(),
        commit=commit,
        tree=tree,
    ) == build_final_execution_identity(Path.cwd())


def test_v4_successor_publication_preserves_bound_research_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "data"
    predecessor_root = tmp_path / "predecessor"
    predecessor_datasets = predecessor_root / "datasets"
    predecessor_artifacts = predecessor_root / "artifacts"
    predecessor_datasets.mkdir(parents=True)
    predecessor_artifacts.mkdir()
    results = predecessor_datasets / "robustness_results.parquet"
    report = predecessor_artifacts / "report.md"
    gate = predecessor_artifacts / "protocol_gate.json"
    results.write_bytes(b"unchanged-results")
    report.write_text("unchanged report", encoding="utf-8")
    gate.write_text(
        json.dumps({"sealed_test_protocol_allowed": True}),
        encoding="utf-8",
    )
    validation = SimpleNamespace(
        run_id="validation",
        manifest_sha256="1" * 64,
    )
    research_records = {
        "validation_run_id": validation.run_id,
        "validation_manifest_sha256": validation.manifest_sha256,
        "predecessor_stage8_run_id": "original-research-release",
        "predecessor_stage8_manifest_sha256": "2" * 64,
        "predecessor_stage8_lineage_sha256": "3" * 64,
        "robustness_results_sha256": sha256_file(results),
        "report_sha256": sha256_file(report),
        "protocol_gate_sha256": sha256_file(gate),
    }
    predecessor_identities = {
        "research": identity_payload(
            "research_result_identity",
            research_records,
        ),
        "final_execution": identity_payload(
            "final_execution_identity",
            {"execution": "same"},
        ),
        "evidence_workflow": identity_payload(
            "evidence_workflow_identity",
            {"workflow": "v4-predecessor"},
        ),
    }
    current_identities = {
        **predecessor_identities,
        "evidence_workflow": identity_payload(
            "evidence_workflow_identity",
            {"workflow": "v4-successor"},
        ),
    }
    lineage_path = predecessor_root / "lineage.json"
    lineage_path.write_text(
        json.dumps(
            {
                "inputs": {},
                "reproducibility": {},
                "research_reuse": research_records,
            }
        ),
        encoding="utf-8",
    )
    (predecessor_artifacts / "sealed_test_protocol.json").write_text(
        json.dumps(
            {
                "protocol_version": 4,
                "status": "sealed",
                "code": {
                    "commit": "a" * 40,
                    "tree": "b" * 40,
                    "dirty": False,
                },
                "protocol_identities": predecessor_identities,
            }
        ),
        encoding="utf-8",
    )
    predecessor = SimpleNamespace(
        run_id="v4-predecessor",
        manifest_sha256="4" * 64,
        lineage=lineage_path,
        datasets=predecessor_datasets,
        artifacts=predecessor_artifacts,
        manifest=predecessor_root / "manifest.json",
    )
    validation_pointer = (
        data_root / "processed/validation_evaluation/CURRENT.json"
    )
    validation_pointer.parent.mkdir(parents=True)
    validation_pointer.write_text(
        json.dumps(
            {
                "run_id": validation.run_id,
                "manifest_sha256": validation.manifest_sha256,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        successor_release,
        "resolve_robustness_data_root",
        lambda _root: data_root,
    )
    monkeypatch.setattr(
        successor_release,
        "code_identity",
        lambda _root: {
            "commit": "c" * 40,
            "tree": "d" * 40,
            "dirty": False,
        },
    )
    monkeypatch.setattr(
        successor_release,
        "resolve_current",
        lambda root: (
            predecessor if root.name == "robustness" else validation
        ),
    )
    monkeypatch.setattr(
        successor_release,
        "build_final_execution_identity_at_revision",
        lambda *_args, **_kwargs: predecessor_identities["final_execution"],
    )
    monkeypatch.setattr(
        successor_release,
        "load_config",
        lambda _path: SimpleNamespace(supported_markets=("sh", "sz")),
    )
    monkeypatch.setattr(
        successor_release,
        "assert_validation_market_scope",
        lambda *_args: None,
    )

    def build_identities(
        _root: Path,
        *,
        research_records: dict[str, object],
    ) -> dict[str, dict[str, object]]:
        assert research_records == research_records_bound_to_v4
        return current_identities

    research_records_bound_to_v4 = research_records
    monkeypatch.setattr(
        successor_release,
        "build_protocol_identities",
        build_identities,
    )

    class ReachedChangeImpact(Exception):
        pass

    def reach_change_impact(*_args, **_kwargs) -> str:
        raise ReachedChangeImpact

    monkeypatch.setattr(
        successor_release,
        "verify_evidence_workflow_only_change_impact",
        reach_change_impact,
    )

    with pytest.raises(ReachedChangeImpact):
        successor_release.publish_evidence_workflow_successor_release(
            Path.cwd(),
            run_id="v4-successor",
            action_audit_root=tmp_path / "action",
            collector_readiness_root=tmp_path / "collector",
            evidence_workflow_readiness_root=tmp_path / "evidence",
            change_impact_audit_path=tmp_path / "change-impact.json",
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
            "missing_effective_date_preserved": True,
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
            "cross_symbol_disposition_rejected": True,
            "cross_symbol_fact_rejected": True,
            "missing_effective_date_requires_reconciliation": True,
            "null_original_comparison_safe": True,
        },
        "coverage_publication.json": {
            **common,
            "status": "passed",
            "atomic_pair_publication": True,
            "direct_review_without_batch_provenance_rejected": True,
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
    for report_name, field in (
        ("candidate_collection.json", "missing_effective_date_preserved"),
        (
            "review_submission.json",
            "missing_effective_date_requires_reconciliation",
        ),
        ("review_submission.json", "null_original_comparison_safe"),
    ):
        report_path = source / report_name
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report[field] = False
        report_path.write_text(json.dumps(report), encoding="utf-8")
        with pytest.raises(
            ValueError,
            match="complete evidence workflow rehearsal is not ready",
        ):
            write_evidence_workflow_readiness_audit(
                source,
                tmp_path / f"rejected-{field}",
                evidence_workflow_identity=identity,
            )
        report[field] = True
        report_path.write_text(json.dumps(report), encoding="utf-8")


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
        predecessor_final_execution_identity=_identities(evidence="v3")[
            "final_execution"
        ],
        change_impact_audit_sha256="5" * 64,
        evidence_workflow_readiness_audit_sha256="6" * 64,
    )

    assert sealed["protocol_version"] == 4
    assert sealed["protocol_identities"]["research"]["role"] == (
        "research_result_identity"
    )
    assert sealed["change_impact_audit_sha256"] == "5" * 64
    assert sealed["evidence_workflow_readiness_audit_sha256"] == "6" * 64
    assert sealed["predecessor_final_execution_identity"] == _identities(
        evidence="v3"
    )["final_execution"]


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
    review = json.loads(
        (audit / "review_submission.json").read_text(encoding="utf-8")
    )
    candidates = json.loads(
        (audit / "candidate_collection.json").read_text(encoding="utf-8")
    )
    assert (
        "tests.test_final_test_corporate_action_candidates::"
        "test_candidate_collection_preserves_missing_payment_date_for_review"
        in candidates["test_cases"]
    )
    assert candidates["missing_effective_date_preserved"] is True
    assert (
        "tests.test_final_test_official_review_batches::"
        "test_review_batch_rejects_unlinked_cross_symbol_corporate_fact"
        in review["test_cases"]
    )
    assert review["cross_symbol_fact_rejected"] is True
    assert (
        "tests.test_final_test_official_review_submission::"
        "test_review_submission_requires_missing_payment_date_to_be_corrected"
        in review["test_cases"]
    )
    assert review["missing_effective_date_requires_reconciliation"] is True
    assert review["null_original_comparison_safe"] is True
