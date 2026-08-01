"""Verified publisher renditions for candidate admission only."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re

from ashare_multifactor.audit.secure_tree import (
    read_frozen_tree_at,
    write_frozen_tree_at,
)
from ashare_multifactor.final_test.official_candidate_query_supplement_basis import (
    file_binding,
)
from ashare_multifactor.final_test.official_candidate_review_rendition_authorization import (
    load_candidate_review_rendition_authorization,
)
from ashare_multifactor.final_test.official_candidate_review_rendition_validation import (
    CandidateReviewRenditionInput,
    build_rendition_record,
    validate_rendition_record,
)
from ashare_multifactor.final_test.official_evidence_workspace import (
    DOCUMENTS_DIRECTORY,
    EvidenceWorkspace,
    REVIEW_MANIFEST_NAME,
    REVIEW_QUEUE_NAME,
    REVIEW_SESSIONS_DIRECTORY,
    VerifiedReviewQueue,
    load_cached_document,
    load_verified_review_queue,
)
from ashare_multifactor.final_test.official_query_collection_root import (
    opened_safe_directory,
)
from ashare_multifactor.final_test.official_query_coverage import canonical_json_bytes


RENDITIONS_DIRECTORY = "official_candidate_review_renditions"
RENDITION_MANIFEST_NAME = "rendition_manifest.json"

_SCHEMA_VERSION = "1"
_QUEUE_SCHEMA_VERSION = "3"
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class VerifiedCandidateReviewRenditions:
    """Content-addressed rendition records anchored to one base review queue."""

    root: Path
    manifest_path: Path
    manifest_sha256: str
    manifest: dict[str, object]
    records: tuple[dict[str, object], ...]


def publish_candidate_review_renditions(
    *,
    workspace: EvidenceWorkspace,
    queue: VerifiedReviewQueue,
    network_authorization_path: Path,
    renditions: tuple[CandidateReviewRenditionInput, ...],
) -> VerifiedCandidateReviewRenditions:
    """Validate and freeze rendition bytes without changing the base queue."""
    verified_queue = load_verified_review_queue(workspace)
    if (
        queue.manifest_sha256 != verified_queue.manifest_sha256
        or not queue.frame.equals(verified_queue.frame)
        or len(renditions) != 1
    ):
        raise ValueError("candidate review rendition inputs are not verified")
    authorization = load_candidate_review_rendition_authorization(
        network_authorization_path,
        workspace=workspace,
        queue=verified_queue,
    )
    records: list[dict[str, object]] = []
    files: dict[str, bytes] = {}
    seen_catalog_ids: set[str] = set()
    for item in renditions:
        if item.canonical_catalog_id in seen_catalog_ids:
            raise ValueError("candidate review rendition catalog anchor repeats")
        seen_catalog_ids.add(item.canonical_catalog_id)
        if item.canonical_catalog_id != authorization.canonical_catalog_id:
            raise ValueError("candidate review rendition authorization differs")
        anchored = _anchored_row(verified_queue, item.canonical_catalog_id)
        if (
            anchored["route"] != "candidate"
            or anchored["candidate_type"] != "corporate_action"
        ):
            raise ValueError("candidate review rendition anchor is not eligible")
        canonical_payload = _canonical_document_payload(
            workspace,
            anchored=anchored,
        )
        record, record_files = build_rendition_record(
            item,
            anchored=anchored,
            canonical_payload=canonical_payload,
            authorization_sha256=authorization.sha256,
            rendition_request=authorization.request(
                "publisher_rendition"
            ),
            authority_request=authorization.request(
                "publisher_authority"
            ),
        )
        for path, payload in record_files.items():
            _add_immutable_file(files, path, payload)
        records.append(record)
    records.sort(key=lambda value: value["canonical_announcement"]["catalog_id"])
    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "role": "official_candidate_review_renditions",
        "attempt_id": workspace.root.parent.name,
        "base_review_manifest": {
            "relative_path": queue.manifest_path.relative_to(
                workspace.root.parent
            ).as_posix(),
            "sha256": queue.manifest_sha256,
        },
        "network_authorization": file_binding(authorization.path),
        "records": records,
        "record_count": len(records),
        "trusted_publisher_policy": "securities_times_epaper_v1",
        "final_test_strategy_outputs_read": False,
    }
    manifest_bytes = canonical_json_bytes(manifest)
    rendition_id = hashlib.sha256(manifest_bytes).hexdigest()
    files[RENDITION_MANIFEST_NAME] = manifest_bytes
    destination = workspace.root.parent
    with opened_safe_directory(
        destination,
        label="official evidence destination",
    ) as root_fd:
        try:
            os.mkdir(RENDITIONS_DIRECTORY, mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        renditions_fd = os.open(
            RENDITIONS_DIRECTORY,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        try:
            write_frozen_tree_at(
                renditions_fd,
                rendition_id,
                files,
                resumable=True,
                label="candidate review renditions",
            )
        finally:
            os.close(renditions_fd)
    return load_verified_candidate_review_renditions(
        destination
        / RENDITIONS_DIRECTORY
        / rendition_id
        / RENDITION_MANIFEST_NAME,
        workspace=workspace,
        queue=queue,
    )


def load_verified_candidate_review_renditions(
    manifest_path: Path,
    *,
    workspace: EvidenceWorkspace,
    queue: VerifiedReviewQueue,
) -> VerifiedCandidateReviewRenditions:
    """Revalidate every rendition, receipt, authority document, and queue anchor."""
    absolute = manifest_path.absolute()
    root = absolute.parent
    if (
        absolute.name != RENDITION_MANIFEST_NAME
        or root.parent != workspace.root.parent / RENDITIONS_DIRECTORY
        or _SHA256.fullmatch(root.name) is None
    ):
        raise ValueError("candidate review rendition path is invalid")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        files = read_frozen_tree_at(
            descriptor,
            label="candidate review renditions",
        )
    finally:
        os.close(descriptor)
    manifest_bytes = files.get(RENDITION_MANIFEST_NAME, b"")
    manifest = _canonical_object(manifest_bytes)
    base = manifest.get("base_review_manifest")
    current_base_sha = (
        queue.manifest.get("base_review_manifest", {}).get("sha256")
        if isinstance(queue.manifest.get("base_review_manifest"), dict)
        else queue.manifest_sha256
    )
    authorization = manifest.get("network_authorization")
    records = manifest.get("records")
    if (
        hashlib.sha256(manifest_bytes).hexdigest() != root.name
        or manifest.get("schema_version") != _SCHEMA_VERSION
        or manifest.get("role") != "official_candidate_review_renditions"
        or manifest.get("attempt_id") != workspace.root.parent.name
        or manifest.get("trusted_publisher_policy")
        != "securities_times_epaper_v1"
        or manifest.get("final_test_strategy_outputs_read") is not False
        or not isinstance(base, dict)
        or base.get("sha256") != current_base_sha
        or not isinstance(records, list)
        or manifest.get("record_count") != len(records)
    ):
        raise ValueError("candidate review rendition manifest is invalid")
    authorization_path = _binding_path(authorization)
    authorization_queue = _authorization_base_queue(
        workspace,
        queue,
        base=base,
    )
    verified_authorization = (
        load_candidate_review_rendition_authorization(
            authorization_path,
            workspace=replace(
                workspace,
                review_queue_path=authorization_queue.manifest_path.with_name(
                    REVIEW_QUEUE_NAME
                ),
            ),
            queue=authorization_queue,
        )
    )
    expected_files = {RENDITION_MANIFEST_NAME}
    catalog_ids: list[str] = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(
            record.get("canonical_announcement"),
            dict,
        ):
            raise ValueError("candidate review rendition record is invalid")
        catalog_id = str(record["canonical_announcement"].get("catalog_id", ""))
        catalog_ids.append(catalog_id)
        if catalog_id != verified_authorization.canonical_catalog_id:
            raise ValueError("candidate review rendition authorization differs")
        anchored = _anchored_row(queue, catalog_id)
        expected_files.update(
            validate_rendition_record(
                record,
                files=files,
                anchored=anchored,
                canonical_payload=_canonical_document_payload(
                    workspace,
                    anchored=anchored,
                ),
                authorization_sha256=verified_authorization.sha256,
                rendition_request=verified_authorization.request(
                    "publisher_rendition"
                ),
                authority_request=verified_authorization.request(
                    "publisher_authority"
                ),
            )
        )
    if catalog_ids != sorted(set(catalog_ids)):
        raise ValueError("candidate review rendition record order differs")
    if set(files) != expected_files:
        raise ValueError("candidate review rendition inventory differs")
    return VerifiedCandidateReviewRenditions(
        root=root,
        manifest_path=absolute,
        manifest_sha256=root.name,
        manifest=manifest,
        records=tuple(records),
    )


def _authorization_base_queue(
    workspace: EvidenceWorkspace,
    queue: VerifiedReviewQueue,
    *,
    base: object,
) -> VerifiedReviewQueue:
    if queue.manifest.get("schema_version") == "2":
        return queue
    if (
        queue.manifest.get("schema_version") != _QUEUE_SCHEMA_VERSION
        or not isinstance(base, dict)
        or not isinstance(base.get("relative_path"), str)
    ):
        raise ValueError("candidate review rendition base queue differs")
    manifest_path = workspace.root.parent / str(base["relative_path"])
    base_workspace = replace(
        workspace,
        review_queue_path=manifest_path.with_name(REVIEW_QUEUE_NAME),
    )
    verified = load_verified_review_queue(base_workspace)
    if (
        verified.manifest_path != manifest_path
        or verified.manifest_sha256 != base.get("sha256")
        or verified.manifest.get("schema_version") != "2"
    ):
        raise ValueError("candidate review rendition base queue differs")
    return verified


def _binding_path(value: object) -> Path:
    if not isinstance(value, dict):
        raise ValueError("candidate review rendition authorization is invalid")
    path = Path(str(value.get("path", "")))
    if file_binding(path) != value:
        raise ValueError("candidate review rendition authorization changed")
    return path


def bind_candidate_review_renditions(
    *,
    workspace: EvidenceWorkspace,
    queue: VerifiedReviewQueue,
    renditions: VerifiedCandidateReviewRenditions,
) -> EvidenceWorkspace:
    """Publish a new immutable review-session identity bound to renditions."""
    verified_queue = load_verified_review_queue(workspace)
    loaded = load_verified_candidate_review_renditions(
        renditions.manifest_path,
        workspace=workspace,
        queue=verified_queue,
    )
    if (
        queue.manifest_sha256 != verified_queue.manifest_sha256
        or renditions.manifest_sha256 != loaded.manifest_sha256
    ):
        raise ValueError("candidate review rendition binding inputs differ")
    if verified_queue.manifest.get("schema_version") != "2":
        raise ValueError(
            "candidate review rendition base review queue must use schema 2"
        )
    base_queue = verified_queue.manifest.get("review_queue")
    if not isinstance(base_queue, dict) or not isinstance(
        base_queue.get("sha256"),
        str,
    ):
        raise ValueError("candidate review rendition base review queue is invalid")
    manifest = dict(verified_queue.manifest)
    manifest["schema_version"] = _QUEUE_SCHEMA_VERSION
    manifest["base_review_manifest"] = {
        "relative_path": verified_queue.manifest_path.relative_to(
            workspace.root.parent
        ).as_posix(),
        "sha256": verified_queue.manifest_sha256,
        "queue_sha256": base_queue["sha256"],
    }
    manifest["candidate_review_renditions"] = {
        "relative_path": loaded.manifest_path.relative_to(
            workspace.root.parent
        ).as_posix(),
        "manifest_sha256": loaded.manifest_sha256,
        "record_count": len(loaded.records),
    }
    manifest_bytes = canonical_json_bytes(manifest)
    queue_bytes = workspace.review_queue_path.read_bytes()
    session_id = hashlib.sha256(
        canonical_json_bytes(
            {
                "base_review_manifest_sha256": verified_queue.manifest_sha256,
                "base_review_queue_sha256": base_queue["sha256"],
                "rendition_manifest_sha256": loaded.manifest_sha256,
            }
        )
    ).hexdigest()
    sessions_root = workspace.root / REVIEW_SESSIONS_DIRECTORY
    descriptor = os.open(
        sessions_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        write_frozen_tree_at(
            descriptor,
            session_id,
            {
                REVIEW_QUEUE_NAME: queue_bytes,
                REVIEW_MANIFEST_NAME: manifest_bytes,
            },
            resumable=True,
            label="official evidence review session",
        )
    finally:
        os.close(descriptor)
    bound = EvidenceWorkspace(
        root=workspace.root,
        catalog_path=workspace.catalog_path,
        routing_path=workspace.routing_path,
        review_queue_path=(
            sessions_root / session_id / REVIEW_QUEUE_NAME
        ),
        ready=False,
    )
    load_verified_review_queue(bound)
    return bound


def _anchored_row(
    queue: VerifiedReviewQueue,
    catalog_id: str,
) -> dict[str, object]:
    rows = queue.frame.filter(
        queue.frame["catalog_id"] == catalog_id
    ).to_dicts()
    if len(rows) != 1:
        raise ValueError("candidate review rendition anchor is not unique")
    return rows[0]


def _add_immutable_file(
    files: dict[str, bytes],
    path: str,
    payload: bytes,
) -> None:
    existing = files.setdefault(path, payload)
    if existing != payload:
        raise ValueError("candidate review rendition content hash collides")


def _canonical_document_payload(
    workspace: EvidenceWorkspace,
    *,
    anchored: dict[str, object],
) -> bytes:
    if anchored.get("document_status") != "cached":
        raise ValueError("candidate review rendition canonical document is not cached")
    documents_root = workspace.root / DOCUMENTS_DIRECTORY
    descriptor = os.open(
        documents_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        cached = load_cached_document(
            descriptor,
            source_url=str(anchored["source_url"]),
        )
    finally:
        os.close(descriptor)
    if (
        cached is None
        or anchored.get("document_cache_path") != cached.cache_path
        or anchored.get("document_sha256") != cached.sha256
        or anchored.get("document_size_bytes") != cached.size_bytes
        or anchored.get("document_page_count") != cached.page_count
        or anchored.get("document_media_type") != cached.media_type
    ):
        raise ValueError("candidate review rendition canonical document differs")
    path = workspace.root / cached.cache_path
    if (
        path.is_symlink()
        or not path.is_file()
        or path.parent.parent.parent.absolute() != workspace.root.absolute()
    ):
        raise ValueError("candidate review rendition canonical path is invalid")
    return path.read_bytes()


def _canonical_object(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("candidate review rendition manifest is invalid") from error
    if not isinstance(value, dict) or raw != canonical_json_bytes(value):
        raise ValueError("candidate review rendition manifest is invalid")
    return value
