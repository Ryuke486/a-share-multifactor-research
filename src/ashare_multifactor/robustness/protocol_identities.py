"""Independent identities for research results, execution, and evidence workflow."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

from ashare_multifactor.audit.records import sha256_file


_FINAL_EXECUTION_PATHS = (
    "configs/final_execution_sources.yaml",
    "configs/market_rules.yaml",
    "src/ashare_multifactor/final_test/action_source_contract.py",
    "src/ashare_multifactor/final_test/backtest.py",
    "src/ashare_multifactor/final_test/coverage_snapshot.py",
    "src/ashare_multifactor/final_test/execution_contracts.py",
    "src/ashare_multifactor/final_test/execution_identity.py",
    "src/ashare_multifactor/final_test/pipeline.py",
    "src/ashare_multifactor/final_test/resume.py",
)
_EVIDENCE_WORKFLOW_EXTRA_PATHS = (
    "configs/evidence/stage9_candidate_review_admission_date_rule.json",
    "configs/evidence/stage9_known_routing_cases.csv",
    "pyproject.toml",
    "src/ashare_multifactor/audit/identity.py",
    "src/ashare_multifactor/cli/final_test.py",
    "src/ashare_multifactor/cli/final_test_review.py",
    "src/ashare_multifactor/cli/robustness.py",
    "src/ashare_multifactor/final_test/baostock_dividend_client.py",
    "src/ashare_multifactor/final_test/corporate_action_candidate_contract.py",
    "src/ashare_multifactor/final_test/corporate_action_candidate_packages.py",
    "src/ashare_multifactor/final_test/corporate_action_candidates.py",
    "src/ashare_multifactor/final_test/corporate_action_coverage.py",
    "src/ashare_multifactor/final_test/data_reuse.py",
    "src/ashare_multifactor/final_test/data_reuse_contract.py",
    "src/ashare_multifactor/final_test/execution_sources.py",
    "src/ashare_multifactor/final_test/gate.py",
    "src/ashare_multifactor/final_test/historical_attempt.py",
    "src/ashare_multifactor/final_test/preparation.py",
    "src/ashare_multifactor/final_test/routing_audit.py",
    "src/ashare_multifactor/final_test/shared_evidence.py",
    "src/ashare_multifactor/final_test/signals.py",
    "src/ashare_multifactor/robustness/change_impact.py",
    "src/ashare_multifactor/robustness/evidence_workflow_readiness.py",
    "src/ashare_multifactor/robustness/evidence_workflow_rehearsal.py",
    "src/ashare_multifactor/robustness/evidence_workflow_successor_release.py",
    "src/ashare_multifactor/robustness/protocol_identities.py",
    "src/ashare_multifactor/robustness/release_context.py",
    "src/ashare_multifactor/robustness/successor_seal.py",
    "src/ashare_multifactor/robustness/test_protocol.py",
    "tests/test_audit_publication.py",
    "tests/test_evidence_workflow_successor.py",
    "tests/test_final_test_corporate_action_candidates.py",
    "tests/test_final_test_cli.py",
    "tests/test_final_test_data_reuse.py",
    "tests/test_final_test_historical_evidence_import.py",
    "tests/test_final_test_official_announcement_routing.py",
    "tests/test_final_test_official_candidate_review_admission.py",
    "tests/test_final_test_official_coverage_publisher.py",
    "tests/test_final_test_official_document_validation.py",
    "tests/test_final_test_official_evidence_import.py",
    "tests/test_final_test_official_evidence_workspace.py",
    "tests/test_final_test_official_review_batches.py",
    "tests/test_final_test_official_review_submission.py",
    "tests/test_final_test_pipeline.py",
    "tests/test_final_test_preparation.py",
    "tests/test_final_test_resume.py",
    "tests/test_final_test_routing_audit.py",
    "tests/test_final_test_signals.py",
    "tests/test_final_test_two_phase.py",
)


def build_protocol_identities(
    code_root: Path,
    *,
    research_records: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    """Build the three identities that determine the smallest required rerun."""
    code_root = code_root.resolve()
    return {
        "research": identity_payload(
            "research_result_identity",
            research_records,
        ),
        "final_execution": build_final_execution_identity(code_root),
        "evidence_workflow": build_evidence_workflow_identity(code_root),
    }


def build_final_execution_identity(code_root: Path) -> dict[str, object]:
    """Hash only the frozen execution domain."""
    root = code_root.resolve()
    return identity_payload(
        "final_execution_identity",
        _file_records(root, _FINAL_EXECUTION_PATHS),
    )


def build_final_execution_identity_at_revision(
    code_root: Path,
    *,
    commit: str,
    tree: str,
) -> dict[str, object]:
    """Rebuild the execution identity from one already sealed Git revision."""
    if not _valid_git_oid(commit) or not _valid_git_oid(tree):
        raise ValueError("sealed predecessor Git identity is invalid")
    root = code_root.resolve()
    actual_tree = _git_bytes(
        root,
        "rev-parse",
        f"{commit}^{{tree}}",
    ).decode().strip()
    if actual_tree != tree:
        raise ValueError("sealed predecessor Git tree differs from commit")
    records: dict[str, object] = {}
    for relative in _FINAL_EXECUTION_PATHS:
        inventory = _git_bytes(root, "ls-tree", commit, "--", relative).decode()
        if not inventory.startswith(("100644 blob ", "100755 blob ")):
            raise ValueError(
                f"sealed predecessor execution source is missing: {relative}"
            )
        payload = _git_bytes(root, "show", f"{commit}:{relative}")
        records[relative] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }
    return identity_payload("final_execution_identity", records)


def build_evidence_workflow_identity(code_root: Path) -> dict[str, object]:
    """Hash every source that can admit, review, import, or seal evidence."""
    root = code_root.resolve()
    evidence_paths = tuple(
        sorted(
            {
                *_EVIDENCE_WORKFLOW_EXTRA_PATHS,
                *(
                    path.relative_to(root).as_posix()
                    for path in (
                        root / "src/ashare_multifactor/final_test"
                    ).glob("official_*.py")
                ),
            }
        )
    )
    return identity_payload(
        "evidence_workflow_identity",
        _file_records(root, evidence_paths),
    )


def validate_domain_identity(identity: object, *, role: str) -> dict[str, object]:
    """Validate one standalone domain identity."""
    if (
        not isinstance(identity, dict)
        or identity.get("role") != role
        or not isinstance(identity.get("records"), dict)
    ):
        raise ValueError("protocol domain identity is invalid")
    expected = identity_payload(role, identity["records"])
    if identity != expected:
        raise ValueError("protocol domain identity hash differs from records")
    return identity


def identity_payload(
    role: str,
    records: Mapping[str, object],
) -> dict[str, object]:
    """Return one canonical mapping identity without conflating its domain."""
    if not isinstance(role, str) or not role or not isinstance(records, Mapping):
        raise ValueError("protocol identity input is invalid")
    normalized = {
        str(key): _jsonable(value)
        for key, value in sorted(records.items(), key=lambda item: str(item[0]))
    }
    if not normalized:
        raise ValueError("protocol identity records are empty")
    canonical = _canonical_json_bytes({"role": role, "records": normalized})
    return {
        "role": role,
        "records": normalized,
        "sha256": hashlib.sha256(canonical).hexdigest(),
    }


def validate_protocol_identities(
    identities: object,
) -> dict[str, dict[str, object]]:
    """Reject missing, merged, or self-inconsistent protocol identities."""
    if not isinstance(identities, dict) or set(identities) != {
        "research",
        "final_execution",
        "evidence_workflow",
    }:
        raise ValueError("protocol identity domains are incomplete")
    expected_roles = {
        "research": "research_result_identity",
        "final_execution": "final_execution_identity",
        "evidence_workflow": "evidence_workflow_identity",
    }
    result: dict[str, dict[str, object]] = {}
    for domain, role in expected_roles.items():
        identity = identities.get(domain)
        try:
            result[domain] = validate_domain_identity(identity, role=role)
        except ValueError as error:
            raise ValueError("protocol identity is invalid") from error
    return result


def _file_records(
    code_root: Path,
    paths: tuple[str, ...],
) -> dict[str, object]:
    records: dict[str, object] = {}
    for relative in paths:
        path = code_root / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"protocol identity source is missing: {relative}")
        records[relative] = {
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
    return records


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("protocol identity record is not JSON-safe")


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


def _valid_git_oid(value: str) -> bool:
    return len(value) == 40 and all(
        character in "0123456789abcdef" for character in value
    )


def _git_bytes(code_root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ("git", *args),
        cwd=code_root,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ValueError("sealed predecessor Git identity cannot be resolved")
    return result.stdout
