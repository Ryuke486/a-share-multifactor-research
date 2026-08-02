"""Transport-free authorization for one candidate rendition evidence pair."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

from ashare_multifactor.final_test.action_source_contract import (
    load_action_source_contract,
)
from ashare_multifactor.final_test.official_candidate_query_topology import (
    load_verified_candidate_query_topology,
)
from ashare_multifactor.final_test.historical_attempt import (
    load_historical_attempt_authorization,
)
from ashare_multifactor.final_test.official_candidate_query_supplement_basis import (
    file_binding,
    read_regular,
)
from ashare_multifactor.final_test.official_candidate_review_admission_storage import (
    ADMISSION_DIRECTORY,
    ADMISSION_MANIFEST_NAME,
    load_published_candidate_review_admission,
)
from ashare_multifactor.final_test.official_candidate_review_admission import (
    derive_unique_candidate_rendition_anchor,
)
from ashare_multifactor.final_test.official_evidence_workspace import (
    EvidenceWorkspace,
    VerifiedReviewQueue,
    load_verified_review_queue,
)
from ashare_multifactor.final_test.official_query_coverage import (
    canonical_json_bytes,
)
from ashare_multifactor.final_test.official_review_submission import (
    load_verified_candidate_snapshot,
)
from ashare_multifactor.final_test.preparation import verify_preparation
from ashare_multifactor.audit.publication import resolve_release


RENDITION_CACHE_DIRECTORY = "official_candidate_review_rendition_cache"

_SCHEMA = "stage9_candidate_review_rendition_network_authorization/v1"
_ROLE = "candidate_review_rendition_network_authorization"
_CANDIDATE_ID = re.compile(r"[0-9a-f]{24}")
_CATALOG_ID = re.compile(r"[0-9a-f]{64}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CNINFO_PATH = re.compile(r"/finalpage/(\d{4}-\d{2}-\d{2})/[^/]+\.PDF")
_STCN_PATH = re.compile(
    r"/att/(\d{4})(\d{2})/(\d{2})/[A-Za-z0-9_-]+_eBook\.pdf"
)
_PURPOSES = ("publisher_authority", "publisher_rendition")


@dataclass(frozen=True)
class AuthorizedRenditionRequest:
    """One exact HTTPS GET and its exclusive output paths."""

    purpose: str
    source_url: str
    request_sha256: str
    max_attempts: int
    document_path: Path
    receipt_path: Path


@dataclass(frozen=True)
class VerifiedCandidateReviewRenditionAuthorization:
    """A replayed authorization with exactly two immutable requests."""

    path: Path
    sha256: str
    payload: dict[str, object]
    candidate_id: str
    canonical_catalog_id: str
    requests: tuple[AuthorizedRenditionRequest, ...]

    def request(self, purpose: str) -> AuthorizedRenditionRequest:
        matches = tuple(
            request for request in self.requests if request.purpose == purpose
        )
        if len(matches) != 1:
            raise ValueError("candidate rendition authorization request differs")
        return matches[0]


def build_candidate_review_rendition_authorization(
    *,
    code_root: Path,
    workspace: EvidenceWorkspace,
    queue: VerifiedReviewQueue,
    blocked_admission_path: Path,
    topology_manifest_path: Path,
    candidate_id: str,
    rendition_source_url: str,
    authority_source_url: str,
    cache_root: Path,
    max_attempts: int = 3,
) -> dict[str, object]:
    """Build canonical authorization data without importing any transport."""
    verified_queue = _base_queue(workspace, queue)
    if (
        _CANDIDATE_ID.fullmatch(candidate_id) is None
        or not isinstance(max_attempts, int)
        or isinstance(max_attempts, bool)
        or not 1 <= max_attempts <= 3
    ):
        raise ValueError("candidate rendition authorization scope is invalid")
    admission, candidates = _blocked_candidate_inputs(
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
    unresolved_ids = set(
        admission.unresolved.get_column("candidate_id").to_list()
    )
    topology_rows = topology.unresolved.filter(
        topology.unresolved["candidate_id"] == candidate_id
    ).to_dicts()
    topology_bindings = topology.manifest.get("bindings")
    if (
        len(candidate_rows) != 1
        or candidate_id not in unresolved_ids
        or candidate_id not in topology.rendition_required_candidate_ids
        or candidate_id in topology.query_required_candidate_ids
        or candidate_id in topology.unclassified_candidate_ids
        or topology.unclassified_candidate_ids
        or len(topology_rows) != 1
        or topology.root.parent.parent != workspace.root.parent
        or topology.manifest.get("attempt_id") != workspace.root.parent.name
        or not isinstance(topology_bindings, dict)
        or topology_bindings.get("blocked_admission")
        != file_binding(admission.manifest_path)
        or topology_bindings.get("successor_candidate_snapshot")
        != file_binding(candidates.manifest_path)
    ):
        raise ValueError("candidate rendition authorization topology differs")
    candidate = candidate_rows[0]
    related_catalog_ids = tuple(topology_rows[0]["related_catalog_ids"])
    rendition_date = _stcn_date(rendition_source_url)
    anchor = derive_unique_candidate_rendition_anchor(
        workspace,
        verified_queue,
        symbol=str(candidate["symbol"]),
        related_catalog_ids=related_catalog_ids,
        required_publication_date=rendition_date,
    )
    canonical_catalog_id = str(anchor["catalog_id"])
    if _CATALOG_ID.fullmatch(canonical_catalog_id) is None:
        raise ValueError("candidate rendition authorization anchor differs")
    canonical_date = _cninfo_date(str(anchor["source_url"]))
    authority_date = _cninfo_date(authority_source_url)
    if (
        rendition_date != canonical_date
        or authority_date >= rendition_date
        or rendition_date > candidate["ex_date"]
    ):
        raise ValueError("candidate rendition authorization date differs")
    lineage = _lineage(
        code_root=code_root,
        workspace=workspace,
    )
    expected_cache = workspace.root.parent / RENDITION_CACHE_DIRECTORY
    if cache_root.absolute() != expected_cache.absolute():
        raise ValueError("candidate rendition authorization cache root differs")
    _assert_directory(cache_root, label="candidate rendition cache root")
    request_root = (
        cache_root.absolute() / candidate_id / canonical_catalog_id
    )
    request_specs = (
        ("publisher_authority", authority_source_url),
        ("publisher_rendition", rendition_source_url),
    )
    requests = [
        _request_record(
            purpose=purpose,
            source_url=source_url,
            max_attempts=max_attempts,
            request_root=request_root,
        )
        for purpose, source_url in request_specs
    ]
    candidate_record = {
        "candidate_id": candidate_id,
        "symbol": str(candidate["symbol"]),
        "ex_date": candidate["ex_date"].isoformat(),
        "effective_date": candidate["effective_date"].isoformat(),
    }
    canonical_record = {
        "catalog_id": canonical_catalog_id,
        "announcement_id": str(anchor["announcement_id"]),
        "symbol": str(anchor["symbol"]),
        "source_url": str(anchor["source_url"]),
        "publication_date": canonical_date.isoformat(),
        "document_cache_path": str(anchor["document_cache_path"]),
        "document_sha256": str(anchor["document_sha256"]),
        "document_size_bytes": int(anchor["document_size_bytes"]),
        "document_page_count": int(anchor["document_page_count"]),
        "document_media_type": str(anchor["document_media_type"]),
    }
    return {
        "schema": _SCHEMA,
        "role": _ROLE,
        "attempt_id": workspace.root.parent.name,
        "lineage": lineage,
        "blocked_admission": file_binding(admission.manifest_path),
        "causal_topology": file_binding(topology.manifest_path),
        "candidate_snapshot": file_binding(candidates.manifest_path),
        "base_review_manifest": file_binding(verified_queue.manifest_path),
        "candidate": candidate_record,
        "canonical_announcement": canonical_record,
        "cache_root": str(cache_root.absolute()),
        "requests": requests,
        "final_test_strategy_outputs_read": False,
    }


def load_candidate_review_rendition_authorization(
    path: Path,
    *,
    workspace: EvidenceWorkspace,
    queue: VerifiedReviewQueue,
) -> VerifiedCandidateReviewRenditionAuthorization:
    """Rebuild the expected authorization and reject arbitrary bound files."""
    raw = read_regular(path, label="candidate rendition authorization")
    payload = _canonical_object(raw)
    candidate = payload.get("candidate")
    canonical = payload.get("canonical_announcement")
    lineage = payload.get("lineage")
    blocked = payload.get("blocked_admission")
    topology = payload.get("causal_topology")
    requests = payload.get("requests")
    if (
        not isinstance(candidate, dict)
        or not isinstance(canonical, dict)
        or not isinstance(lineage, dict)
        or not isinstance(blocked, dict)
        or not isinstance(topology, dict)
        or not isinstance(requests, list)
        or len(requests) != 2
    ):
        raise ValueError("candidate rendition authorization is invalid")
    request_by_purpose = {
        str(record.get("purpose")): record
        for record in requests
        if isinstance(record, dict)
    }
    if set(request_by_purpose) != set(_PURPOSES):
        raise ValueError("candidate rendition authorization request differs")
    code_root = Path(str(lineage.get("code_root", "")))
    expected = build_candidate_review_rendition_authorization(
        code_root=code_root,
        workspace=workspace,
        queue=queue,
        blocked_admission_path=Path(str(blocked.get("path", ""))),
        topology_manifest_path=Path(str(topology.get("path", ""))),
        candidate_id=str(candidate.get("candidate_id", "")),
        rendition_source_url=str(
            request_by_purpose["publisher_rendition"].get("source_url", "")
        ),
        authority_source_url=str(
            request_by_purpose["publisher_authority"].get("source_url", "")
        ),
        cache_root=Path(str(payload.get("cache_root", ""))),
        max_attempts=int(
            request_by_purpose["publisher_rendition"].get(
                "max_attempts",
                0,
            )
        ),
    )
    if payload != expected:
        raise ValueError("candidate rendition authorization differs")
    verified_requests = tuple(
        AuthorizedRenditionRequest(
            purpose=str(record["purpose"]),
            source_url=str(record["source_url"]),
            request_sha256=str(record["request_sha256"]),
            max_attempts=int(record["max_attempts"]),
            document_path=Path(str(record["document_path"])),
            receipt_path=Path(str(record["receipt_path"])),
        )
        for record in requests
    )
    return VerifiedCandidateReviewRenditionAuthorization(
        path=path.absolute(),
        sha256=hashlib.sha256(raw).hexdigest(),
        payload=payload,
        candidate_id=str(candidate["candidate_id"]),
        canonical_catalog_id=str(canonical["catalog_id"]),
        requests=verified_requests,
    )


def _blocked_candidate_inputs(
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
        or _SHA256.fullmatch(root.name) is None
    ):
        raise ValueError("candidate rendition blocked admission path differs")
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
        raise ValueError("candidate rendition blocked admission differs")
    snapshot_path = destination / str(snapshot.get("relative_path", ""))
    candidates = load_verified_candidate_snapshot(
        snapshot_path,
        workspace=workspace,
    )
    if candidates.manifest_sha256 != snapshot.get("manifest_sha256"):
        raise ValueError("candidate rendition candidate snapshot differs")
    return admission, candidates


def _base_queue(
    workspace: EvidenceWorkspace,
    queue: VerifiedReviewQueue,
) -> VerifiedReviewQueue:
    verified = load_verified_review_queue(workspace)
    if (
        queue.manifest_sha256 != verified.manifest_sha256
        or not queue.frame.equals(verified.frame)
    ):
        raise ValueError("candidate rendition base review queue differs")
    if verified.manifest.get("schema_version") != "2":
        raise ValueError("candidate rendition authorization requires base review")
    return verified


def _lineage(*, code_root: Path, workspace: EvidenceWorkspace) -> dict[str, object]:
    code_root = code_root.absolute()
    _assert_directory(code_root, label="candidate rendition code root")
    data_root = workspace.root.parents[3]
    attempt_id = workspace.root.parent.name
    final_root = data_root / "processed/final_test"
    registry = final_root / "attempts"
    authorization = load_historical_attempt_authorization(
        data_root=data_root,
        attempt_id=attempt_id,
        code_root=code_root,
    )
    preparation = verify_preparation(
        final_root,
        attempt_id=attempt_id,
        authorization=authorization,
        expected_state="awaiting_official_evidence",
        require_current_stage8=True,
    )
    commit = _git(code_root, "rev-parse", "HEAD")
    tree = _git(code_root, "rev-parse", "HEAD^{tree}")
    if (
        authorization.git_commit != commit
        or authorization.git_tree != tree
    ):
        raise ValueError("candidate rendition authorization lineage differs")
    attempt_path = registry / f"{attempt_id}.json"
    state_path = registry / (
        f"{attempt_id}.state.02-awaiting_official_evidence.json"
    )
    token_path = registry / f"{attempt_id}.token"
    release = resolve_release(
        data_root / "processed/robustness",
        authorization.robustness_release,
    )
    seal_path = release.artifacts / "sealed_test_protocol.json"
    seal = _json_file(seal_path, label="candidate rendition seal")
    ledger_root = Path(str(seal.get("opening_ledger_root", "")))
    ledger_path = ledger_root / f"{authorization.sealed_protocol_sha256}.json"
    return {
        "code_root": str(code_root),
        "git_commit": commit,
        "git_tree": tree,
        "authorization": {
            "attempt_id": authorization.attempt_id,
            "approval_id": authorization.approval_id,
            "registered_at": authorization.registered_at,
            "git_commit": authorization.git_commit,
            "git_tree": authorization.git_tree,
            "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
            "robustness_release": authorization.robustness_release,
            "robustness_manifest_sha256": (
                authorization.robustness_manifest_sha256
            ),
            "robustness_lineage_sha256": (
                authorization.robustness_lineage_sha256
            ),
            "test_period": [
                authorization.test_period[0].isoformat(),
                authorization.test_period[1].isoformat(),
            ],
        },
        "parent_authorization": file_binding(attempt_path),
        "attempt_state": file_binding(state_path),
        "token_snapshot": file_binding(token_path),
        "consumed_opening_ledger": file_binding(ledger_path),
        "release_manifest": file_binding(release.manifest),
        "release_lineage": file_binding(release.lineage),
        "seal": file_binding(seal_path),
        "preparation": file_binding(preparation.manifest_path),
    }


def _request_record(
    *,
    purpose: str,
    source_url: str,
    max_attempts: int,
    request_root: Path,
) -> dict[str, object]:
    parsed = urlsplit(source_url)
    expected_host = (
        "static.cninfo.com.cn"
        if purpose == "publisher_authority"
        else "epaper.stcn.com"
    )
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected_host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("candidate rendition authorization URL differs")
    stem = purpose.removeprefix("publisher_")
    core: dict[str, object] = {
        "purpose": purpose,
        "method": "GET",
        "source_url": source_url,
        "host": expected_host,
        "max_attempts": max_attempts,
        "follow_redirects": False,
        "document_path": str((request_root / f"{stem}.pdf").absolute()),
        "receipt_path": str(
            (request_root / f"{stem}.receipt.json").absolute()
        ),
    }
    return {
        **core,
        "request_sha256": hashlib.sha256(
            canonical_json_bytes(core)
        ).hexdigest(),
    }


def _cninfo_date(source_url: str) -> date:
    parsed = urlsplit(source_url)
    match = _CNINFO_PATH.fullmatch(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "static.cninfo.com.cn"
        or match is None
    ):
        raise ValueError("candidate rendition CNInfo URL differs")
    return date.fromisoformat(match.group(1))


def _stcn_date(source_url: str) -> date:
    parsed = urlsplit(source_url)
    match = _STCN_PATH.fullmatch(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "epaper.stcn.com"
        or match is None
    ):
        raise ValueError("candidate rendition publisher URL differs")
    return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _json_file(path: Path, *, label: str) -> dict[str, object]:
    raw = read_regular(path, label=label)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is invalid") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} is invalid")
    return value


def _canonical_object(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("candidate rendition authorization is invalid") from error
    if not isinstance(value, dict) or raw != canonical_json_bytes(value):
        raise ValueError("candidate rendition authorization is invalid")
    return value


def _assert_directory(path: Path, *, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label} is missing or unsafe")
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise ValueError(f"{label} uses a symlink")


def _git(root: Path, *args: str) -> str:
    environment = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    return subprocess.run(
        ("git", "-c", "core.fsmonitor=false", *args),
        cwd=root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
