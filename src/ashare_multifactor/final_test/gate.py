from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import hmac
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import subprocess
import uuid

from ashare_multifactor.audit.publication import resolve_current
from ashare_multifactor.audit.records import sha256_file, verify_file_record
from ashare_multifactor.config import load_config
from ashare_multifactor.final_test.registry import (
    append_attempt_outcome,
    assert_no_authoritative_success,
    register_attempt,
    save_token_snapshot,
)
from ashare_multifactor.robustness.protocol import load_robustness_protocol
from ashare_multifactor.robustness.successor_seal import (
    verify_action_coverage_audit,
)
from ashare_multifactor.robustness.test_protocol import verify_test_opening_token


FINAL_TEST_START = date(2022, 1, 1)
FINAL_TEST_END = date(2025, 12, 31)


@dataclass(frozen=True)
class FinalTestAuthorization:
    attempt_id: str
    approval_id: str
    registered_at: str
    git_commit: str
    git_tree: str
    sealed_protocol_sha256: str
    robustness_release: str
    test_period: tuple[date, date]
    robustness_manifest_sha256: str = ""
    robustness_lineage_sha256: str = ""


@dataclass(frozen=True)
class FrozenStage8Identity:
    run_id: str
    manifest_sha256: str
    lineage_sha256: str
    seal_sha256: str


def authorize_final_test(
    *,
    code_root: Path,
    robustness_root: Path,
    validation_root: Path,
    opening_token_path: Path,
    approval_key: bytes,
    registry_root: Path,
    requested_start: date,
    requested_end: date,
    attempt_id: str | None = None,
) -> FinalTestAuthorization:
    """Verify the frozen Stage-8 contract and register before any test-data read."""
    if (requested_start, requested_end) != (FINAL_TEST_START, FINAL_TEST_END):
        raise ValueError("requested dates differ from the sealed final-test period")

    code_root = code_root.resolve()
    robustness = resolve_current(robustness_root)
    sealed = json.loads(
        (robustness.artifacts / "sealed_test_protocol.json").read_text(
            encoding="utf-8"
        )
    )
    seal = _verify_sealed_payload(sealed)
    frozen = FrozenStage8Identity(
        run_id=robustness.run_id,
        manifest_sha256=robustness.manifest_sha256,
        lineage_sha256=sha256_file(robustness.lineage),
        seal_sha256=seal,
    )
    _verify_frozen_contract(code_root, sealed, validation_root, robustness.lineage)
    assert_no_authoritative_success(registry_root)

    current_commit = _git(code_root, "rev-parse", "HEAD").strip()
    current_tree = _git(code_root, "rev-parse", "HEAD^{tree}").strip()
    token, token_bytes, token_sha256 = _load_execution_token(
        opening_token_path,
        sealed_protocol_sha256=seal,
        approval_key=approval_key,
        current_commit=current_commit,
        current_tree=current_tree,
        robustness_release=frozen.run_id,
        robustness_manifest_sha256=frozen.manifest_sha256,
        robustness_lineage_sha256=frozen.lineage_sha256,
    )
    actual_attempt_id = attempt_id or uuid.uuid4().hex
    record = register_attempt(
        registry_root,
        attempt_id=actual_attempt_id,
        git_commit=current_commit,
        git_tree=current_tree,
        token_sha256=token_sha256,
        sealed_protocol_sha256=seal,
        robustness_release=frozen.run_id,
        approval_id=str(token["approval_id"]),
        robustness_manifest_sha256=frozen.manifest_sha256,
        robustness_lineage_sha256=frozen.lineage_sha256,
    )
    try:
        token_snapshot = save_token_snapshot(
            registry_root,
            attempt_id=actual_attempt_id,
            token_bytes=token_bytes,
            expected_sha256=token_sha256,
        )
        verify_test_opening_token(
            token_snapshot,
            sealed,
            approval_key=approval_key,
        )
        _assert_stage8_identity_unchanged(robustness, frozen)
    except BaseException as error:
        append_attempt_outcome(
            registry_root,
            attempt_id=actual_attempt_id,
            status="failed",
            authoritative=False,
            reason=f"authorization failed after registration: {error}",
        )
        raise
    return FinalTestAuthorization(
        attempt_id=actual_attempt_id,
        approval_id=str(token["approval_id"]),
        registered_at=str(record["registered_at"]),
        git_commit=current_commit,
        git_tree=current_tree,
        sealed_protocol_sha256=seal,
        robustness_release=frozen.run_id,
        test_period=(requested_start, requested_end),
        robustness_manifest_sha256=frozen.manifest_sha256,
        robustness_lineage_sha256=frozen.lineage_sha256,
    )


