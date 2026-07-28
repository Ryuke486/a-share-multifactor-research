"""Protocol-only Stage-8 successor publication for evidence-workflow changes."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import shutil

from ashare_multifactor.audit.identity import code_identity
from ashare_multifactor.audit.publication import publish_release, resolve_current
from ashare_multifactor.audit.records import sha256_file
from ashare_multifactor.config import load_config
from ashare_multifactor.execution.fee_protocol import validate_fee_protocol
from ashare_multifactor.execution.fees import load_market_rules
from ashare_multifactor.robustness.change_impact import (
    verify_evidence_workflow_only_change_impact,
)
from ashare_multifactor.robustness.collector_readiness import (
    verify_collector_readiness_audit,
)
from ashare_multifactor.robustness.evidence_workflow_readiness import (
    verify_evidence_workflow_readiness_audit,
)
from ashare_multifactor.robustness.release_context import (
    assert_validation_market_scope,
    resolve_robustness_data_root,
)
from ashare_multifactor.robustness.protocol import load_robustness_protocol
from ashare_multifactor.robustness.protocol_identities import (
    build_final_execution_identity_at_revision,
    build_protocol_identities,
    identity_payload,
)
from ashare_multifactor.robustness.successor_seal import (
    build_stage8_successor_lineage,
    build_stage8_supersession,
    verify_action_coverage_audit,
)
from ashare_multifactor.robustness.test_protocol import seal_test_protocol


def publish_evidence_workflow_successor_release(
    code_root: Path,
    *,
    run_id: str,
    action_audit_root: Path,
    collector_readiness_root: Path,
    evidence_workflow_readiness_root: Path,
    change_impact_audit_path: Path,
) -> object:
    """Reseal unchanged Stage-7/8 results after bounded workflow rehearsals."""
    code_root = code_root.resolve()
    data_root = resolve_robustness_data_root(code_root)
    identity = code_identity(code_root)
    if identity.get("dirty") is not False:
        raise ValueError("evidence-workflow successor requires a clean Git identity")
    predecessor = resolve_current(data_root / "processed/robustness")
    predecessor_lineage = json.loads(predecessor.lineage.read_text(encoding="utf-8"))
    predecessor_sealed = json.loads(
        (predecessor.artifacts / "sealed_test_protocol.json").read_text(
            encoding="utf-8"
        )
    )
    predecessor_code = predecessor_sealed.get("code")
    if (
        predecessor_sealed.get("protocol_version") != 3
        or predecessor_sealed.get("status") != "sealed"
        or not isinstance(predecessor_code, dict)
        or predecessor_code.get("dirty") is not False
    ):
        raise ValueError("predecessor Stage-8 seal is not protocol v3")
    predecessor_final_execution_identity = (
        build_final_execution_identity_at_revision(
            code_root,
            commit=str(predecessor_code.get("commit", "")),
            tree=str(predecessor_code.get("tree", "")),
        )
    )
    validation = resolve_current(data_root / "processed/validation_evaluation")
    research_config = load_config(code_root / "configs/research_protocol.yaml")
    assert_validation_market_scope(validation, research_config.supported_markets)
    validation_pointer = json.loads(
        (data_root / "processed/validation_evaluation/CURRENT.json").read_text(
            encoding="utf-8"
        )
    )
    research_records = {
        "validation_run_id": validation.run_id,
        "validation_manifest_sha256": validation.manifest_sha256,
        "predecessor_stage8_run_id": predecessor.run_id,
        "predecessor_stage8_manifest_sha256": predecessor.manifest_sha256,
        "predecessor_stage8_lineage_sha256": sha256_file(predecessor.lineage),
        "robustness_results_sha256": sha256_file(
            predecessor.datasets / "robustness_results.parquet"
        ),
        "report_sha256": sha256_file(predecessor.artifacts / "report.md"),
        "protocol_gate_sha256": sha256_file(
            predecessor.artifacts / "protocol_gate.json"
        ),
    }
    expected_identities = build_protocol_identities(
        code_root,
        research_records=research_records,
    )
    change_impact_sha256 = verify_evidence_workflow_only_change_impact(
        change_impact_audit_path,
        expected_current_identities=expected_identities,
        expected_predecessor_final_execution_identity=(
            predecessor_final_execution_identity
        ),
    )
    if (
        expected_identities["research"]
        != identity_payload("research_result_identity", research_records)
    ):
        raise ValueError("change-impact audit does not permit protocol-only reseal")
    action_audit_sha256 = verify_action_coverage_audit(action_audit_root)
    collector_sha256 = verify_collector_readiness_audit(collector_readiness_root)
    evidence_readiness_sha256 = verify_evidence_workflow_readiness_audit(
        evidence_workflow_readiness_root
    )
    evidence_readiness = json.loads(
        (evidence_workflow_readiness_root / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    if (
        evidence_readiness.get("evidence_workflow_identity")
        != expected_identities["evidence_workflow"]
    ):
        raise ValueError("evidence readiness was produced by different workflow code")
    source_contract = code_root / "configs/final_execution_sources.yaml"
    market_rules = code_root / "configs/market_rules.yaml"
    validate_fee_protocol(
        load_market_rules(market_rules),
        date(2022, 1, 1),
        date(2025, 12, 31),
    )
    gate = json.loads(
        (predecessor.artifacts / "protocol_gate.json").read_text(encoding="utf-8")
    )
    if gate.get("sealed_test_protocol_allowed") is not True:
        raise ValueError("predecessor robustness gate does not permit resealing")
    supersession = build_stage8_supersession(
        predecessor_pointer={
            "run_id": predecessor.run_id,
            "manifest_sha256": predecessor.manifest_sha256,
        },
        predecessor_manifest=predecessor.manifest,
        reason=(
            "predecessor evidence workflow lacked validated PDF admission, "
            "review publication, atomic coverage publication, and evidence import"
        ),
    )
    staged = data_root / "artifacts/robustness/release_staging" / run_id
    if staged.exists():
        raise FileExistsError(staged)
    datasets = staged / "datasets"
    artifacts = staged / "artifacts"
    datasets.mkdir(parents=True)
    artifacts.mkdir()
    shutil.copy2(predecessor.datasets / "robustness_results.parquet", datasets)
    shutil.copy2(predecessor.artifacts / "report.md", artifacts)
    shutil.copy2(predecessor.artifacts / "protocol_gate.json", artifacts)
    shutil.copytree(action_audit_root, artifacts / "action_coverage_audit")
    shutil.copytree(
        collector_readiness_root,
        artifacts / "collector_readiness_audit",
    )
    shutil.copytree(
        evidence_workflow_readiness_root,
        artifacts / "evidence_workflow_readiness",
    )
    shutil.copy2(change_impact_audit_path, artifacts / "change_impact_audit.json")
    protocol = load_robustness_protocol(code_root / "configs/robustness_protocol.yaml")
    sealed = seal_test_protocol(
        artifacts / "sealed_test_protocol.json",
        protocol=protocol,
        code_identity=identity,
        validation_pointer=validation_pointer,
        market_rules_sha256=sha256_file(market_rules),
        report_template_sha256=sha256_file(
            code_root / "docs/templates/stage9-final-test-report-template.md"
        ),
        opening_ledger_root=(
            data_root / "processed/robustness/final_test_opening_ledger"
        ),
        gate=gate,
        supported_markets=research_config.supported_markets,
        action_source_contract_sha256=sha256_file(source_contract),
        action_coverage_audit_sha256=action_audit_sha256,
        collector_readiness_audit_sha256=collector_sha256,
        predecessor=supersession.to_dict(),
        protocol_identities=expected_identities,
        predecessor_final_execution_identity=(
            predecessor_final_execution_identity
        ),
        change_impact_audit_sha256=change_impact_sha256,
        evidence_workflow_readiness_audit_sha256=evidence_readiness_sha256,
    )
    lineage = {
        "stage": "robustness",
        "period": ["2005-01-01", "2021-12-31"],
        "sealed_test_start": "2022-01-01",
        "main_candidate": protocol.main_candidate,
        "code": identity,
        "inputs": predecessor_lineage.get("inputs"),
        "reproducibility": predecessor_lineage.get("reproducibility"),
        "research_reuse": research_records,
        "sealed_protocol_sha256": sealed["sealed_protocol_sha256"],
        "supported_markets": list(research_config.supported_markets),
    }
    lineage = build_stage8_successor_lineage(
        lineage,
        supersession=supersession,
        action_source_contract_sha256=sha256_file(source_contract),
        action_coverage_audit_sha256=action_audit_sha256,
        collector_readiness_audit_sha256=collector_sha256,
        protocol_identities=expected_identities,
        predecessor_final_execution_identity=(
            predecessor_final_execution_identity
        ),
        change_impact_audit_sha256=change_impact_sha256,
        evidence_workflow_readiness_audit_sha256=evidence_readiness_sha256,
    )
    lineage["execution_protocol"]["supported_markets"] = list(
        research_config.supported_markets
    )
    return publish_release(
        data_root / "processed/robustness",
        run_id=run_id,
        staged_datasets=datasets,
        staged_artifacts=artifacts,
        lineage=lineage,
        manifest_metadata={
            "stage": "robustness",
            "research_results_reused": True,
        },
    )
