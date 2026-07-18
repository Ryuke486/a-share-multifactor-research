"""Read-only recovery and evidence preflight for a prepared final-test attempt."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
from pathlib import Path

import polars as pl

from ashare_multifactor.audit.publication import resolve_current
from ashare_multifactor.audit.records import sha256_file
from ashare_multifactor.final_test.execution_sources import (
    validate_final_execution_coverages,
)
from ashare_multifactor.final_test.gate import (
    FINAL_TEST_END,
    FINAL_TEST_START,
    FinalTestAuthorization,
    FrozenStage8Identity,
    _assert_stage8_identity_unchanged,
    _git,
    _load_execution_token,
    _validate_execution_token_bytes,
    _valid_sha256,
    _verify_frozen_contract,
    _verify_sealed_payload,
)
from ashare_multifactor.final_test.preparation import (
    FinalTestPreparation,
    verify_preparation,
)
from ashare_multifactor.final_test.final_root_binding import FinalRootBinding
from ashare_multifactor.final_test.recovery_secure_fs import (
    opened_directory_at,
    read_bytes_at,
)
from ashare_multifactor.final_test.registry import (
    resolve_attempt_state_at,
    resolve_attempt_state_readonly,
    validate_publication_id,
)


@dataclass(frozen=True)
class ResumePreflight:
    authorization: FinalTestAuthorization
    preparation: FinalTestPreparation
    security_event_coverage_sha256: str
    corporate_action_coverage_sha256: str


def load_registered_authorization(
    *,
    code_root: Path,
    data_root: Path,
    attempt_id: str,
    approval_key: bytes,
    root_binding: FinalRootBinding | None = None,
) -> FinalTestAuthorization:
    """Reconstruct one consumed authorization without consuming its token again."""
    validate_publication_id(attempt_id)
    code_root = _resolve_safe_root(code_root, "code")
    data_root = _resolve_safe_root(data_root, "data")
    final_root = data_root / "processed/final_test"
    registry_root = final_root / "attempts"
    _verify_data_paths(data_root, final_root, registry_root)

    record_path = registry_root / f"{attempt_id}.json"
    if record_path.is_symlink():
        raise ValueError("final-test attempt registration uses a symlink")
    record = (
        resolve_attempt_state_readonly(registry_root, attempt_id)
        if root_binding is None
        else resolve_attempt_state_at(root_binding.attempts_fd, attempt_id)
    )
    if root_binding is not None:
        root_binding.assert_bound()
    _verify_registration(record, attempt_id)

    current_commit = _git(code_root, "rev-parse", "HEAD").strip()
    current_tree = _git(code_root, "rev-parse", "HEAD^{tree}").strip()
    if record["git_commit"] != current_commit or record["git_tree"] != current_tree:
        raise ValueError("registered final-test Git identity changed")

    try:
        robustness = resolve_current(data_root / "processed/robustness")
    except ValueError as error:
        raise ValueError("registered Stage-8 identity changed") from error
    sealed_path = robustness.artifacts / "sealed_test_protocol.json"
    if sealed_path.is_symlink():
        raise ValueError("Stage-8 sealed protocol uses a symlink")
    try:
        sealed = json.loads(sealed_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError("invalid Stage-8 sealed protocol") from error
    if not isinstance(sealed, dict):
        raise ValueError("invalid Stage-8 sealed protocol")
    seal_sha256 = _verify_sealed_payload(sealed)
    frozen = FrozenStage8Identity(
        run_id=robustness.run_id,
        manifest_sha256=robustness.manifest_sha256,
        lineage_sha256=sha256_file(robustness.lineage),
        seal_sha256=seal_sha256,
    )
    if (
        record["robustness_release"] != frozen.run_id
        or record["robustness_manifest_sha256"] != frozen.manifest_sha256
        or record["robustness_lineage_sha256"] != frozen.lineage_sha256
        or record["sealed_protocol_sha256"] != frozen.seal_sha256
    ):
        raise ValueError("registered Stage-8 identity changed")
    _verify_frozen_contract(
        code_root,
        sealed,
        data_root / "processed/validation_evaluation",
        robustness.lineage,
    )

    token_path = registry_root / f"{attempt_id}.token"
    if root_binding is None:
        if token_path.is_symlink() or not token_path.is_file():
            raise ValueError("final-test token snapshot is missing or uses a symlink")
        token_bytes = token_path.read_bytes()
    else:
        token_bytes = read_bytes_at(
            root_binding.attempts_fd,
            f"{attempt_id}.token",
            label="final-test token snapshot",
        )
    token_sha256 = hashlib.sha256(token_bytes).hexdigest()
    if token_sha256 != record["token_sha256"]:
        raise ValueError("final-test token snapshot hash changed")
    if root_binding is None:
        token, verified_bytes, verified_sha256 = _load_execution_token(
            token_path,
            sealed_protocol_sha256=frozen.seal_sha256,
            approval_key=approval_key,
            current_commit=current_commit,
            current_tree=current_tree,
            robustness_release=frozen.run_id,
            robustness_manifest_sha256=frozen.manifest_sha256,
            robustness_lineage_sha256=frozen.lineage_sha256,
        )
    else:
        token, verified_bytes, verified_sha256 = _validate_execution_token_bytes(
            token_bytes,
            sealed_protocol_sha256=frozen.seal_sha256,
            approval_key=approval_key,
            current_commit=current_commit,
            current_tree=current_tree,
            robustness_release=frozen.run_id,
            robustness_manifest_sha256=frozen.manifest_sha256,
            robustness_lineage_sha256=frozen.lineage_sha256,
        )
    if (
        verified_bytes != token_bytes
        or verified_sha256 != token_sha256
        or token.get("approval_id") != record["approval_id"]
    ):
        raise ValueError("final-test token snapshot differs from registration")
    _verify_consumption_ledger(
        sealed,
        attempt_id=attempt_id,
        token=token,
        token_sha256=token_sha256,
        frozen=frozen,
    )
    _assert_stage8_identity_unchanged(robustness, frozen)
    return _authorization_from_record(record)


def preflight_resume(
    *,
    code_root: Path,
    data_root: Path,
    attempt_id: str,
    approval_key: bytes,
    security_event_coverage_path: Path,
    corporate_action_coverage_root: Path,
    expected_state: str = "awaiting_official_evidence",
    root_binding: FinalRootBinding | None = None,
) -> ResumePreflight:
    """Verify a prepared attempt and exact official evidence without state writes."""
    authorization = load_registered_authorization(
        code_root=code_root,
        data_root=data_root,
        attempt_id=attempt_id,
        approval_key=approval_key,
        root_binding=root_binding,
    )
    final_root = data_root.resolve() / "processed/final_test"
    preparation = verify_preparation(
        final_root,
        attempt_id=attempt_id,
        authorization=authorization,
        expected_state=expected_state,
        root_binding=root_binding,
    )
    symbols = (
        load_bound_symbol_scope(preparation)
        if root_binding is None
        else load_bound_symbol_scope_at(root_binding.final_fd, preparation)
    )
    security, corporate = validate_final_execution_coverages(
        symbols=symbols,
        security_event_coverage_path=security_event_coverage_path,
        corporate_action_coverage_root=corporate_action_coverage_root,
    )
    result = ResumePreflight(
        authorization=authorization,
        preparation=preparation,
        security_event_coverage_sha256=_coverage_manifest_sha256(
            security, "security-event coverage"
        ),
        corporate_action_coverage_sha256=_coverage_manifest_sha256(
            corporate, "corporate-action coverage"
        ),
    )
    state = (
        resolve_attempt_state_readonly(final_root / "attempts", attempt_id)
        if root_binding is None
        else resolve_attempt_state_at(root_binding.attempts_fd, attempt_id)
    )
    if state["state"] != expected_state:
        raise ValueError("final-test attempt state changed during resume preflight")
    if expected_state == "executing" and state["identities"] != {
        "prepare_manifest_sha256": result.preparation.manifest_sha256,
        "security_event_coverage_sha256": result.security_event_coverage_sha256,
        "corporate_action_coverage_sha256": result.corporate_action_coverage_sha256,
    }:
        raise ValueError("executing state identity differs from resume preflight")
    return result




def verify_resume_coverages(
    preflight: ResumePreflight,
    *,
    security_event_coverage_path: Path,
    corporate_action_coverage_root: Path,
) -> None:
    """Revalidate the exact evidence bytes after execution has been claimed."""
    symbols = load_bound_symbol_scope(preflight.preparation)
    security, corporate = validate_final_execution_coverages(
        symbols=symbols,
        security_event_coverage_path=security_event_coverage_path,
        corporate_action_coverage_root=corporate_action_coverage_root,
    )
    current = (
        _coverage_manifest_sha256(security, "security-event coverage"),
        _coverage_manifest_sha256(corporate, "corporate-action coverage"),
    )
    expected = (
        preflight.security_event_coverage_sha256,
        preflight.corporate_action_coverage_sha256,
    )
    if current != expected:
        raise ValueError("official coverage identity changed after execution claim")


def _verify_registration(record: dict[str, object], attempt_id: str) -> None:
    required_text = (
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
        or any(
            not isinstance(record.get(field), str) or not record[field] for field in required_text
        )
    ):
        raise ValueError("invalid final-test attempt registration")
    sha_fields = (
        "sealed_protocol_sha256",
        "robustness_manifest_sha256",
        "robustness_lineage_sha256",
        "token_sha256",
    )
    if any(not _valid_sha256(str(record[field])) for field in sha_fields):
        raise ValueError("invalid final-test attempt registration")


def _verify_consumption_ledger(
    sealed: dict[str, object],
    *,
    attempt_id: str,
    token: dict[str, object],
    token_sha256: str,
    frozen: FrozenStage8Identity,
) -> None:
    ledger_root = Path(str(sealed.get("opening_ledger_root", "")))
    if not ledger_root.is_absolute() or ledger_root.is_symlink():
        raise ValueError("sealed protocol opening ledger root is invalid")
    ledger_path = ledger_root / f"{frozen.seal_sha256}.json"
    if ledger_path.is_symlink():
        raise ValueError("final-test opening consumption ledger uses a symlink")
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError("invalid final-test opening consumption ledger") from error
    expected = {
        "status": "consumed",
        "attempt_id": attempt_id,
        "approval_id": token["approval_id"],
        "sealed_protocol_sha256": frozen.seal_sha256,
        "robustness_release": frozen.run_id,
        "robustness_manifest_sha256": frozen.manifest_sha256,
        "robustness_lineage_sha256": frozen.lineage_sha256,
        "token_sha256": token_sha256,
    }
    if ledger != expected:
        raise ValueError("final-test opening consumption ledger changed")


def _authorization_from_record(
    record: dict[str, object],
) -> FinalTestAuthorization:
    return FinalTestAuthorization(
        attempt_id=str(record["attempt_id"]),
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


def _resolve_safe_root(root: Path, label: str) -> Path:
    if root.is_symlink():
        raise ValueError(f"final-test {label} root uses a symlink")
    try:
        return root.resolve(strict=True)
    except FileNotFoundError as error:
        raise ValueError(f"final-test {label} root is missing") from error


def _verify_data_paths(data_root: Path, final_root: Path, registry_root: Path) -> None:
    processed = data_root / "processed"
    for path in (
        processed,
        final_root,
        registry_root,
        data_root / "processed/robustness",
        data_root / "processed/validation_evaluation",
    ):
        if path.is_symlink():
            raise ValueError("final-test data path uses a symlink")
    if final_root.resolve() != processed.resolve() / "final_test":
        raise ValueError("final-test data path escapes processed root")


def load_bound_symbol_scope(preparation: FinalTestPreparation) -> list[str]:
    """Load the sorted symbols already bound by the verified preparation."""
    try:
        frame = pl.read_parquet(preparation.symbol_scope_path)
    except pl.exceptions.PolarsError as error:
        raise ValueError("invalid final-test preparation symbol scope") from error
    return _validated_symbol_scope(frame, preparation)


def load_bound_symbol_scope_at(
    final_fd: int,
    preparation: FinalTestPreparation,
) -> list[str]:
    """Load the preparation symbol scope below one held final-root FD."""
    with opened_directory_at(
        final_fd,
        "preparations",
        label="final-test preparations",
    ) as preparations_fd:
        with opened_directory_at(
            preparations_fd,
            preparation.attempt_id,
            label="final-test preparation",
        ) as preparation_fd:
            payload = read_bytes_at(
                preparation_fd,
                "symbol_scope.parquet",
                label="final-test preparation symbol scope",
            )
    try:
        frame = pl.read_parquet(BytesIO(payload))
    except pl.exceptions.PolarsError as error:
        raise ValueError("invalid final-test preparation symbol scope") from error
    return _validated_symbol_scope(frame, preparation)


def _validated_symbol_scope(
    frame: pl.DataFrame,
    preparation: FinalTestPreparation,
) -> list[str]:
    if frame.columns != ["symbol"] or frame.get_column("symbol").null_count():
        raise ValueError("invalid final-test preparation symbol scope")
    symbols = frame.get_column("symbol").cast(pl.String).to_list()
    symbols_sha256 = hashlib.sha256(("\n".join(symbols) + "\n").encode()).hexdigest()
    if (
        len(symbols) != preparation.symbol_count
        or symbols != sorted(set(symbols))
        or symbols_sha256 != preparation.symbols_sha256
    ):
        raise ValueError("final-test preparation symbol scope identity changed")
    return symbols


def _coverage_manifest_sha256(coverage: dict[str, object], label: str) -> str:
    value = coverage.get("coverage_manifest_sha256")
    if not isinstance(value, str) or not _valid_sha256(value):
        raise ValueError(f"verified {label} identity is invalid")
    return value