def _assert_stage8_identity_unchanged(
    robustness: object, frozen: FrozenStage8Identity
) -> None:
    manifest = Path(getattr(robustness, "manifest"))
    lineage = Path(getattr(robustness, "lineage"))
    if (
        getattr(robustness, "run_id") != frozen.run_id
        or sha256_file(manifest) != frozen.manifest_sha256
        or sha256_file(lineage) != frozen.lineage_sha256
    ):
        raise ValueError("Stage-8 identity changed during final-test authorization")


def _verify_sealed_payload(sealed: dict[str, object]) -> str:
    payload = dict(sealed)
    recorded = str(payload.pop("sealed_protocol_sha256", ""))
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    if not recorded or hashlib.sha256(canonical).hexdigest() != recorded:
        raise ValueError("sealed protocol hash does not match its payload")
    if (
        sealed.get("protocol_version") != 2
        or not _valid_sha256(str(sealed.get("action_source_contract_sha256", "")))
        or not _valid_sha256(str(sealed.get("action_coverage_audit_sha256", "")))
        or not isinstance(sealed.get("predecessor"), dict)
        or sealed.get("supported_markets") != ["sh", "sz"]
    ):
        raise ValueError(
            "final-test authorization requires Stage-8 successor protocol v2"
        )
    if (
        sealed.get("status") != "sealed"
        or sealed.get("opening_token_status") != "closed"
        or sealed.get("one_authoritative_run") is not True
        or sealed.get("failed_runs_must_be_retained") is not True
        or sealed.get("opening_token_required") is not True
    ):
        raise ValueError("sealed final-test policy is incomplete")
    gate = sealed.get("robustness_gate")
    if not isinstance(gate, dict) or gate.get("sealed_test_protocol_allowed") is not True:
        raise ValueError("robustness gate does not authorize the sealed protocol")
    if sealed.get("test_period") != [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()]:
        raise ValueError("sealed final-test period changed")
    return recorded


def _verify_frozen_contract(
    code_root: Path,
    sealed: dict[str, object],
    validation_root: Path,
    robustness_lineage_path: Path,
) -> None:
    protocol = load_robustness_protocol(code_root / "configs/robustness_protocol.yaml")
    research_config = load_config(code_root / "configs/research_protocol.yaml")
    if list(research_config.supported_markets) != sealed.get("supported_markets"):
        raise ValueError("research supported markets differ from sealed protocol")
    if protocol.protocol_sha256 != sealed.get("robustness_protocol_sha256"):
        raise ValueError("robustness protocol hash changed; refreezing is forbidden")
    if list(protocol.final_test_metrics) != sealed.get("metrics"):
        raise ValueError("frozen metrics hash changed; refreezing is forbidden")
    if protocol.main_candidate != sealed.get("main_candidate"):
        raise ValueError("frozen main candidate changed")
    if sealed.get("cost_model") != "full_audited_cost_model":
        raise ValueError("frozen cost model changed")
    if sha256_file(code_root / "configs/market_rules.yaml") != sealed.get(
        "market_rules_sha256"
    ):
        raise ValueError("market rules hash changed; refreezing is forbidden")
    if sha256_file(
        code_root / "configs/final_execution_sources.yaml"
    ) != sealed.get("action_source_contract_sha256"):
        raise ValueError("final execution source contract changed")
    if sha256_file(
        code_root / "docs/templates/stage9-final-test-report-template.md"
    ) != sealed.get("report_template_sha256"):
        raise ValueError("report template hash changed; refreezing is forbidden")

    frozen_code = sealed.get("code")
    if not isinstance(frozen_code, dict) or frozen_code.get("dirty") is not False:
        raise ValueError("sealed code identity is invalid")
    if frozen_code.get("python") != platform.python_version():
        raise ValueError("sealed runtime Python version changed")
    dependencies = frozen_code.get("dependencies")
    if not isinstance(dependencies, dict):
        raise ValueError("sealed runtime dependencies are missing")
    for package, expected in dependencies.items():
        if version(str(package)) != expected:
            raise ValueError(f"sealed runtime dependency changed: {package}")
    sources = frozen_code.get("sources")
    if not isinstance(sources, list):
        raise ValueError("sealed code source records are missing")
    for source in sources:
        try:
            verify_file_record(source, root=code_root)
        except (FileNotFoundError, TypeError, ValueError) as exc:
            raise ValueError("frozen code hash changed; refreezing is forbidden") from exc
    if _git(code_root, "status", "--porcelain=v1", "--untracked-files=all").strip():
        raise ValueError("final-test authorization requires a clean Git identity")
    sealed_commit = str(frozen_code.get("commit", ""))
    if not sealed_commit or subprocess.run(
        ("git", "merge-base", "--is-ancestor", sealed_commit, "HEAD"),
        cwd=code_root,
        capture_output=True,
        env=_git_readonly_env(),
    ).returncode:
        raise ValueError("current code does not descend from the sealed commit")

    validation = resolve_current(validation_root)
    expected_validation = sealed.get("upstream_validation")
    if not isinstance(expected_validation, dict) or (
        expected_validation.get("run_id") != validation.run_id
        or expected_validation.get("manifest_sha256") != validation.manifest_sha256
    ):
        raise ValueError("upstream validation release changed")

    lineage = json.loads(robustness_lineage_path.read_text(encoding="utf-8"))
    audit_root = robustness_lineage_path.parent / "artifacts/action_coverage_audit"
    if verify_action_coverage_audit(audit_root) != sealed.get(
        "action_coverage_audit_sha256"
    ):
        raise ValueError("action coverage audit differs from successor seal")
    execution_protocol = lineage.get("execution_protocol")
    if (
        lineage.get("predecessor") != sealed.get("predecessor")
        or not isinstance(execution_protocol, dict)
        or execution_protocol.get("action_source_contract_sha256")
        != sealed.get("action_source_contract_sha256")
        or execution_protocol.get("action_coverage_audit_sha256")
        != sealed.get("action_coverage_audit_sha256")
        or execution_protocol.get("supported_markets")
        != sealed.get("supported_markets")
        or execution_protocol.get("status")
        != "ready_for_new_final_test_authorization"
    ):
        raise ValueError("successor lineage differs from sealed execution protocol")
    inputs = lineage.get("inputs")
    research_record = inputs.get("research_protocol") if isinstance(inputs, dict) else None
    research_path = code_root / "configs/research_protocol.yaml"
    if not isinstance(research_record, dict) or (
        research_record.get("sha256") != sha256_file(research_path)
        or research_record.get("size") != research_path.stat().st_size
    ):
        raise ValueError("research protocol differs from robustness lineage")


