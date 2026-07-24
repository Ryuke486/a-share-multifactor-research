from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any

from ashare_multifactor.audit.records import sha256_file, verify_file_record


@dataclass(frozen=True)
class Stage8Supersession:
    run_id: str
    manifest_sha256: str
    reason: str
    status: str = "superseded_for_final_execution"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def build_stage8_supersession(
    *,
    predecessor_pointer: dict[str, object],
    predecessor_manifest: Path,
    reason: str,
) -> Stage8Supersession:
    actual = sha256_file(predecessor_manifest)
    if predecessor_pointer.get("manifest_sha256") != actual:
        raise ValueError("predecessor Stage-8 manifest changed")
    run_id = str(predecessor_pointer.get("run_id", ""))
    if not run_id or not reason.strip():
        raise ValueError("Stage-8 supersession identity is incomplete")
    return Stage8Supersession(
        run_id=run_id,
        manifest_sha256=actual,
        reason=reason.strip(),
    )


def verify_action_coverage_audit(root: Path) -> str:
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError("invalid Stage-8 action coverage audit manifest") from error
    records = manifest.get("files") if isinstance(manifest, dict) else None
    if manifest.get("status") != "ready" or not isinstance(records, list):
        raise ValueError("Stage-8 action coverage audit is not ready")
    required_roles = {
        "action_coverage_summary",
        "action_query_coverage",
        "action_event_differences",
        "official_action_evidence_index",
    }
    if {
        record.get("role") for record in records if isinstance(record, dict)
    } != required_roles or len(records) != len(required_roles):
        raise ValueError("Stage-8 action coverage audit file set is incomplete")
    resolved = {}
    for record in records:
        try:
            resolved[str(record["role"])] = verify_file_record(record, root=root)
        except (FileNotFoundError, TypeError, ValueError) as error:
            raise ValueError("Stage-8 action coverage audit hash changed") from error
    summary = json.loads(resolved["action_coverage_summary"].read_text(encoding="utf-8"))
    if summary.get("status") != "ready" or summary.get("event_difference_rows") not in {
        None,
        0,
    } or summary.get("official_sample_gaps") not in {None, 0}:
        raise ValueError("Stage-8 action coverage audit gates are not closed")
    return sha256_file(manifest_path)


def build_stage8_successor_lineage(
    base: dict[str, Any],
    *,
    supersession: Stage8Supersession,
    action_source_contract_sha256: str,
    action_coverage_audit_sha256: str,
    collector_readiness_audit_sha256: str,
) -> dict[str, Any]:
    for value in (
        action_source_contract_sha256,
        action_coverage_audit_sha256,
        collector_readiness_audit_sha256,
    ):
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("Stage-8 successor lineage hash is invalid")
    lineage = dict(base)
    lineage["predecessor"] = supersession.to_dict()
    lineage["execution_protocol"] = {
        "action_source_contract_sha256": action_source_contract_sha256,
        "action_coverage_audit_sha256": action_coverage_audit_sha256,
        "collector_readiness_audit_sha256": collector_readiness_audit_sha256,
        "status": "ready_for_new_final_test_authorization",
    }
    return lineage
