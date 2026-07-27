"""Fail-closed change impact for protocol-only Stage-8 successors."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ashare_multifactor.robustness.protocol_identities import (
    validate_protocol_identities,
)


_SCHEMA_VERSION = "1"


def write_change_impact_audit(
    destination: Path,
    *,
    predecessor_identities: dict[str, dict[str, object]],
    current_identities: dict[str, dict[str, object]],
) -> dict[str, Any]:
    """Record exactly which identity changed and therefore which work must rerun."""
    predecessor = validate_protocol_identities(predecessor_identities)
    current = validate_protocol_identities(current_identities)
    changed = [
        domain
        for domain in ("research", "final_execution", "evidence_workflow")
        if predecessor[domain]["sha256"] != current[domain]["sha256"]
    ]
    payload: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "role": "stage8_change_impact_audit",
        "status": "ready",
        "predecessor_identities": predecessor,
        "current_identities": current,
        "changed_domains": changed,
        "research_replay_required": "research" in changed,
        "final_execution_rehearsal_required": "final_execution" in changed,
        "evidence_workflow_rehearsal_required": "evidence_workflow" in changed,
        "stage7_stage8_research_results_reusable": "research" not in changed,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(_canonical_json_bytes(payload))
    verify_change_impact_audit(destination)
    return payload


def verify_change_impact_audit(path: Path) -> str:
    """Verify both the audit hash and the logical rerun decisions."""
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("change-impact audit is invalid") from error
    if not isinstance(payload, dict) or raw != _canonical_json_bytes(payload):
        raise ValueError("change-impact audit is not canonical")
    predecessor = validate_protocol_identities(payload.get("predecessor_identities"))
    current = validate_protocol_identities(payload.get("current_identities"))
    changed = [
        domain
        for domain in ("research", "final_execution", "evidence_workflow")
        if predecessor[domain]["sha256"] != current[domain]["sha256"]
    ]
    if (
        payload.get("schema_version") != _SCHEMA_VERSION
        or payload.get("role") != "stage8_change_impact_audit"
        or payload.get("status") != "ready"
        or payload.get("changed_domains") != changed
        or payload.get("research_replay_required") != ("research" in changed)
        or payload.get("final_execution_rehearsal_required")
        != ("final_execution" in changed)
        or payload.get("evidence_workflow_rehearsal_required")
        != ("evidence_workflow" in changed)
        or payload.get("stage7_stage8_research_results_reusable")
        != ("research" not in changed)
    ):
        raise ValueError("change-impact audit decisions are inconsistent")
    return hashlib.sha256(raw).hexdigest()


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()
