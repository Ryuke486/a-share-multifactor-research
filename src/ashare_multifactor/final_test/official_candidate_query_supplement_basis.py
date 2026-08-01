"""Revalidate the immutable basis for candidate query supplementation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from ashare_multifactor.data.security import market_for_symbol
from ashare_multifactor.final_test.action_source_contract import (
    SHARED_ANNOUNCEMENT_CATEGORY,
    FinalActionSourceContract,
)
from ashare_multifactor.final_test.official_candidate_review_admission_storage import (
    ADMISSION_DIRECTORY,
    ADMISSION_MANIFEST_NAME,
    VerifiedCandidateReviewAdmission,
    load_published_candidate_review_admission,
)
from ashare_multifactor.final_test.official_candidate_review_admission import (
    DEFAULT_ADMISSION_DATE_RULE_PATH,
    CandidateReviewTopology,
    recompute_candidate_review_topology,
)
from ashare_multifactor.final_test.official_evidence_workspace import (
    REVIEW_MANIFEST_NAME,
    REVIEW_QUEUE_NAME,
    REVIEW_SESSIONS_DIRECTORY,
    WORKSPACE_DIRECTORY,
    EvidenceWorkspace,
    VerifiedReviewQueue,
    load_verified_review_queue,
)
from ashare_multifactor.final_test.official_query_coverage import (
    OfficialQueryScope,
    canonical_json_bytes,
)
from ashare_multifactor.final_test.official_query_index import (
    official_query_coverage_schema_version,
    validate_official_query_coverage_index,
)
from ashare_multifactor.final_test.official_review_submission import (
    VerifiedCandidateSnapshot,
    load_verified_candidate_snapshot,
)


_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class VerifiedCandidateQueryBasis:
    """Old blocked admission and every upstream object it names."""

    root: Path
    admission: VerifiedCandidateReviewAdmission
    queue: VerifiedReviewQueue
    candidates: VerifiedCandidateSnapshot
    topology: CandidateReviewTopology
    base_collection_path: Path
    base_coverage_path: Path
    base_catalog_path: Path
    org_ids: dict[str, str]
    bindings: dict[str, dict[str, object]]


def load_candidate_query_basis(
    blocked_admission_path: Path,
    *,
    contract: FinalActionSourceContract,
) -> VerifiedCandidateQueryBasis:
    """Traverse one blocked admission back through its complete frozen lineage."""
    admission, root = _load_admission(blocked_admission_path)
    workspace, queue = _load_review_queue(root, admission.manifest)
    candidates = _load_candidates(
        root,
        workspace=workspace,
        admission_manifest=admission.manifest,
    )
    unresolved_ids = tuple(
        sorted(admission.unresolved.get_column("candidate_id").to_list())
    )
    topology = recompute_candidate_review_topology(
        workspace=workspace,
        queue=queue,
        candidates=candidates,
        candidate_ids=unresolved_ids,
    )
    collection_path = root / "official_query_collection.json"
    coverage_path = (
        root / "official_query_coverage" / "official_query_coverage.json"
    )
    catalog_path = root / "official_announcement_catalog" / "catalog.parquet"
    if workspace.catalog_path.absolute() != catalog_path.absolute():
        raise ValueError("candidate query basis catalog path differs")
    _validate_collection_coverage_root(
        collection_path,
        basis_root=root,
        coverage_path=coverage_path,
    )
    org_ids = _validate_coverage(coverage_path, contract=contract)
    bindings = {
        "basis_candidate_snapshot": file_binding(
            candidates.manifest_path
        ),
        "base_query_collection": file_binding(collection_path),
        "base_coverage": file_binding(coverage_path),
        "base_catalog": file_binding(catalog_path),
        "base_routing": file_binding(workspace.routing_path),
        "base_date_rule": file_binding(
            DEFAULT_ADMISSION_DATE_RULE_PATH
        ),
        "blocked_admission": file_binding(admission.manifest_path),
        "blocked_unresolved": file_binding(admission.unresolved_path),
        "base_review_queue": file_binding(queue.manifest_path),
    }
    return VerifiedCandidateQueryBasis(
        root=root,
        admission=admission,
        queue=queue,
        candidates=candidates,
        topology=topology,
        base_collection_path=collection_path,
        base_coverage_path=coverage_path,
        base_catalog_path=catalog_path,
        org_ids=org_ids,
        bindings=bindings,
    )


def _validate_collection_coverage_root(
    path: Path,
    *,
    basis_root: Path,
    coverage_path: Path,
) -> None:
    payload = _canonical_object(
        read_regular(path, label="candidate query base collection"),
        label="candidate query base collection",
    )
    relative = payload.get("coverage_relative_path")
    expected_root = coverage_path.parent
    if (
        payload.get("role") != "official_query_collection"
        or payload.get("schema_version") not in {"1", "2"}
        or payload.get("attempt_id") != basis_root.name
        or not isinstance(relative, str)
        or Path(relative).is_absolute()
        or (basis_root / relative).absolute() != expected_root.absolute()
        or expected_root.name != "official_query_coverage"
    ):
        raise ValueError(
            "candidate query base collection coverage root differs"
        )


def load_successor_candidate_snapshot(
    *,
    destination: Path,
    candidate_manifest_path: Path,
) -> VerifiedCandidateSnapshot:
    """Load the successor snapshot without requiring its future review workspace."""
    expected = (
        destination
        / "corporate_action_candidate_collection"
        / "snapshot"
        / "manifest.json"
    ).absolute()
    absolute = candidate_manifest_path.absolute()
    if absolute != expected:
        raise ValueError(
            "candidate query successor candidate snapshot path is not canonical"
        )
    workspace = EvidenceWorkspace(
        root=destination / WORKSPACE_DIRECTORY,
        catalog_path=destination / "official_announcement_catalog/catalog.parquet",
        routing_path=destination / "official_announcement_routing/routing.parquet",
        review_queue_path=(
            destination
            / WORKSPACE_DIRECTORY
            / REVIEW_SESSIONS_DIRECTORY
            / ("0" * 64)
            / REVIEW_QUEUE_NAME
        ),
        ready=False,
    )
    return load_verified_candidate_snapshot(absolute, workspace=workspace)


def file_binding(path: Path) -> dict[str, object]:
    """Bind one safe regular file by absolute path, bytes, and size."""
    raw = read_regular(path, label="candidate query supplement binding")
    return {
        "path": str(path.absolute()),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }


def read_regular(path: Path, *, label: str) -> bytes:
    """Read one non-symlink regular file through a checked path."""
    absolute = path.absolute()
    if absolute.is_symlink() or not absolute.is_file():
        raise ValueError(f"{label} is missing or unsafe")
    for ancestor in absolute.parents:
        if ancestor.is_symlink():
            raise ValueError(f"{label} uses a symlink")
    return absolute.read_bytes()


def assert_directory(path: Path, *, label: str) -> None:
    """Reject missing directories and symlinks anywhere in their path."""
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label} is missing or unsafe")
    for ancestor in path.parents:
        if ancestor.is_symlink():
            raise ValueError(f"{label} uses a symlink")


def _load_admission(
    path: Path,
) -> tuple[VerifiedCandidateReviewAdmission, Path]:
    absolute = path.absolute()
    admission_root = absolute.parent
    if (
        absolute.name != ADMISSION_MANIFEST_NAME
        or admission_root.parent.name != ADMISSION_DIRECTORY
        or _SHA256.fullmatch(admission_root.name) is None
    ):
        raise ValueError("candidate query blocked admission path is not canonical")
    basis_root = admission_root.parent.parent
    admission = load_published_candidate_review_admission(
        basis_root,
        admission_id=admission_root.name,
    )
    if (
        admission.manifest_path.absolute() != absolute
        or admission.ready
        or admission.manifest.get("status") != "blocked"
        or admission.manifest.get("attempt_id") != basis_root.name
        or admission.manifest.get("final_test_strategy_outputs_read") is not False
    ):
        raise ValueError("candidate query blocked admission is invalid")
    return admission, basis_root


def _load_review_queue(
    basis_root: Path,
    manifest: dict[str, object],
) -> tuple[EvidenceWorkspace, VerifiedReviewQueue]:
    record = manifest.get("review_queue")
    if not isinstance(record, dict):
        raise ValueError("candidate query admission review queue binding is invalid")
    relative = Path(str(record.get("relative_path", "")))
    if (
        relative.is_absolute()
        or len(relative.parts) != 4
        or relative.parts[:2]
        != (WORKSPACE_DIRECTORY, REVIEW_SESSIONS_DIRECTORY)
        or _SHA256.fullmatch(relative.parts[2]) is None
        or relative.parts[3] != REVIEW_MANIFEST_NAME
    ):
        raise ValueError("candidate query admission review queue path is invalid")
    manifest_path = basis_root / relative
    workspace = EvidenceWorkspace(
        root=basis_root / WORKSPACE_DIRECTORY,
        catalog_path=basis_root / "official_announcement_catalog/catalog.parquet",
        routing_path=basis_root / "official_announcement_routing/routing.parquet",
        review_queue_path=manifest_path.parent / REVIEW_QUEUE_NAME,
        ready=False,
    )
    queue = load_verified_review_queue(workspace)
    if (
        queue.manifest_path.absolute() != manifest_path.absolute()
        or queue.manifest_sha256 != record.get("manifest_sha256")
        or queue.session_id != manifest.get("review_session_id")
    ):
        raise ValueError("candidate query admission review queue binding changed")
    return workspace, queue


def _load_candidates(
    basis_root: Path,
    *,
    workspace: EvidenceWorkspace,
    admission_manifest: dict[str, object],
) -> VerifiedCandidateSnapshot:
    record = admission_manifest.get("candidate_snapshot")
    if not isinstance(record, dict):
        raise ValueError("candidate query admission candidate binding is invalid")
    relative = Path(str(record.get("relative_path", "")))
    expected = Path("corporate_action_candidate_collection/snapshot/manifest.json")
    if relative != expected:
        raise ValueError("candidate query admission candidate path is invalid")
    candidates = load_verified_candidate_snapshot(
        basis_root / relative,
        workspace=workspace,
    )
    if candidates.manifest_sha256 != record.get("manifest_sha256"):
        raise ValueError("candidate query admission candidate binding changed")
    return candidates


def _validate_coverage(
    path: Path,
    *,
    contract: FinalActionSourceContract,
) -> dict[str, str]:
    raw = read_regular(path, label="candidate query base coverage")
    payload = _canonical_object(raw, label="candidate query base coverage")
    records = payload.get("packages")
    if (
        payload.get("role") != "official_query_coverage"
        or not isinstance(records, list)
        or not records
    ):
        raise ValueError("candidate query base coverage is invalid")
    grouped: dict[tuple[str, str, str, str], str] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("candidate query base coverage is invalid")
        values = (
            record.get("category"),
            record.get("market"),
            record.get("symbol"),
            record.get("query_category"),
            record.get("org_id"),
        )
        if not all(isinstance(value, str) for value in values):
            raise ValueError("candidate query base coverage scope is invalid")
        category, market, symbol, query_category, org_id = values
        key = (category, market, symbol, query_category)
        existing = grouped.setdefault(key, org_id)
        if existing != org_id:
            raise ValueError("candidate query base coverage orgId differs")
    scopes = tuple(
        OfficialQueryScope(
            symbol=symbol,
            market=market,
            category=category,
            query_category=query_category,
            start=contract.start,
            end=contract.end,
            org_id=org_id,
        )
        for (category, market, symbol, query_category), org_id in sorted(
            grouped.items()
        )
    )
    verified = validate_official_query_coverage_index(
        path,
        expected_scopes=scopes,
    )
    if (
        official_query_coverage_schema_version(path) not in {"1", "2"}
        or not verified.packages
    ):
        raise ValueError("candidate query base coverage is invalid")
    return _validated_org_ids(verified.packages, contract=contract)


def _validated_org_ids(
    packages,
    *,
    contract: FinalActionSourceContract,
) -> dict[str, str]:
    org_ids: dict[str, str] = {}
    expected_query_category = dict(contract.official_query_categories)[
        "corporate_actions"
    ]
    for package in packages:
        scope = package.scope
        if (
            scope.category != SHARED_ANNOUNCEMENT_CATEGORY
            or scope.query_category != expected_query_category
            or scope.market != market_for_symbol(scope.symbol)
            or not isinstance(scope.org_id, str)
        ):
            raise ValueError("candidate query base coverage scope differs")
        existing = org_ids.setdefault(scope.symbol, scope.org_id)
        if existing != scope.org_id:
            raise ValueError("candidate query base coverage orgId differs")
    return org_ids


def _canonical_object(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{label} is invalid") from error
    if not isinstance(value, dict) or raw != canonical_json_bytes(value):
        raise ValueError(f"{label} is invalid")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value
