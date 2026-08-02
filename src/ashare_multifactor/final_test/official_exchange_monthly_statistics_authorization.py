"""Exact offline authorization for one SZSE monthly-statistics GET."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from ashare_multifactor.final_test.action_source_contract import (
    load_action_source_contract,
)
from ashare_multifactor.final_test.official_candidate_query_supplement_basis import (
    file_binding,
    read_regular,
)
from ashare_multifactor.final_test.official_candidate_query_topology import (
    load_verified_candidate_query_topology,
)
from ashare_multifactor.final_test.official_candidate_review_admission_storage import (
    ADMISSION_DIRECTORY,
    ADMISSION_MANIFEST_NAME,
    load_published_candidate_review_admission,
)
from ashare_multifactor.final_test.official_evidence_workspace import (
    EvidenceWorkspace,
    VerifiedReviewQueue,
    load_verified_review_queue,
)
from ashare_multifactor.final_test.official_query_coverage import canonical_json_bytes
from ashare_multifactor.final_test.official_review_submission import (
    load_verified_candidate_snapshot,
)


STATISTICS_CACHE_DIRECTORY = "official_exchange_monthly_statistics_cache"

_SCHEMA = "stage9_exchange_monthly_statistics_network_authorization/v1"
_ROLE = "exchange_monthly_statistics_network_authorization"
_CANDIDATE_ID = re.compile(r"[0-9a-f]{24}")
_IMMUTABLE_ID = re.compile(r"[0-9a-f]{64}")
_SOURCE_PATH = re.compile(
    r"/www/market/periodical/month/W020(?P<asset_date>\d{6})\d+\.html"
)
_SOURCE_PREFIX = "https://docs.static.szse.cn/www/market/periodical/month/"


@dataclass(frozen=True)
class AuthorizedStatisticsRequest:
    """The sole permitted GET and its exclusive immutable output paths."""

    source_url: str
    request_sha256: str
    max_attempts: int
    document_path: Path
    receipt_path: Path


@dataclass(frozen=True)
class VerifiedStatisticsAuthorization:
    """Replayed authorization bound to one unresolved candidate."""

    path: Path
    sha256: str
    payload: dict[str, object]
    candidate_id: str
    related_catalog_ids: tuple[str, ...]
    request: AuthorizedStatisticsRequest


def build_exchange_monthly_statistics_authorization(
    *,
    code_root: Path,
    workspace: EvidenceWorkspace,
    queue: VerifiedReviewQueue,
    blocked_admission_path: Path,
    topology_manifest_path: Path,
    candidate_id: str,
    source_url: str,
    cache_root: Path,
    max_attempts: int = 3,
) -> dict[str, object]:
    """Build one transport-free, candidate-bound authorization payload."""
    verified_queue = load_verified_review_queue(workspace)
    if (
        queue.manifest_sha256 != verified_queue.manifest_sha256
        or not queue.frame.equals(verified_queue.frame)
        or verified_queue.manifest.get("schema_version") != "2"
        or _CANDIDATE_ID.fullmatch(candidate_id) is None
        or not isinstance(max_attempts, int)
        or isinstance(max_attempts, bool)
        or not 1 <= max_attempts <= 3
    ):
        raise ValueError("exchange statistics authorization scope is invalid")
    admission, candidates = _blocked_inputs(
        workspace,
        verified_queue,
        blocked_admission_path,
    )
    topology = load_verified_candidate_query_topology(
        topology_manifest_path,
        contract=load_action_source_contract(
            code_root / "configs/final_execution_sources.yaml"
        ),
    )
    candidate_rows = candidates.candidates.filter(
        candidates.candidates["candidate_id"] == candidate_id
    ).to_dicts()
    topology_rows = topology.unresolved.filter(
        topology.unresolved["candidate_id"] == candidate_id
    ).to_dicts()
    unresolved_ids = set(admission.unresolved.get_column("candidate_id"))
    bindings = topology.manifest.get("bindings")
    if (
        len(candidate_rows) != 1
        or len(topology_rows) != 1
        or candidate_id not in unresolved_ids
        or candidate_id not in topology.rendition_required_candidate_ids
        or candidate_id in topology.query_required_candidate_ids
        or candidate_id in topology.unclassified_candidate_ids
        or topology.root.parent.parent != workspace.root.parent
        or not isinstance(bindings, dict)
        or bindings.get("blocked_admission") != file_binding(admission.manifest_path)
        or bindings.get("successor_candidate_snapshot")
        != file_binding(candidates.manifest_path)
    ):
        raise ValueError("exchange statistics authorization topology differs")
    candidate = candidate_rows[0]
    related_catalog_ids = tuple(topology_rows[0]["related_catalog_ids"])
    if (
        not related_catalog_ids
        or tuple(sorted(set(related_catalog_ids))) != related_catalog_ids
        or not all(_IMMUTABLE_ID.fullmatch(value) for value in related_catalog_ids)
    ):
        raise ValueError("exchange statistics canonical scope differs")
    asset_date = _source_asset_date(source_url)
    ex_date = candidate["ex_date"]
    source_period_end = date(
        ex_date.year,
        ex_date.month,
        monthrange(ex_date.year, ex_date.month)[1],
    )
    publication_lag = (asset_date - source_period_end).days
    if (
        asset_date <= ex_date
        or asset_date < source_period_end
        or not 0 <= publication_lag <= 31
    ):
        raise ValueError("exchange statistics source is not post-event")
    expected_cache = workspace.root.parent / STATISTICS_CACHE_DIRECTORY
    if cache_root.absolute() != expected_cache.absolute():
        raise ValueError("exchange statistics cache root differs")
    _assert_directory(cache_root, label="exchange statistics cache root")
    request_root = cache_root / candidate_id
    request_core: dict[str, object] = {
        "purpose": "exchange_monthly_statistics",
        "method": "GET",
        "source_url": source_url,
        "host": "docs.static.szse.cn",
        "source_policy": "szse_monthly_market_statistics_v1",
        "max_attempts": max_attempts,
        "follow_redirects": False,
        "document_path": str((request_root / "statistics.html").absolute()),
        "receipt_path": str(
            (request_root / "statistics.receipt.json").absolute()
        ),
    }
    request = {
        **request_core,
        "request_sha256": hashlib.sha256(
            canonical_json_bytes(request_core)
        ).hexdigest(),
    }
    return {
        "schema": _SCHEMA,
        "role": _ROLE,
        "attempt_id": workspace.root.parent.name,
        "code_root": str(code_root.absolute()),
        "blocked_admission": file_binding(admission.manifest_path),
        "causal_topology": file_binding(topology.manifest_path),
        "candidate_snapshot": file_binding(candidates.manifest_path),
        "base_review_manifest": file_binding(verified_queue.manifest_path),
        "candidate": {
            "candidate_id": candidate_id,
            "symbol": str(candidate["symbol"]),
            "ex_date": ex_date.isoformat(),
            "effective_date": (
                candidate["effective_date"].isoformat()
                if candidate["effective_date"] is not None
                else None
            ),
            "cash_per_share": _decimal_literal(candidate["cash_per_share"]),
            "share_ratio": _decimal_literal(candidate["share_ratio"]),
        },
        "related_catalog_ids": list(related_catalog_ids),
        "source_period_end": source_period_end.isoformat(),
        "asset_path_date": asset_date.isoformat(),
        "post_event": True,
        "cache_root": str(cache_root.absolute()),
        "requests": [request],
        "final_test_strategy_outputs_read": False,
    }


def load_exchange_monthly_statistics_authorization(
    path: Path,
    *,
    workspace: EvidenceWorkspace,
    queue: VerifiedReviewQueue,
) -> VerifiedStatisticsAuthorization:
    """Rebuild the expected payload and reject authorization drift."""
    raw = read_regular(path, label="exchange statistics authorization")
    payload = _canonical_object(raw)
    candidate = payload.get("candidate")
    blocked = payload.get("blocked_admission")
    topology = payload.get("causal_topology")
    requests = payload.get("requests")
    if (
        payload.get("schema") != _SCHEMA
        or payload.get("role") != _ROLE
        or payload.get("attempt_id") != workspace.root.parent.name
        or payload.get("post_event") is not True
        or payload.get("final_test_strategy_outputs_read") is not False
        or not isinstance(candidate, dict)
        or not isinstance(blocked, dict)
        or not isinstance(topology, dict)
        or not isinstance(requests, list)
        or len(requests) != 1
        or not isinstance(requests[0], dict)
    ):
        raise ValueError("exchange statistics authorization is invalid")
    request = requests[0]
    expected = build_exchange_monthly_statistics_authorization(
        code_root=Path(str(payload.get("code_root", ""))),
        workspace=workspace,
        queue=queue,
        blocked_admission_path=Path(str(blocked.get("path", ""))),
        topology_manifest_path=Path(str(topology.get("path", ""))),
        candidate_id=str(candidate.get("candidate_id", "")),
        source_url=str(request.get("source_url", "")),
        cache_root=Path(str(payload.get("cache_root", ""))),
        max_attempts=int(request.get("max_attempts", 0)),
    )
    if payload != expected:
        raise ValueError("exchange statistics authorization differs")
    verified_request = AuthorizedStatisticsRequest(
        source_url=str(request["source_url"]),
        request_sha256=str(request["request_sha256"]),
        max_attempts=int(request["max_attempts"]),
        document_path=Path(str(request["document_path"])),
        receipt_path=Path(str(request["receipt_path"])),
    )
    return VerifiedStatisticsAuthorization(
        path=path.absolute(),
        sha256=hashlib.sha256(raw).hexdigest(),
        payload=payload,
        candidate_id=str(candidate["candidate_id"]),
        related_catalog_ids=tuple(payload["related_catalog_ids"]),
        request=verified_request,
    )


def _blocked_inputs(
    workspace: EvidenceWorkspace,
    queue: VerifiedReviewQueue,
    path: Path,
):
    absolute = path.absolute()
    root = absolute.parent
    destination = workspace.root.parent
    if (
        absolute.name != ADMISSION_MANIFEST_NAME
        or root.parent != destination / ADMISSION_DIRECTORY
        or _IMMUTABLE_ID.fullmatch(root.name) is None
    ):
        raise ValueError("exchange statistics blocked admission path differs")
    admission = load_published_candidate_review_admission(
        destination,
        admission_id=root.name,
    )
    snapshot = admission.manifest.get("candidate_snapshot")
    review = admission.manifest.get("review_queue")
    if (
        admission.ready
        or admission.manifest.get("status") != "blocked"
        or not isinstance(snapshot, dict)
        or not isinstance(review, dict)
        or review.get("manifest_sha256") != queue.manifest_sha256
    ):
        raise ValueError("exchange statistics blocked admission differs")
    candidates = load_verified_candidate_snapshot(
        destination / str(snapshot.get("relative_path", "")),
        workspace=workspace,
    )
    if candidates.manifest_sha256 != snapshot.get("manifest_sha256"):
        raise ValueError("exchange statistics candidate snapshot differs")
    return admission, candidates


def _source_asset_date(source_url: str) -> date:
    parsed = urlsplit(source_url)
    match = _SOURCE_PATH.fullmatch(parsed.path)
    if (
        not source_url.startswith(_SOURCE_PREFIX)
        or parsed.scheme != "https"
        or parsed.netloc != "docs.static.szse.cn"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or match is None
    ):
        raise ValueError("exchange statistics source URL is invalid")
    try:
        compact = f"20{match.group('asset_date')}"
        return date.fromisoformat(
            f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
        )
    except ValueError as error:
        raise ValueError("exchange statistics source URL is invalid") from error


def _decimal_literal(value: object) -> str:
    return format(float(value), ".12g")


def _canonical_object(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("exchange statistics authorization is invalid") from error
    if not isinstance(value, dict) or raw != canonical_json_bytes(value):
        raise ValueError("exchange statistics authorization is invalid")
    return value


def _assert_directory(path: Path, *, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label} is missing or unsafe")
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise ValueError(f"{label} uses a symlink")
