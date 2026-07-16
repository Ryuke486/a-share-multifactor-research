from __future__ import annotations

from collections.abc import Callable
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from ashare_multifactor.robustness.protocol import RobustnessProtocol


def seal_test_protocol(
    destination: Path,
    *,
    protocol: RobustnessProtocol,
    code_identity: dict[str, object],
    validation_pointer: dict[str, object],
    market_rules_sha256: str,
    report_template_sha256: str,
    opening_ledger_root: Path,
    gate: dict[str, object],
    action_source_contract_sha256: str | None = None,
    action_coverage_audit_sha256: str | None = None,
    predecessor: dict[str, object] | None = None,
    supported_markets: tuple[str, ...] = ("sh", "sz"),
) -> dict[str, Any]:
    """Write the complete Stage-9 contract without opening or scanning test data."""
    if code_identity.get("dirty") is not False:
        raise ValueError("sealed test protocol requires a clean Git identity")
    if gate.get("sealed_test_protocol_allowed") is not True:
        raise ValueError("robustness gate does not allow test protocol sealing")
    if supported_markets != ("sh", "sz"):
        raise ValueError("sealed protocol supported markets differ from research scope")
    if validation_pointer.get("run_id") != protocol.validation_release:
        raise ValueError("validation release differs from the frozen robustness protocol")
    successor_values = (
        action_source_contract_sha256,
        action_coverage_audit_sha256,
        predecessor,
    )
    is_successor = any(value is not None for value in successor_values)
    if is_successor:
        if any(value is None for value in successor_values):
            raise ValueError("Stage-8 successor execution contract is incomplete")
        if not _valid_sha256(str(action_source_contract_sha256)) or not _valid_sha256(
            str(action_coverage_audit_sha256)
        ):
            raise ValueError("Stage-8 successor execution contract hash is invalid")
        if (
            not isinstance(predecessor, dict)
            or predecessor.get("status") != "superseded_for_final_execution"
            or not predecessor.get("run_id")
            or not _valid_sha256(str(predecessor.get("manifest_sha256", "")))
            or not predecessor.get("reason")
        ):
            raise ValueError("Stage-8 successor predecessor identity is invalid")
    payload: dict[str, Any] = {
        "protocol_version": 2 if is_successor else 1,
        "status": "sealed",
        "robustness_protocol_sha256": protocol.protocol_sha256,
        "code": code_identity,
        "upstream_validation": validation_pointer,
        "main_candidate": protocol.main_candidate,
        "test_period": [
            protocol.sealed_test_start.isoformat(),
            protocol.sealed_test_end.isoformat(),
        ],
        "cost_model": "full_audited_cost_model",
        "supported_markets": list(supported_markets),
        "metrics": list(protocol.final_test_metrics),
        "market_rules_sha256": market_rules_sha256,
        "report_template_sha256": report_template_sha256,
        "random_seed": protocol.random_seed,
        "one_authoritative_run": True,
        "failed_runs_must_be_retained": True,
        "opening_token_required": True,
        "opening_token_status": "closed",
        "opening_token_algorithm": "HMAC-SHA256-with-external-user-key",
        "opening_token_consumption": "atomic_one_shot_ledger",
        "opening_ledger_root": str(opening_ledger_root.resolve()),
        "robustness_gate": gate,
    }
    if is_successor:
        payload.update(
            {
                "action_source_contract_sha256": action_source_contract_sha256,
                "action_coverage_audit_sha256": action_coverage_audit_sha256,
                "predecessor": predecessor,
            }
        )
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    payload["sealed_protocol_sha256"] = hashlib.sha256(canonical).hexdigest()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def verify_test_opening_token(
    token_path: Path,
    sealed_protocol: dict[str, object],
    *,
    approval_key: bytes,
    before_scan: Callable[[], object] | None = None,
) -> dict[str, object]:
    """Validate explicit Stage-9 approval before invoking any test-data scanner."""
    try:
        token = json.loads(token_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise ValueError("invalid final-test opening token") from exc
    sealed_copy = dict(sealed_protocol)
    recorded_seal = str(sealed_copy.pop("sealed_protocol_sha256", ""))
    canonical = json.dumps(
        sealed_copy, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    if not recorded_seal or not hmac.compare_digest(
        recorded_seal, hashlib.sha256(canonical).hexdigest()
    ):
        raise ValueError("sealed protocol hash does not match its payload")
    if (
        sealed_protocol.get("status") != "sealed"
        or sealed_protocol.get("opening_token_status") != "closed"
    ):
        raise ValueError("sealed protocol is not closed")
    approval_id = str(token.get("approval_id", ""))
    seal = recorded_seal
    identity_fields = (
        "robustness_release", "robustness_manifest_sha256", "robustness_lineage_sha256"
    )
    if any(not token.get(field) for field in identity_fields):
        raise ValueError("opening token lacks frozen Stage-8 identity")
    identity = "|".join(
        (seal, *(str(token[field]) for field in identity_fields))
    )
    if len(approval_key) < 16:
        raise ValueError("final-test approval key is too short")
    expected = hmac.new(
        approval_key,
        f"{identity}|{approval_id}|stage9-one-shot".encode(),
        hashlib.sha256,
    ).hexdigest()
    valid = (
        token.get("status") == "approved"
        and token.get("sealed_protocol_sha256") == seal
        and bool(approval_id)
        and hmac.compare_digest(str(token.get("signature", "")), expected)
    )
    if not valid:
        raise ValueError("invalid final-test opening token")
    ledger_root = Path(str(sealed_protocol.get("opening_ledger_root", "")))
    if not ledger_root.is_absolute():
        raise ValueError("sealed protocol opening ledger root is invalid")
    consumption_ledger = ledger_root / f"{seal}.json"
    consumption_ledger.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "status": "consumed",
        "approval_id": approval_id,
        "sealed_protocol_sha256": seal,
        **{field: token[field] for field in identity_fields},
        "token_sha256": hashlib.sha256(token_path.read_bytes()).hexdigest(),
    }
    try:
        with consumption_ledger.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
    except FileExistsError as exc:
        raise ValueError("final-test opening token has already been consumed") from exc
    if before_scan is not None:
        try:
            before_scan()
        except BaseException:
            record["status"] = "failed_after_open"
            consumption_ledger.write_text(
                json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            raise
    return token