def _load_execution_token(
    token_path: Path,
    *,
    sealed_protocol_sha256: str,
    approval_key: bytes,
    current_commit: str,
    current_tree: str,
    robustness_release: str,
    robustness_manifest_sha256: str,
    robustness_lineage_sha256: str,
) -> tuple[dict[str, object], bytes, str]:
    try:
        token_bytes = token_path.read_bytes()
        token = json.loads(token_bytes)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise ValueError("invalid final-test opening token") from exc
    if not isinstance(token, dict) or len(approval_key) < 16:
        raise ValueError("invalid final-test opening token")
    approval_id = str(token.get("approval_id", ""))
    identity = (
        f"{sealed_protocol_sha256}|{robustness_release}|"
        f"{robustness_manifest_sha256}|{robustness_lineage_sha256}"
    )
    expected_opening = hmac.new(
        approval_key,
        f"{identity}|{approval_id}|stage9-one-shot".encode(),
        hashlib.sha256,
    ).hexdigest()
    opening_valid = (
        token.get("status") == "approved"
        and token.get("sealed_protocol_sha256") == sealed_protocol_sha256
        and token.get("robustness_release") == robustness_release
        and token.get("robustness_manifest_sha256") == robustness_manifest_sha256
        and token.get("robustness_lineage_sha256") == robustness_lineage_sha256
        and bool(approval_id)
        and hmac.compare_digest(str(token.get("signature", "")), expected_opening)
    )
    if not opening_valid:
        raise ValueError("invalid final-test opening token")
    if (
        token.get("approved_git_commit") != current_commit
        or token.get("approved_git_tree") != current_tree
    ):
        raise ValueError("user approval does not match execution Git commit/tree")
    expected_execution = hmac.new(
        approval_key,
        (
            f"{identity}|{approval_id}|{current_commit}|{current_tree}"
            "|stage9-one-shot-execution"
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(
        str(token.get("execution_signature", "")), expected_execution
    ):
        raise ValueError("invalid final-test execution signature")
    return token, token_bytes, hashlib.sha256(token_bytes).hexdigest()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=root,
        check=True,
        capture_output=True,
        env=_git_readonly_env(),
        text=True,
    ).stdout


def _git_readonly_env() -> dict[str, str]:
    return {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )
