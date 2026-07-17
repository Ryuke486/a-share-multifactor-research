import json
from pathlib import Path

from ashare_multifactor.audit.records import sha256_file
from ashare_multifactor.robustness.protocol import load_robustness_protocol
from ashare_multifactor.robustness.successor_seal import (
    build_stage8_successor_lineage,
    build_stage8_supersession,
    verify_action_coverage_audit,
)
from ashare_multifactor.robustness.test_protocol import seal_test_protocol


def _identity() -> dict[str, object]:
    return {
        "commit": "a" * 40,
        "dirty": False,
        "diff_sha256": "b" * 64,
        "sources": [],
        "python": "3.14.6",
        "dependencies": {},
    }


def test_successor_seal_preserves_predecessor_and_binds_execution_contracts(
    tmp_path: Path,
) -> None:
    predecessor_manifest = tmp_path / "predecessor-manifest.json"
    predecessor_manifest.write_text('{"release":"old"}\n', encoding="utf-8")
    predecessor_hash = sha256_file(predecessor_manifest)
    pointer = {
        "run_id": "58adad4_stage8_robustness",
        "manifest_sha256": predecessor_hash,
    }
    before = predecessor_manifest.read_bytes()
    supersession = build_stage8_supersession(
        predecessor_pointer=pointer,
        predecessor_manifest=predecessor_manifest,
        reason="final execution fee and corporate-action coverage incomplete",
    )

    sealed = seal_test_protocol(
        tmp_path / "sealed.json",
        protocol=load_robustness_protocol(Path("configs/robustness_protocol.yaml")),
        code_identity=_identity(),
        validation_pointer={
            "run_id": "2acbfd8_stage7_validation_two_phase_shsz_successor",
            "manifest_sha256": "e" * 64,
        },
        market_rules_sha256="1" * 64,
        action_source_contract_sha256="2" * 64,
        action_coverage_audit_sha256="3" * 64,
        predecessor=supersession.to_dict(),
        report_template_sha256="4" * 64,
        opening_ledger_root=tmp_path / "opening-ledger",
        gate={"sealed_test_protocol_allowed": True, "status": "ready_to_seal"},
    )

    assert predecessor_manifest.read_bytes() == before
    assert sealed["protocol_version"] == 2
    assert sealed["action_source_contract_sha256"] == "2" * 64
    assert sealed["action_coverage_audit_sha256"] == "3" * 64
    assert sealed["predecessor"] == supersession.to_dict()
    assert sealed["opening_token_status"] == "closed"
    assert json.loads((tmp_path / "sealed.json").read_text()) == sealed


def test_supersession_rejects_predecessor_manifest_drift(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")

    try:
        build_stage8_supersession(
            predecessor_pointer={
                "run_id": "old",
                "manifest_sha256": "0" * 64,
            },
            predecessor_manifest=manifest,
            reason="coverage incomplete",
        )
    except ValueError as error:
        assert "predecessor Stage-8 manifest changed" in str(error)
    else:
        raise AssertionError("drifted predecessor manifest was accepted")


def test_successor_lineage_requires_ready_hashed_action_audit(tmp_path: Path) -> None:
    audit = tmp_path / "audit"
    audit.mkdir()
    summary = audit / "summary.json"
    summary.write_text('{"status":"ready"}\n', encoding="utf-8")
    files = {
        "summary.json": (summary, "action_coverage_summary"),
        "query_coverage.parquet": (
            audit / "query_coverage.parquet",
            "action_query_coverage",
        ),
        "event_differences.parquet": (
            audit / "event_differences.parquet",
            "action_event_differences",
        ),
        "official_evidence_index.parquet": (
            audit / "official_evidence_index.parquet",
            "official_action_evidence_index",
        ),
    }
    for name, (path, _role) in files.items():
        if name != "summary.json":
            path.write_bytes(name.encode())
    manifest = {
        "status": "ready",
        "files": [
            {
                "path": name,
                "role": role,
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
            for name, (path, role) in files.items()
        ],
    }
    (audit / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )

    audit_hash = verify_action_coverage_audit(audit)
    predecessor = tmp_path / "old-manifest.json"
    predecessor.write_text('{"release":"old"}\n', encoding="utf-8")
    supersession = build_stage8_supersession(
        predecessor_pointer={
            "run_id": "old",
            "manifest_sha256": sha256_file(predecessor),
        },
        predecessor_manifest=predecessor,
        reason="coverage incomplete",
    )
    lineage = build_stage8_successor_lineage(
        {"stage": "robustness"},
        supersession=supersession,
        action_source_contract_sha256="a" * 64,
        action_coverage_audit_sha256=audit_hash,
    )

    assert lineage["predecessor"]["run_id"] == "old"
    assert lineage["execution_protocol"]["action_coverage_audit_sha256"] == audit_hash
