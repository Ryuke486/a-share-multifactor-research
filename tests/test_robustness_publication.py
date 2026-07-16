import json
from pathlib import Path

import pytest

from ashare_multifactor.audit.publication import publish_release
from ashare_multifactor.audit.records import file_record
from ashare_multifactor.final_test.data_inventory import write_json
from ashare_multifactor.robustness.pipeline import (
    _successor_release_contract,
    verify_reproducible_source,
)


def test_publication_rechecks_source_files_against_reproducibility(tmp_path: Path) -> None:
    source = tmp_path / "run_2"
    source.mkdir()
    files = {
        "robustness_results.parquet": b"results",
        "report.md": b"report",
        "protocol_gate.json": b'{"sealed_test_protocol_allowed":true}',
    }
    import hashlib

    hashes = {}
    manifest_files = []
    for name, content in files.items():
        path = source / name
        path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        hashes[name] = digest
        manifest_files.append({"path": name, "sha256": digest, "size": len(content)})
    (source / "run_manifest.json").write_text(
        json.dumps({"run_id": "run_2", "code": {"dirty": False}, "inputs": {}, "files": manifest_files}),
        encoding="utf-8",
    )
    reproducibility = {
        "run_ids": ["run_1", "run_2"],
        "core_hashes": hashes,
        "code": {"dirty": False},
        "inputs": {},
    }

    verify_reproducible_source(source, reproducibility)
    (source / "report.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="source hash changed"):
        verify_reproducible_source(source, reproducibility)


def test_successor_publication_contract_binds_predecessor_fees_source_and_audit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data/processed/robustness"
    datasets = tmp_path / "staged/datasets"
    artifacts = tmp_path / "staged/artifacts"
    datasets.mkdir(parents=True)
    artifacts.mkdir(parents=True)
    (datasets / "result.bin").write_bytes(b"old")
    publish_release(
        root,
        run_id="old-stage8",
        staged_datasets=datasets,
        staged_artifacts=artifacts,
        lineage={"stage": "robustness"},
    )
    code = tmp_path / "code"
    (code / "configs").mkdir(parents=True)
    (code / "configs/market_rules.yaml").write_text(
        Path("configs/market_rules.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    source_contract = code / "configs/final_execution_sources.yaml"
    source_contract.write_text(
        Path("configs/final_execution_sources.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    audit = tmp_path / "audit"
    audit.mkdir()
    files = {}
    for name, role, content in (
        ("summary.json", "action_coverage_summary", b'{"status":"ready"}\n'),
        ("query_coverage.parquet", "action_query_coverage", b"coverage"),
        ("event_differences.parquet", "action_event_differences", b"diff"),
        ("official_evidence_index.parquet", "official_action_evidence_index", b"evidence"),
    ):
        path = audit / name
        path.write_bytes(content)
        files[name] = file_record(path, root=audit, role=role).to_dict()
    write_json(audit / "manifest.json", {"status": "ready", "files": list(files.values())})

    contract = _successor_release_contract(code, tmp_path / "data", audit)

    assert contract["predecessor"]["run_id"] == "old-stage8"
    assert len(contract["market_rules_sha256"]) == 64
    assert len(contract["action_source_contract_sha256"]) == 64
    assert len(contract["action_coverage_audit_sha256"]) == 64
