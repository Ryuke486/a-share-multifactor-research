"""Canonical authorization for candidate-scoped announcement requests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
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
from ashare_multifactor.final_test.official_candidate_query_supplement_basis import (
    assert_directory,
    file_binding,
    load_candidate_query_basis,
    load_successor_candidate_snapshot,
    read_regular,
)
from ashare_multifactor.final_test.official_candidate_query_topology import (
    load_verified_candidate_query_topology,
)
from ashare_multifactor.final_test.official_query_coverage import (
    OfficialQueryScope,
    canonical_json_bytes,
)
from ashare_multifactor.final_test.official_query_packages import (
    official_query_request_sha256,
)
from ashare_multifactor.final_test.official_review_contract import (
    CANDIDATE_FIELDS,
)


_AUTHORIZATION_SCHEMA = "stage9_candidate_query_network_authorization/v1"
_AUTHORIZATION_ROLE = "stage9_candidate_query_network_authorization"
CANDIDATE_QUERY_COVERAGE_DIRECTORY = "official_candidate_query_coverage"
_CANDIDATE_ID = re.compile(r"[0-9a-f]{24}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TOP_LEVEL_FIELDS = {
    "schema",
    "role",
    "attempt_id",
    "candidate_snapshot_manifest_sha256",
    "basis",
    "causal_topology",
    "coverage_root",
    "method",
    "max_attempts_per_request",
    "requests",
    "final_test_strategy_outputs_read",
}
_BASIS_FIELDS = {
    "attempt_id",
    "candidate_snapshot_manifest_sha256",
    "base_coverage_sha256",
    "base_catalog_sha256",
    "blocked_admission_manifest_sha256",
    "blocked_unresolved_sha256",
}
_REQUEST_FIELDS = {
    "request_id",
    "candidate_ids",
    "scope",
    "request_sha256",
}
_SCOPE_FIELDS = {
    "symbol",
    "market",
    "category",
    "query_category",
    "start",
    "end",
    "org_id",
}


@dataclass(frozen=True)
class AuthorizedCandidateQueryRequest:
    """One request whose scope and candidates have been fully authorized."""

    request_id: str
    candidate_ids: tuple[str, ...]
    scope: OfficialQueryScope
    request_sha256: str


@dataclass(frozen=True)
class VerifiedCandidateQueryAuthorization:
    """A canonical authorization bound to old and successor candidate state."""

    path: Path
    sha256: str
    coverage_root: Path
    max_attempts_per_request: int
    requests: tuple[AuthorizedCandidateQueryRequest, ...]
    bindings: dict[str, dict[str, object]]

    def request(self, request_id: str) -> AuthorizedCandidateQueryRequest:
        matches = tuple(
            request
            for request in self.requests
            if request.request_id == request_id
        )
        if len(matches) != 1:
            raise ValueError(
                "candidate query authorization request is missing or ambiguous"
            )
        return matches[0]


def load_candidate_query_network_authorization(
    *,
    destination: Path,
    candidate_manifest_path: Path,
    blocked_admission_path: Path,
    network_authorization_path: Path,
    contract: FinalActionSourceContract,
    require_catalog_absent: bool,
) -> VerifiedCandidateQueryAuthorization:
    """Revalidate all lineage before returning any network-capable scope."""
    destination = destination.absolute()
    assert_directory(destination, label="candidate query destination")
    if require_catalog_absent and (
        destination / "official_announcement_catalog"
    ).exists():
        raise ValueError(
            "candidate query supplement must publish before the catalog already exists"
        )
    basis = load_candidate_query_basis(
        blocked_admission_path,
        contract=contract,
    )
    successor = load_successor_candidate_snapshot(
        destination=destination,
        candidate_manifest_path=candidate_manifest_path,
    )
    authorization_path = network_authorization_path.absolute()
    raw = read_regular(
        authorization_path,
        label="candidate query network authorization",
    )
    payload = _canonical_object(
        raw,
        label="candidate query network authorization",
    )
    topology_record = payload.get("causal_topology")
    topology_path = _binding_path(topology_record)
    topology = load_verified_candidate_query_topology(
        topology_path,
        contract=contract,
    )
    requests = _validate_authorization(
        payload,
        destination=destination,
        basis=basis,
        successor_candidates=successor.candidates,
        successor_manifest_path=successor.manifest_path,
        successor_candidate_sha256=successor.manifest_sha256,
        topology=topology,
        contract=contract,
    )
    coverage_root = _coverage_root(
        payload.get("coverage_root"),
        destination=destination,
    )
    bindings = {
        "candidate_snapshot": file_binding(candidate_manifest_path),
        **basis.bindings,
        "causal_topology": file_binding(topology_path),
        "network_authorization": file_binding(authorization_path),
    }
    return VerifiedCandidateQueryAuthorization(
        path=authorization_path,
        sha256=hashlib.sha256(raw).hexdigest(),
        coverage_root=coverage_root,
        max_attempts_per_request=int(payload["max_attempts_per_request"]),
        requests=requests,
        bindings=bindings,
    )


def candidate_query_request_id(
    *,
    candidate_ids: tuple[str, ...],
    scope: OfficialQueryScope,
    request_sha256: str,
) -> str:
    """Bind one request identity to its exact candidate set and scope."""
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "candidate_ids": list(candidate_ids),
                "scope": _scope_record(scope),
                "request_sha256": request_sha256,
            }
        )
    ).hexdigest()


def _validate_authorization(
    payload: dict[str, Any],
    *,
    destination: Path,
    basis,
    successor_candidates,
    successor_manifest_path: Path,
    successor_candidate_sha256: str,
    topology,
    contract: FinalActionSourceContract,
) -> tuple[AuthorizedCandidateQueryRequest, ...]:
    basis_record = payload.get("basis")
    records = payload.get("requests")
    attempts = payload.get("max_attempts_per_request")
    if (
        set(payload) != _TOP_LEVEL_FIELDS
        or payload.get("schema") != _AUTHORIZATION_SCHEMA
        or payload.get("role") != _AUTHORIZATION_ROLE
        or payload.get("attempt_id") != destination.name
        or payload.get("candidate_snapshot_manifest_sha256")
        != successor_candidate_sha256
        or payload.get("method") != "POST"
        or not isinstance(attempts, int)
        or isinstance(attempts, bool)
        or not 1 <= attempts <= 3
        or payload.get("final_test_strategy_outputs_read") is not False
        or not isinstance(basis_record, dict)
        or set(basis_record) != _BASIS_FIELDS
        or not isinstance(records, list)
        or not records
    ):
        raise ValueError("candidate query network authorization is invalid")
    admission = basis.admission
    candidate_record = admission.manifest["candidate_snapshot"]
    unresolved_record = admission.manifest["unresolved_candidates"]
    expected_basis = {
        "attempt_id": basis.root.name,
        "candidate_snapshot_manifest_sha256": candidate_record[
            "manifest_sha256"
        ],
        "base_coverage_sha256": basis.bindings["base_coverage"]["sha256"],
        "base_catalog_sha256": basis.bindings["base_catalog"]["sha256"],
        "blocked_admission_manifest_sha256": admission.manifest_sha256,
        "blocked_unresolved_sha256": unresolved_record["sha256"],
    }
    topology_bindings = topology.manifest.get("bindings")
    expected_topology = file_binding(topology.manifest_path)
    expected_topology_bindings = {
        **basis.bindings,
        "successor_candidate_snapshot": file_binding(
            successor_manifest_path
        ),
    }
    if (
        basis_record != expected_basis
        or payload.get("causal_topology") != expected_topology
        or topology_bindings != expected_topology_bindings
        or topology.manifest.get("attempt_id") != destination.name
        or topology.unclassified_candidate_ids
    ):
        raise ValueError("candidate query authorization basis differs")
    return _validate_requests(
        records,
        basis=basis,
        successor_candidates=successor_candidates,
        query_required_candidate_ids=(
            topology.query_required_candidate_ids
        ),
        contract=contract,
    )


def _validate_requests(
    records: list[object],
    *,
    basis,
    successor_candidates,
    query_required_candidate_ids: tuple[str, ...],
    contract: FinalActionSourceContract,
) -> tuple[AuthorizedCandidateQueryRequest, ...]:
    basis_by_id = {
        str(row["candidate_id"]): row
        for row in basis.candidates.candidates.iter_rows(named=True)
    }
    successor_by_id = {
        str(row["candidate_id"]): row
        for row in successor_candidates.iter_rows(named=True)
    }
    query_candidate_ids = set(query_required_candidate_ids)
    authorized: list[AuthorizedCandidateQueryRequest] = []
    seen_candidates: set[str] = set()
    previous_id = ""
    for record in records:
        if not isinstance(record, dict) or set(record) != _REQUEST_FIELDS:
            raise ValueError("candidate query authorization request is invalid")
        candidate_ids = _candidate_ids(
            record.get("candidate_ids"),
            already_seen=seen_candidates,
        )
        scope = _scope_from_record(record.get("scope"))
        request_sha256 = official_query_request_sha256(scope)
        request_id = candidate_query_request_id(
            candidate_ids=candidate_ids,
            scope=scope,
            request_sha256=request_sha256,
        )
        if (
            record.get("request_sha256") != request_sha256
            or record.get("request_id") != request_id
            or request_id <= previous_id
        ):
            raise ValueError("candidate query authorization request identity differs")
        _validate_candidate_scope(
            candidate_ids,
            scope=scope,
            basis_by_id=basis_by_id,
            successor_by_id=successor_by_id,
            query_candidate_ids=query_candidate_ids,
            org_ids=basis.org_ids,
            contract=contract,
        )
        seen_candidates.update(candidate_ids)
        previous_id = request_id
        authorized.append(
            AuthorizedCandidateQueryRequest(
                request_id=request_id,
                candidate_ids=candidate_ids,
                scope=scope,
                request_sha256=request_sha256,
            )
        )
    if seen_candidates != query_candidate_ids:
        raise ValueError(
            "candidate query authorization does not exactly cover query-needed candidates"
        )
    return tuple(authorized)


def _candidate_ids(
    value: object,
    *,
    already_seen: set[str],
) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or value != sorted(set(value))
        or not value
        or any(
            not isinstance(candidate_id, str)
            or _CANDIDATE_ID.fullmatch(candidate_id) is None
            for candidate_id in value
        )
        or already_seen.intersection(value)
    ):
        raise ValueError("candidate query authorization candidate set is invalid")
    return tuple(value)


def _validate_candidate_scope(
    candidate_ids: tuple[str, ...],
    *,
    scope: OfficialQueryScope,
    basis_by_id: dict[str, dict[str, object]],
    successor_by_id: dict[str, dict[str, object]],
    query_candidate_ids: set[str],
    org_ids: dict[str, str],
    contract: FinalActionSourceContract,
) -> None:
    expected_query_category = dict(contract.official_query_categories)[
        "corporate_actions"
    ]
    if (
        scope.market != market_for_symbol(scope.symbol)
        or scope.category != SHARED_ANNOUNCEMENT_CATEGORY
        or scope.query_category != expected_query_category
        or scope.org_id != org_ids.get(scope.symbol)
        or scope.start < contract.start
        or scope.end > contract.end
        or scope.start > scope.end
    ):
        raise ValueError("candidate query authorization scope differs")
    for candidate_id in candidate_ids:
        basis = basis_by_id.get(candidate_id)
        successor = successor_by_id.get(candidate_id)
        if (
            candidate_id not in query_candidate_ids
            or basis is None
            or successor is None
            or basis["symbol"] != scope.symbol
            or basis["ex_date"] is None
            or not scope.start <= basis["ex_date"] <= scope.end
            or any(basis[field] != successor[field] for field in CANDIDATE_FIELDS)
        ):
            raise ValueError(
                "candidate query authorization candidate scope differs"
            )


def _coverage_root(value: object, *, destination: Path) -> Path:
    if not isinstance(value, str):
        raise ValueError("candidate query authorization coverage root is invalid")
    path = Path(value)
    expected = destination / CANDIDATE_QUERY_COVERAGE_DIRECTORY
    if not path.is_absolute() or path.absolute() != expected.absolute():
        raise ValueError("candidate query authorization coverage root is invalid")
    assert_directory(path, label="candidate query authorization coverage root")
    return path.absolute()


def _binding_path(value: object) -> Path:
    if not isinstance(value, dict):
        raise ValueError("candidate query topology binding is invalid")
    path = Path(str(value.get("path", "")))
    if file_binding(path) != value:
        raise ValueError("candidate query topology binding changed")
    return path


def _scope_from_record(value: object) -> OfficialQueryScope:
    if not isinstance(value, dict) or set(value) != _SCOPE_FIELDS:
        raise ValueError("candidate query authorization scope is invalid")
    try:
        return OfficialQueryScope(
            symbol=str(value["symbol"]),
            market=str(value["market"]),
            category=str(value["category"]),
            query_category=str(value["query_category"]),
            start=date.fromisoformat(str(value["start"])),
            end=date.fromisoformat(str(value["end"])),
            org_id=str(value["org_id"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("candidate query authorization scope is invalid") from error


def _scope_record(scope: OfficialQueryScope) -> dict[str, object]:
    return {
        "symbol": scope.symbol,
        "market": scope.market,
        "category": scope.category,
        "query_category": scope.query_category,
        "start": scope.start.isoformat(),
        "end": scope.end.isoformat(),
        "org_id": scope.org_id,
    }


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
