"""Read-only verification of an older non-authoritative final-test attempt."""

from __future__ import annotations

import json
from pathlib import Path
import re
import stat

from ashare_multifactor.audit.publication import resolve_release
from ashare_multifactor.audit.records import sha256_file
from ashare_multifactor.final_test.gate import (
    FINAL_TEST_END,
    FINAL_TEST_START,
    FinalTestAuthorization,
    _git,
    _valid_git_oid,
    _verify_sealed_payload,
)
from ashare_multifactor.final_test.registry import (
    resolve_attempt_state_readonly,
    validate_publication_id,
)


_SHA256 = re.compile(r"[0-9a-f]{64}")


def load_historical_attempt_authorization(
    *,
    data_root: Path,
    attempt_id: str,
    code_root: Path | None = None,
) -> FinalTestAuthorization:
    """Verify an older source attempt without its secret or CURRENT pointer."""
    validate_publication_id(attempt_id)
    data_root = _safe_existing_root(data_root, label="data")
    resolved_code_root = (
        _safe_existing_root(code_root, label="code")
        if code_root is not None
        else None
    )
    registry = data_root / "processed/final_test/attempts"
    if registry.is_symlink() or not registry.is_dir():
        raise ValueError("historical final-test source registry is unsafe")
    record = resolve_attempt_state_readonly(registry, attempt_id)
    required = (
        "approval_id",
        "registered_at",
        "git_commit",
        "git_tree",
        "sealed_protocol_sha256",
        "robustness_release",
        "robustness_manifest_sha256",
        "robustness_lineage_sha256",
        "token_sha256",
    )
    if (
        record.get("attempt_id") != attempt_id
        or record.get("status") != "registered"
        or record.get("authoritative") is not False
        or record.get("state") != "awaiting_official_evidence"
        or any(
            not isinstance(record.get(field), str) or not record[field]
            for field in required
        )
        or any(
            _SHA256.fullmatch(str(record[field])) is None
            for field in (
                "sealed_protocol_sha256",
                "robustness_manifest_sha256",
                "robustness_lineage_sha256",
                "token_sha256",
            )
        )
    ):
        raise ValueError("historical final-test source registration is invalid")
    token_path = registry / f"{attempt_id}.token"
    _assert_safe_file(token_path, label="historical final-test source token")
    if sha256_file(token_path) != record["token_sha256"]:
        raise ValueError("historical final-test source token snapshot changed")

    release = resolve_release(
        data_root / "processed/robustness",
        str(record["robustness_release"]),
    )
    if (
        release.manifest_sha256 != record["robustness_manifest_sha256"]
        or sha256_file(release.lineage) != record["robustness_lineage_sha256"]
    ):
        raise ValueError("historical final-test source Stage-8 release changed")
    try:
        sealed = json.loads(
            (release.artifacts / "sealed_test_protocol.json").read_text(
                encoding="utf-8"
            )
        )
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError("historical final-test source seal is invalid") from error
    if not isinstance(sealed, dict):
        raise ValueError("historical final-test source seal is invalid")
    seal_sha256 = _verify_sealed_payload(sealed)
    sealed_code = sealed.get("code")
    if (
        seal_sha256 != record["sealed_protocol_sha256"]
        or not isinstance(sealed_code, dict)
        or sealed_code.get("commit") != record["git_commit"]
        or sealed_code.get("tree") != record["git_tree"]
        or not _valid_git_oid(str(record["git_commit"]))
        or not _valid_git_oid(str(record["git_tree"]))
    ):
        raise ValueError("historical final-test source code or seal identity changed")
    if resolved_code_root is not None and (
        _git(
            resolved_code_root,
            "rev-parse",
            f"{record['git_commit']}^{{tree}}",
        ).strip()
        != record["git_tree"]
    ):
        raise ValueError("historical final-test source Git object changed")
    _verify_consumption_ledger(
        sealed,
        record=record,
        attempt_id=attempt_id,
    )
    return FinalTestAuthorization(
        attempt_id=attempt_id,
        approval_id=str(record["approval_id"]),
        registered_at=str(record["registered_at"]),
        git_commit=str(record["git_commit"]),
        git_tree=str(record["git_tree"]),
        sealed_protocol_sha256=str(record["sealed_protocol_sha256"]),
        robustness_release=str(record["robustness_release"]),
        test_period=(FINAL_TEST_START, FINAL_TEST_END),
        robustness_manifest_sha256=str(record["robustness_manifest_sha256"]),
        robustness_lineage_sha256=str(record["robustness_lineage_sha256"]),
    )


def _verify_consumption_ledger(
    sealed: dict[str, object],
    *,
    record: dict[str, object],
    attempt_id: str,
) -> None:
    ledger_root = Path(str(sealed.get("opening_ledger_root", "")))
    if not ledger_root.is_absolute() or ledger_root.is_symlink():
        raise ValueError("historical final-test source ledger root is invalid")
    ledger_path = ledger_root / f"{record['sealed_protocol_sha256']}.json"
    _assert_safe_file(ledger_path, label="historical final-test source ledger")
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("historical final-test source ledger is invalid") from error
    expected = {
        "status": "consumed",
        "attempt_id": attempt_id,
        "approval_id": record["approval_id"],
        "sealed_protocol_sha256": record["sealed_protocol_sha256"],
        "robustness_release": record["robustness_release"],
        "robustness_manifest_sha256": record["robustness_manifest_sha256"],
        "robustness_lineage_sha256": record["robustness_lineage_sha256"],
        "token_sha256": record["token_sha256"],
    }
    if ledger != expected:
        raise ValueError("historical final-test source ledger changed")


def _assert_safe_file(path: Path, *, label: str) -> None:
    if path.is_symlink() or not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"{label} is unsafe")


def _safe_existing_root(path: Path, *, label: str) -> Path:
    if path.is_symlink():
        raise ValueError(f"historical final-test {label} root uses a symlink")
    try:
        return path.resolve(strict=True)
    except FileNotFoundError as error:
        raise ValueError(f"historical final-test {label} root is missing") from error
