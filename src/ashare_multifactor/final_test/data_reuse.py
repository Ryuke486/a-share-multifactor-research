"""Explicit authorization binding for one immutable final-test data panel."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from ashare_multifactor.audit.publication import resolve_current, resolve_release
from ashare_multifactor.audit.records import sha256_file
from ashare_multifactor.config import ResearchConfig
from ashare_multifactor.data.discovery import discover_daily_pairs
from ashare_multifactor.final_test.data_inventory import build_input_inventory
from ashare_multifactor.final_test.data_publication import (
    FinalTestDataResolution,
    read_data_claim_bytes_at,
)
from ashare_multifactor.final_test.data_reuse_contract import (
    RECEIPT_ROLE,
    RECEIPT_SCHEMA_VERSION,
    RECEIPT_STATUS,
    authorization_identity,
    canonical_reuse_json_bytes,
    validate_data_reuse_receipt,
)
from ashare_multifactor.final_test.final_root_binding import FinalRootBinding
from ashare_multifactor.final_test.gate import (
    FINAL_TEST_END,
    FINAL_TEST_START,
    FinalTestAuthorization,
    _verify_sealed_payload,
)
from ashare_multifactor.final_test.historical_attempt import (
    load_historical_attempt_authorization,
)
from ashare_multifactor.final_test.recovery_secure_fs import (
    open_directory_at,
    read_bytes_at,
    write_bytes_exclusive_at,
)
from ashare_multifactor.robustness.change_impact import (
    verify_evidence_workflow_only_change_impact,
)
from ashare_multifactor.robustness.protocol_identities import (
    validate_domain_identity,
    validate_protocol_identities,
)


_OWNED_MODE = "owned_claim"
_REUSED_MODE = "verified_reuse"


def bind_final_test_data_panel(
    config: ResearchConfig,
    authorization: FinalTestAuthorization,
    resolution: FinalTestDataResolution,
    *,
    code_root: Path,
    root_binding: FinalRootBinding,
) -> dict[str, object]:
    """Bind a direct claim or publish one v4-only immutable reuse receipt."""
    root_binding.assert_bound()
    claim_bytes = read_data_claim_bytes_at(root_binding.final_fd)
    claim = _json_object(claim_bytes, label="final-test data claim")
    if _claim_matches_authorization(claim, authorization, resolution):
        return _binding_record(
            mode=_OWNED_MODE,
            relative_path="data-build-claim.json",
            payload=claim_bytes,
        )
    if claim.get("attempt_id") == authorization.attempt_id:
        raise ValueError("final-test data claim differs from authorization")
    if claim.get("status") != "published" or resolution.requires_recovery:
        raise ValueError("foreign final-test data claim is not published")
    return _publish_reuse_receipt(
        config,
        authorization,
        resolution,
        code_root=code_root,
        claim=claim,
        claim_bytes=claim_bytes,
        root_binding=root_binding,
    )


def verify_preparation_data_binding(
    final_root: Path,
    authorization: FinalTestAuthorization,
    resolution: FinalTestDataResolution,
    binding: object,
    *,
    final_fd: int | None = None,
    require_current_stage8: bool = True,
) -> None:
    """Verify the manifest-bound direct claim or v4 reuse receipt."""
    if final_fd is None:
        with FinalRootBinding.open(final_root) as opened:
            verify_preparation_data_binding(
                final_root,
                authorization,
                resolution,
                binding,
                final_fd=opened.final_fd,
                require_current_stage8=require_current_stage8,
            )
            return
    if not isinstance(binding, dict) or set(binding) != {
        "mode",
        "relative_path",
        "sha256",
        "size_bytes",
    }:
        raise ValueError("final-test data binding is invalid")
    mode = binding.get("mode")
    expected_relative = (
        "data-build-claim.json"
        if mode == _OWNED_MODE
        else f"data-reuse/{authorization.attempt_id}.json"
        if mode == _REUSED_MODE
        else None
    )
    if binding.get("relative_path") != expected_relative:
        raise ValueError("final-test data binding path is invalid")
    payload = _read_relative_at(
        final_fd,
        str(expected_relative),
        label="final-test data binding",
    )
    if (
        binding.get("sha256") != hashlib.sha256(payload).hexdigest()
        or binding.get("size_bytes") != len(payload)
    ):
        raise ValueError("final-test data binding identity changed")
    if mode == _OWNED_MODE:
        claim = _json_object(payload, label="final-test data claim")
        if not _claim_matches_authorization(claim, authorization, resolution):
            raise ValueError("final-test data claim differs from authorization")
        return
    _verify_reuse_receipt_at(
        final_root,
        authorization,
        resolution,
        payload,
        final_fd=final_fd,
        require_current_stage8=require_current_stage8,
    )


def verify_final_test_data_reuse(
    final_root: Path,
    authorization: FinalTestAuthorization,
    resolution: FinalTestDataResolution,
) -> None:
    """Verify the deterministic receipt when a shared claim belongs to another attempt."""
    with FinalRootBinding.open(final_root) as root_binding:
        relative = f"data-reuse/{authorization.attempt_id}.json"
        payload = _read_relative_at(
            root_binding.final_fd,
            relative,
            label="final-test data reuse",
        )
        _verify_reuse_receipt_at(
            final_root,
            authorization,
            resolution,
            payload,
            final_fd=root_binding.final_fd,
            require_current_stage8=True,
        )


def _publish_reuse_receipt(
    config: ResearchConfig,
    authorization: FinalTestAuthorization,
    resolution: FinalTestDataResolution,
    *,
    code_root: Path,
    claim: dict[str, Any],
    claim_bytes: bytes,
    root_binding: FinalRootBinding,
) -> dict[str, object]:
    source_attempt_id = str(claim.get("attempt_id", ""))
    if source_attempt_id == authorization.attempt_id:
        raise ValueError("final-test data reuse source equals destination")
    source = load_historical_attempt_authorization(
        code_root=code_root,
        data_root=config.paths.processed.parent,
        attempt_id=source_attempt_id,
    )
    _assert_source_claim(claim, source, resolution)
    sealed = _load_destination_protocol(
        config.paths.processed / "robustness",
        authorization,
        require_current=True,
    )
    inventory_bytes = _read_relative_at(
        root_binding.final_fd,
        "daily_panel/input_files.json",
        label="final-test panel input inventory",
    )
    recorded_inventory = _json_object(
        inventory_bytes,
        label="final-test panel input inventory",
    )
    current_inventory = build_input_inventory(
        config,
        FINAL_TEST_START,
        FINAL_TEST_END,
        discover=discover_daily_pairs,
    )
    _verify_source_input_inventory(recorded_inventory, current_inventory, source)
    identities = validate_protocol_identities(sealed.get("protocol_identities"))
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "role": RECEIPT_ROLE,
        "status": RECEIPT_STATUS,
        **authorization_identity(authorization),
        "source_attempt_id": source_attempt_id,
        "source_claim_sha256": hashlib.sha256(claim_bytes).hexdigest(),
        "source_claim_size_bytes": len(claim_bytes),
        "data_manifest_sha256": resolution.data_manifest_sha256,
        "input_inventory_sha256": hashlib.sha256(inventory_bytes).hexdigest(),
        "input_inventory_size_bytes": len(inventory_bytes),
        "protocol_version": 4,
        "protocol_identity_sha256": {
            domain: str(identity["sha256"])
            for domain, identity in identities.items()
        },
        "change_impact_audit_sha256": str(
            sealed["change_impact_audit_sha256"]
        ),
        "created_at": authorization.registered_at,
    }
    validate_data_reuse_receipt(
        receipt,
        authorization=authorization,
        source_attempt_id=source_attempt_id,
        source_claim_sha256=hashlib.sha256(claim_bytes).hexdigest(),
        source_claim_size_bytes=len(claim_bytes),
        data_manifest_sha256=resolution.data_manifest_sha256,
        input_inventory_sha256=hashlib.sha256(inventory_bytes).hexdigest(),
        input_inventory_size_bytes=len(inventory_bytes),
        sealed_protocol=sealed,
    )
    payload = canonical_reuse_json_bytes(receipt)
    try:
        os.mkdir("data-reuse", mode=0o700, dir_fd=root_binding.final_fd)
    except FileExistsError:
        pass
    reuse_fd = open_directory_at(
        root_binding.final_fd,
        "data-reuse",
        label="final-test data reuse",
    )
    try:
        name = f"{authorization.attempt_id}.json"
        try:
            existing = read_bytes_at(
                reuse_fd,
                name,
                label="final-test data reuse receipt",
            )
        except FileNotFoundError:
            write_bytes_exclusive_at(reuse_fd, name, payload)
        else:
            if existing != payload:
                raise ValueError("final-test data reuse receipt differs")
        os.fsync(reuse_fd)
    finally:
        os.close(reuse_fd)
    root_binding.assert_bound()
    return _binding_record(
        mode=_REUSED_MODE,
        relative_path=f"data-reuse/{authorization.attempt_id}.json",
        payload=payload,
    )


def _verify_source_input_inventory(
    recorded_inventory: dict[str, Any],
    current_inventory: dict[str, object],
    source: FinalTestAuthorization,
) -> None:
    raw_fields = ("schema_version", "period", "pairs")
    if set(recorded_inventory) != {*raw_fields, "authorization_identity"}:
        raise ValueError("final-test source input inventory schema is invalid")
    expected_authorization = {
        **authorization_identity(source),
        "registered_at": source.registered_at,
    }
    if recorded_inventory.get("authorization_identity") != expected_authorization:
        raise ValueError(
            "final-test source authorization identity differs in input inventory"
        )
    recorded_raw_inventory = {
        field: recorded_inventory[field]
        for field in raw_fields
    }
    if recorded_raw_inventory != current_inventory:
        raise ValueError("final-test raw inventory changed; panel reuse is forbidden")


def _verify_reuse_receipt_at(
    final_root: Path,
    authorization: FinalTestAuthorization,
    resolution: FinalTestDataResolution,
    receipt_bytes: bytes,
    *,
    final_fd: int,
    require_current_stage8: bool,
) -> None:
    receipt = _json_object(receipt_bytes, label="final-test data reuse receipt")
    claim_bytes = read_data_claim_bytes_at(final_fd)
    claim = _json_object(claim_bytes, label="final-test data claim")
    source_attempt_id = str(claim.get("attempt_id", ""))
    source = load_historical_attempt_authorization(
        data_root=final_root.parent.parent,
        attempt_id=source_attempt_id,
    )
    _assert_source_claim(claim, source, resolution)
    inventory_bytes = _read_relative_at(
        final_fd,
        "daily_panel/input_files.json",
        label="final-test panel input inventory",
    )
    sealed = _load_destination_protocol(
        final_root.parent / "robustness",
        authorization,
        require_current=require_current_stage8,
    )
    validate_data_reuse_receipt(
        receipt,
        authorization=authorization,
        source_attempt_id=source_attempt_id,
        source_claim_sha256=hashlib.sha256(claim_bytes).hexdigest(),
        source_claim_size_bytes=len(claim_bytes),
        data_manifest_sha256=resolution.data_manifest_sha256,
        input_inventory_sha256=hashlib.sha256(inventory_bytes).hexdigest(),
        input_inventory_size_bytes=len(inventory_bytes),
        sealed_protocol=sealed,
    )
    if receipt_bytes != canonical_reuse_json_bytes(receipt):
        raise ValueError("final-test data reuse receipt is not canonical")


def _load_destination_protocol(
    robustness_root: Path,
    authorization: FinalTestAuthorization,
    *,
    require_current: bool,
) -> dict[str, object]:
    release = (
        resolve_current(robustness_root)
        if require_current
        else resolve_release(robustness_root, authorization.robustness_release)
    )
    if (
        release.run_id != authorization.robustness_release
        or release.manifest_sha256 != authorization.robustness_manifest_sha256
        or sha256_file(release.lineage) != authorization.robustness_lineage_sha256
    ):
        raise ValueError("final-test data reuse Stage-8 identity differs")
    try:
        sealed = json.loads(
            (release.artifacts / "sealed_test_protocol.json").read_text(
                encoding="utf-8"
            )
        )
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError("final-test data reuse seal is invalid") from error
    if (
        not isinstance(sealed, dict)
        or _verify_sealed_payload(sealed) != authorization.sealed_protocol_sha256
        or sealed.get("protocol_version") != 4
    ):
        raise ValueError("final-test data reuse requires protocol v4")
    identities = validate_protocol_identities(sealed.get("protocol_identities"))
    predecessor_final_execution = validate_domain_identity(
        sealed.get("predecessor_final_execution_identity"),
        role="final_execution_identity",
    )
    audit_path = release.artifacts / "change_impact_audit.json"
    if verify_evidence_workflow_only_change_impact(
        audit_path,
        expected_current_identities=identities,
        expected_predecessor_final_execution_identity=(
            predecessor_final_execution
        ),
    ) != sealed.get("change_impact_audit_sha256"):
        raise ValueError("final-test data reuse change-impact audit differs")
    return sealed


def _assert_source_claim(
    claim: dict[str, Any],
    source: FinalTestAuthorization,
    resolution: FinalTestDataResolution,
) -> None:
    expected = {
        **authorization_identity(source),
        "status": "published",
    }
    if any(claim.get(key) != value for key, value in expected.items()):
        raise ValueError("final-test data reuse source claim differs")
    manifest = claim.get("data_manifest")
    if (
        not isinstance(manifest, dict)
        or manifest.get("relative_path") != "daily_panel/data_manifest.json"
        or manifest.get("sha256") != resolution.data_manifest_sha256
        or not isinstance(manifest.get("size_bytes"), int)
    ):
        raise ValueError("final-test data reuse source manifest differs")


def _claim_matches_authorization(
    claim: dict[str, Any],
    authorization: FinalTestAuthorization,
    resolution: FinalTestDataResolution,
) -> bool:
    try:
        _assert_source_claim(claim, authorization, resolution)
    except ValueError:
        return False
    return True


def _binding_record(
    *,
    mode: str,
    relative_path: str,
    payload: bytes,
) -> dict[str, object]:
    return {
        "mode": mode,
        "relative_path": relative_path,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }


def _read_relative_at(final_fd: int, relative: str, *, label: str) -> bytes:
    parts = Path(relative).parts
    if (
        Path(relative).is_absolute()
        or ".." in parts
        or len(parts) not in {1, 2}
    ):
        raise ValueError(f"{label} path is invalid")
    if len(parts) == 1:
        return read_bytes_at(final_fd, parts[0], label=label)
    directory_fd = open_directory_at(final_fd, parts[0], label=label)
    try:
        return read_bytes_at(directory_fd, parts[1], label=label)
    finally:
        os.close(directory_fd)


def _json_object(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} is invalid") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{label} is invalid")
    return payload
