"""Canonical contract for an immutable final-test data reuse receipt."""

from __future__ import annotations

import json

from ashare_multifactor.final_test.gate import FinalTestAuthorization
from ashare_multifactor.robustness.protocol_identities import (
    validate_protocol_identities,
)


RECEIPT_SCHEMA_VERSION = "1"
RECEIPT_ROLE = "final_test_data_reuse"
RECEIPT_STATUS = "reused_verified_immutable_panel"


def validate_data_reuse_receipt(
    payload: object,
    *,
    authorization: FinalTestAuthorization,
    source_attempt_id: str,
    source_claim_sha256: str,
    source_claim_size_bytes: int,
    data_manifest_sha256: str,
    input_inventory_sha256: str,
    input_inventory_size_bytes: int,
    sealed_protocol: dict[str, object],
) -> dict[str, object]:
    """Validate the complete reuse identity without touching the filesystem."""
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "role",
        "status",
        "attempt_id",
        "approval_id",
        "git_commit",
        "git_tree",
        "sealed_protocol_sha256",
        "robustness_release",
        "robustness_manifest_sha256",
        "robustness_lineage_sha256",
        "source_attempt_id",
        "source_claim_sha256",
        "source_claim_size_bytes",
        "data_manifest_sha256",
        "input_inventory_sha256",
        "input_inventory_size_bytes",
        "protocol_version",
        "protocol_identity_sha256",
        "change_impact_audit_sha256",
        "created_at",
    }:
        raise ValueError("final-test data reuse receipt is invalid")
    if (
        payload.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or payload.get("role") != RECEIPT_ROLE
        or payload.get("status") != RECEIPT_STATUS
        or any(
            payload.get(key) != value
            for key, value in authorization_identity(authorization).items()
        )
        or payload.get("created_at") != authorization.registered_at
    ):
        raise ValueError("final-test data reuse authorization differs")
    if (
        payload.get("source_attempt_id") != source_attempt_id
        or payload.get("source_claim_sha256") != source_claim_sha256
        or payload.get("source_claim_size_bytes") != source_claim_size_bytes
    ):
        raise ValueError("final-test data reuse source claim differs")
    if payload.get("data_manifest_sha256") != data_manifest_sha256:
        raise ValueError("final-test data reuse data manifest differs")
    if (
        payload.get("input_inventory_sha256") != input_inventory_sha256
        or payload.get("input_inventory_size_bytes") != input_inventory_size_bytes
    ):
        raise ValueError("final-test data reuse input inventory differs")
    if (
        payload.get("protocol_version") != 4
        or sealed_protocol.get("protocol_version") != 4
    ):
        raise ValueError("final-test data reuse requires protocol v4")
    identities = validate_protocol_identities(
        sealed_protocol.get("protocol_identities")
    )
    expected_identity_hashes = {
        domain: str(identity["sha256"])
        for domain, identity in identities.items()
    }
    if (
        payload.get("protocol_identity_sha256") != expected_identity_hashes
        or payload.get("change_impact_audit_sha256")
        != sealed_protocol.get("change_impact_audit_sha256")
    ):
        raise ValueError("final-test data reuse protocol identity differs")
    return payload


def authorization_identity(
    authorization: FinalTestAuthorization,
) -> dict[str, str]:
    return {
        "attempt_id": authorization.attempt_id,
        "approval_id": authorization.approval_id,
        "git_commit": authorization.git_commit,
        "git_tree": authorization.git_tree,
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "robustness_release": authorization.robustness_release,
        "robustness_manifest_sha256": authorization.robustness_manifest_sha256,
        "robustness_lineage_sha256": authorization.robustness_lineage_sha256,
    }


def canonical_reuse_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
