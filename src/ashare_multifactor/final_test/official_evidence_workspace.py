"""Immutable document cache and fail-closed human-review workspace."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import stat
from typing import Iterator, Mapping

import polars as pl

from ashare_multifactor.audit.secure_tree import (
    read_frozen_tree_at,
    write_frozen_tree_at,
)
from ashare_multifactor.final_test.official_announcement_catalog import (
    VerifiedAnnouncementCatalog,
    load_verified_announcement_catalog,
)
from ashare_multifactor.final_test.official_announcement_routing import (
    ROUTING_DIRECTORY,
    ROUTING_NAME,
    VerifiedAnnouncementRouting,
    load_verified_announcement_routing,
)
from ashare_multifactor.final_test.official_document_validation import (
    validate_official_document,
)
from ashare_multifactor.final_test.official_query_coverage import canonical_json_bytes
from ashare_multifactor.final_test.recovery_secure_fs import open_directory_at


WORKSPACE_DIRECTORY = "official_document_workspace"
DOCUMENTS_DIRECTORY = "documents"
QUARANTINE_DIRECTORY = "quarantine"
REVIEW_SESSIONS_DIRECTORY = "review_sessions"
REVIEW_QUEUE_NAME = "review_queue.parquet"
REVIEW_MANIFEST_NAME = "review_manifest.json"
DOCUMENT_NAME = "document.bin"
DOCUMENT_MANIFEST_NAME = "document_manifest.json"
QUARANTINE_RESPONSE_NAME = "response.bin"
QUARANTINE_MANIFEST_NAME = "quarantine_manifest.json"

_SCHEMA_VERSION = "2"
_RENDITION_SCHEMA_VERSION = "3"
_QUARANTINE_SCHEMA_VERSION = "1"
_IMMUTABLE_ENTRY = re.compile(r"[0-9a-f]{64}")
_QUEUE_COLUMNS = (
    "catalog_id",
    "announcement_id",
    "announcement_title",
    "source_url",
    "source",
    "symbol",
    "market",
    "category",
    "route",
    "candidate_type",
    "routing_reason",
    "source_response",
    "document_cache_path",
    "document_sha256",
    "document_size_bytes",
    "document_page_count",
    "document_media_type",
    "document_quarantine_path",
    "document_validation_reason",
    "document_status",
    "review_status",
)
_QUEUE_SCHEMA = {
    "catalog_id": pl.String,
    "announcement_id": pl.String,
    "announcement_title": pl.String,
    "source_url": pl.String,
    "source": pl.String,
    "symbol": pl.String,
    "market": pl.String,
    "category": pl.String,
    "route": pl.String,
    "candidate_type": pl.String,
    "routing_reason": pl.String,
    "source_response": pl.String,
    "document_cache_path": pl.String,
    "document_sha256": pl.String,
    "document_size_bytes": pl.Int64,
    "document_page_count": pl.Int64,
    "document_media_type": pl.String,
    "document_quarantine_path": pl.String,
    "document_validation_reason": pl.String,
    "document_status": pl.String,
    "review_status": pl.String,
}


@dataclass(frozen=True)
class EvidenceWorkspace:
    """Review-only artifacts; ``ready`` can never be inferred from raw documents."""

    root: Path
    catalog_path: Path
    routing_path: Path
    review_queue_path: Path
    ready: bool


@dataclass(frozen=True)
class CachedOfficialDocument:
    """Exact bytes cached for one approved official document URL."""

    source_url: str
    cache_path: str
    sha256: str
    size_bytes: int
    page_count: int
    media_type: str


@dataclass(frozen=True)
class QuarantinedOfficialDocument:
    """Invalid response bytes preserved outside the valid document cache."""

    source_url: str
    quarantine_path: str
    sha256: str
    size_bytes: int
    reason: str


@dataclass(frozen=True)
class VerifiedReviewQueue:
    """One immutable queue with its catalog and routing lineage rechecked."""

    frame: pl.DataFrame
    manifest: dict[str, object]
    manifest_path: Path
    manifest_sha256: str
    session_id: str


@contextmanager
def open_workspace(destination: Path) -> Iterator[tuple[Path, int, int, int, int]]:
    """Open or create the sole review workspace below a safe evidence root."""
    _assert_safe_directory(destination, label="official evidence destination")
    root_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    workspace_fd: int | None = None
    documents_fd: int | None = None
    quarantine_fd: int | None = None
    sessions_fd: int | None = None
    try:
        workspace_fd = _open_or_create_directory(
            root_fd,
            WORKSPACE_DIRECTORY,
            label="official document workspace",
        )
        documents_fd = _open_or_create_directory(
            workspace_fd,
            DOCUMENTS_DIRECTORY,
            label="official document cache",
        )
        quarantine_fd = _open_or_create_directory(
            workspace_fd,
            QUARANTINE_DIRECTORY,
            label="official document quarantine",
        )
        sessions_fd = _open_or_create_directory(
            workspace_fd,
            REVIEW_SESSIONS_DIRECTORY,
            label="official review sessions",
        )
        if set(os.listdir(workspace_fd)) != {
            DOCUMENTS_DIRECTORY,
            QUARANTINE_DIRECTORY,
            REVIEW_SESSIONS_DIRECTORY,
        }:
            raise ValueError("official document workspace inventory is unsafe")
        _assert_immutable_directory_inventory(
            documents_fd,
            label="official document cache",
        )
        _assert_immutable_directory_inventory(
            quarantine_fd,
            label="official document quarantine",
        )
        _assert_immutable_directory_inventory(
            sessions_fd,
            label="official review sessions",
        )
        yield (
            destination / WORKSPACE_DIRECTORY,
            workspace_fd,
            documents_fd,
            quarantine_fd,
            sessions_fd,
        )
    finally:
        if sessions_fd is not None:
            os.close(sessions_fd)
        if documents_fd is not None:
            os.close(documents_fd)
        if quarantine_fd is not None:
            os.close(quarantine_fd)
        if workspace_fd is not None:
            os.close(workspace_fd)
        os.close(root_fd)


def load_cached_document(
    documents_fd: int,
    *,
    source_url: str,
) -> CachedOfficialDocument | None:
    """Return exact cached bytes metadata, rejecting a malformed cache entry."""
    cache_id = _cache_id(source_url)
    try:
        metadata = os.stat(cache_id, dir_fd=documents_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("official document cache entry is unsafe")
    cache_fd = open_directory_at(
        documents_fd,
        cache_id,
        label="official document cache entry",
    )
    try:
        files = read_frozen_tree_at(cache_fd, label="official document cache entry")
    finally:
        os.close(cache_fd)
    return _validate_document_tree(files, source_url=source_url, cache_id=cache_id)


def publish_document_cache(
    documents_fd: int,
    *,
    source_url: str,
    payload: bytes,
) -> CachedOfficialDocument:
    """Write one immutable document tree, never replacing cached bytes."""
    validated = validate_official_document(payload, source_url=source_url)
    cache_id = _cache_id(source_url)
    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "role": "official_document_cache",
        "source_url": source_url,
        "cache_path": f"{DOCUMENTS_DIRECTORY}/{cache_id}/{DOCUMENT_NAME}",
        "sha256": validated.sha256,
        "size_bytes": validated.size_bytes,
        "media_type": validated.media_type,
        "page_count": validated.page_count,
    }
    write_frozen_tree_at(
        documents_fd,
        cache_id,
        {
            DOCUMENT_NAME: payload,
            DOCUMENT_MANIFEST_NAME: canonical_json_bytes(manifest),
        },
        resumable=True,
        label="official document cache entry",
    )
    cached = load_cached_document(documents_fd, source_url=source_url)
    if (
        cached is None
        or cached.sha256 != validated.sha256
        or cached.size_bytes != validated.size_bytes
        or cached.page_count != validated.page_count
    ):
        raise ValueError("official document cache identity differs after publication")
    return cached


def load_quarantined_document(
    quarantine_fd: int,
    *,
    source_url: str,
) -> QuarantinedOfficialDocument | None:
    """Return one immutable invalid response previously kept outside the cache."""
    quarantine_id = _cache_id(source_url)
    try:
        metadata = os.stat(quarantine_id, dir_fd=quarantine_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("official document quarantine entry is unsafe")
    entry_fd = open_directory_at(
        quarantine_fd,
        quarantine_id,
        label="official document quarantine entry",
    )
    try:
        files = read_frozen_tree_at(
            entry_fd,
            label="official document quarantine entry",
        )
    finally:
        os.close(entry_fd)
    return _validate_quarantine_tree(
        files,
        source_url=source_url,
        quarantine_id=quarantine_id,
    )


def publish_document_quarantine(
    quarantine_fd: int,
    *,
    source_url: str,
    payload: bytes,
    reason: str,
) -> QuarantinedOfficialDocument:
    """Preserve invalid response bytes without admitting them as PDF evidence."""
    if (
        not isinstance(payload, bytes)
        or not payload
        or not isinstance(reason, str)
        or not reason
    ):
        raise ValueError("official document quarantine response is invalid")
    quarantine_id = _cache_id(source_url)
    digest = hashlib.sha256(payload).hexdigest()
    manifest = {
        "schema_version": _QUARANTINE_SCHEMA_VERSION,
        "role": "official_document_quarantine",
        "source_url": source_url,
        "quarantine_path": (
            f"{QUARANTINE_DIRECTORY}/{quarantine_id}/{QUARANTINE_RESPONSE_NAME}"
        ),
        "sha256": digest,
        "size_bytes": len(payload),
        "reason": reason,
    }
    write_frozen_tree_at(
        quarantine_fd,
        quarantine_id,
        {
            QUARANTINE_RESPONSE_NAME: payload,
            QUARANTINE_MANIFEST_NAME: canonical_json_bytes(manifest),
        },
        resumable=True,
        label="official document quarantine entry",
    )
    quarantined = load_quarantined_document(quarantine_fd, source_url=source_url)
    if (
        quarantined is None
        or quarantined.sha256 != digest
        or quarantined.size_bytes != len(payload)
        or quarantined.reason != reason
    ):
        raise ValueError("official document quarantine identity differs after publication")
    return quarantined


def publish_review_workspace(
    *,
    workspace_root: Path,
    sessions_fd: int,
    catalog: VerifiedAnnouncementCatalog,
    routing: VerifiedAnnouncementRouting,
    cached_documents: Mapping[str, CachedOfficialDocument],
    quarantined_documents: Mapping[str, QuarantinedOfficialDocument],
) -> EvidenceWorkspace:
    """Publish one immutable review snapshot and deliberately leave it unready."""
    rows = _review_rows(
        catalog.frame,
        routing.frame,
        cached_documents,
        quarantined_documents,
    )
    queue = pl.DataFrame(rows, schema=_QUEUE_SCHEMA).sort("catalog_id")
    queue_bytes = _parquet_bytes(queue)
    document_records = [
        {
            "source_url": item.source_url,
            "cache_path": item.cache_path,
            "sha256": item.sha256,
            "size_bytes": item.size_bytes,
            "page_count": item.page_count,
            "media_type": item.media_type,
        }
        for item in sorted(cached_documents.values(), key=lambda value: value.source_url)
    ]
    quarantine_records = [
        {
            "source_url": item.source_url,
            "quarantine_path": item.quarantine_path,
            "sha256": item.sha256,
            "size_bytes": item.size_bytes,
            "reason": item.reason,
        }
        for item in sorted(
            quarantined_documents.values(),
            key=lambda value: value.source_url,
        )
    ]
    session_id = hashlib.sha256(
        canonical_json_bytes(
            {
                "catalog_sha256": catalog.catalog_sha256,
                "routing_sha256": routing.routing_sha256,
                "documents": document_records,
                "quarantined_documents": quarantine_records,
            }
        )
    ).hexdigest()
    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "role": "official_evidence_review_workspace",
        "catalog": {
            "relative_path": _catalog_relative_path(catalog.path),
            "sha256": catalog.catalog_sha256,
        },
        "routing": {
            "relative_path": f"{ROUTING_DIRECTORY}/{ROUTING_NAME}",
            "sha256": routing.routing_sha256,
        },
        "documents": document_records,
        "quarantined_documents": quarantine_records,
        "review_queue": {
            "path": REVIEW_QUEUE_NAME,
            "sha256": hashlib.sha256(queue_bytes).hexdigest(),
            "size_bytes": len(queue_bytes),
            "row_count": queue.height,
        },
        "ready": False,
        "execution_coverage_published": False,
    }
    write_frozen_tree_at(
        sessions_fd,
        session_id,
        {
            REVIEW_QUEUE_NAME: queue_bytes,
            REVIEW_MANIFEST_NAME: canonical_json_bytes(manifest),
        },
        resumable=True,
        label="official evidence review session",
    )
    session_root = workspace_root / REVIEW_SESSIONS_DIRECTORY / session_id
    _verify_review_session(session_root, catalog=catalog, routing=routing)
    return EvidenceWorkspace(
        root=workspace_root,
        catalog_path=catalog.path,
        routing_path=routing.path,
        review_queue_path=session_root / REVIEW_QUEUE_NAME,
        ready=False,
    )


def validate_review_submission(workspace: EvidenceWorkspace, submission: Path) -> None:
    """Fail closed until a separate, schema-valid reviewed-coverage publisher exists."""
    if not isinstance(workspace, EvidenceWorkspace) or workspace.ready:
        raise ValueError("official evidence workspace is invalid")
    if submission.absolute() != workspace.review_queue_path.absolute():
        raise ValueError("official review submission is not bound to this workspace")
    _assert_safe_regular_file(submission, label="official review queue")
    _assert_safe_directory(workspace.root, label="official evidence workspace")
    raise ValueError(
        "official evidence review is not ready; raw documents cannot create execution coverage"
    )


def load_verified_review_queue(workspace: EvidenceWorkspace) -> VerifiedReviewQueue:
    """Reload a review queue only after rechecking its complete upstream lineage."""
    if not isinstance(workspace, EvidenceWorkspace) or workspace.ready:
        raise ValueError("official evidence workspace is invalid")
    root = workspace.review_queue_path.parent.absolute()
    expected_parent = workspace.root / REVIEW_SESSIONS_DIRECTORY
    if (
        workspace.review_queue_path.name != REVIEW_QUEUE_NAME
        or root.parent.absolute() != expected_parent.absolute()
        or _IMMUTABLE_ENTRY.fullmatch(root.name) is None
    ):
        raise ValueError("official evidence review queue path is not canonical")
    manifest_path = root / REVIEW_MANIFEST_NAME
    _assert_safe_regular_file(manifest_path, label="official evidence review manifest")
    manifest_bytes = manifest_path.read_bytes()
    manifest = _canonical_object(
        manifest_bytes,
        label="official evidence review manifest",
    )
    catalog_record = manifest.get("catalog")
    routing_record = manifest.get("routing")
    if not isinstance(catalog_record, dict) or not isinstance(routing_record, dict):
        raise ValueError("official evidence review lineage is invalid")
    destination = workspace.root.parent
    catalog_path = destination / str(catalog_record.get("relative_path", ""))
    routing_path = destination / str(routing_record.get("relative_path", ""))
    catalog = load_verified_announcement_catalog(catalog_path)
    routing = load_verified_announcement_routing(routing_path, catalog=catalog)
    _verify_review_session(root, catalog=catalog, routing=routing)
    queue = pl.read_parquet(workspace.review_queue_path)
    return VerifiedReviewQueue(
        frame=queue,
        manifest=manifest,
        manifest_path=manifest_path,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        session_id=root.name,
    )


def _validate_document_tree(
    files: dict[str, bytes],
    *,
    source_url: str,
    cache_id: str,
) -> CachedOfficialDocument:
    if set(files) != {DOCUMENT_NAME, DOCUMENT_MANIFEST_NAME}:
        raise ValueError("official document cache inventory is invalid")
    manifest = _canonical_object(
        files[DOCUMENT_MANIFEST_NAME],
        label="official document cache manifest",
    )
    payload = files[DOCUMENT_NAME]
    validated = validate_official_document(payload, source_url=source_url)
    expected = {
        "schema_version": _SCHEMA_VERSION,
        "role": "official_document_cache",
        "source_url": source_url,
        "cache_path": f"{DOCUMENTS_DIRECTORY}/{cache_id}/{DOCUMENT_NAME}",
        "sha256": validated.sha256,
        "size_bytes": validated.size_bytes,
        "media_type": validated.media_type,
        "page_count": validated.page_count,
    }
    if manifest != expected:
        raise ValueError("official document cache identity differs")
    return CachedOfficialDocument(
        source_url=source_url,
        cache_path=str(manifest["cache_path"]),
        sha256=str(manifest["sha256"]),
        size_bytes=int(manifest["size_bytes"]),
        page_count=int(manifest["page_count"]),
        media_type=str(manifest["media_type"]),
    )


def _validate_quarantine_tree(
    files: dict[str, bytes],
    *,
    source_url: str,
    quarantine_id: str,
) -> QuarantinedOfficialDocument:
    if set(files) != {QUARANTINE_RESPONSE_NAME, QUARANTINE_MANIFEST_NAME}:
        raise ValueError("official document quarantine inventory is invalid")
    payload = files[QUARANTINE_RESPONSE_NAME]
    manifest = _canonical_object(
        files[QUARANTINE_MANIFEST_NAME],
        label="official document quarantine manifest",
    )
    expected = {
        "schema_version": _QUARANTINE_SCHEMA_VERSION,
        "role": "official_document_quarantine",
        "source_url": source_url,
        "quarantine_path": (
            f"{QUARANTINE_DIRECTORY}/{quarantine_id}/{QUARANTINE_RESPONSE_NAME}"
        ),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "reason": manifest.get("reason"),
    }
    if (
        manifest != expected
        or not isinstance(manifest.get("reason"), str)
        or not manifest["reason"]
    ):
        raise ValueError("official document quarantine identity differs")
    return QuarantinedOfficialDocument(
        source_url=source_url,
        quarantine_path=str(manifest["quarantine_path"]),
        sha256=str(manifest["sha256"]),
        size_bytes=int(manifest["size_bytes"]),
        reason=str(manifest["reason"]),
    )


def _review_rows(
    catalog: pl.DataFrame,
    routing: pl.DataFrame,
    cached_documents: Mapping[str, CachedOfficialDocument],
    quarantined_documents: Mapping[str, QuarantinedOfficialDocument],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    selected = (
        catalog.join(routing, on="catalog_id", how="inner")
        .filter(pl.col("route") != "excluded")
        .sort("catalog_id")
    )
    for row in selected.iter_rows(named=True):
        cached = cached_documents.get(row["source_url"])
        quarantined = quarantined_documents.get(row["source_url"])
        if cached is not None and quarantined is not None:
            raise ValueError("official document cannot be both cached and quarantined")
        rows.append(
            {
                "catalog_id": row["catalog_id"],
                "announcement_id": row["announcement_id"],
                "announcement_title": row["announcement_title"],
                "source_url": row["source_url"],
                "source": row["source"],
                "symbol": row["symbol"],
                "market": row["market"],
                "category": row["category"],
                "route": row["route"],
                "candidate_type": row["candidate_type"],
                "routing_reason": row["reason"],
                "source_response": row["source_response"],
                "document_cache_path": cached.cache_path if cached else "",
                "document_sha256": cached.sha256 if cached else "",
                "document_size_bytes": cached.size_bytes if cached else 0,
                "document_page_count": cached.page_count if cached else 0,
                "document_media_type": cached.media_type if cached else "",
                "document_quarantine_path": (
                    quarantined.quarantine_path if quarantined else ""
                ),
                "document_validation_reason": (
                    quarantined.reason if quarantined else ""
                ),
                "document_status": (
                    "cached"
                    if cached
                    else "quarantined"
                    if quarantined
                    else "not_fetched"
                ),
                "review_status": "needs_review",
            }
        )
    return rows


def _verify_review_session(
    root: Path,
    *,
    catalog: VerifiedAnnouncementCatalog,
    routing: VerifiedAnnouncementRouting,
) -> None:
    _assert_safe_directory(root, label="official evidence review session")
    with os.scandir(root) as entries:
        names = {entry.name for entry in entries}
    if names != {REVIEW_QUEUE_NAME, REVIEW_MANIFEST_NAME}:
        raise ValueError("official evidence review session inventory is invalid")
    queue_path = root / REVIEW_QUEUE_NAME
    manifest_path = root / REVIEW_MANIFEST_NAME
    _assert_safe_regular_file(queue_path, label="official evidence review queue")
    _assert_safe_regular_file(manifest_path, label="official evidence review manifest")
    queue_bytes = queue_path.read_bytes()
    manifest = _canonical_object(
        manifest_path.read_bytes(),
        label="official evidence review manifest",
    )
    if root.name != _expected_review_session_id(manifest):
        raise ValueError("official evidence review session identity differs")
    queue_record = manifest.get("review_queue")
    catalog_record = manifest.get("catalog")
    routing_record = manifest.get("routing")
    schema_version = manifest.get("schema_version")
    if (
        schema_version not in {_SCHEMA_VERSION, _RENDITION_SCHEMA_VERSION}
        or manifest.get("role") != "official_evidence_review_workspace"
        or manifest.get("ready") is not False
        or manifest.get("execution_coverage_published") is not False
        or not isinstance(queue_record, dict)
        or queue_record.get("path") != REVIEW_QUEUE_NAME
        or queue_record.get("sha256") != hashlib.sha256(queue_bytes).hexdigest()
        or queue_record.get("size_bytes") != len(queue_bytes)
        or not isinstance(catalog_record, dict)
        or catalog_record.get("sha256") != catalog.catalog_sha256
        or catalog_record.get("relative_path") != _catalog_relative_path(catalog.path)
        or not isinstance(routing_record, dict)
        or routing_record.get("sha256") != routing.routing_sha256
        or routing_record.get("relative_path")
        != f"{ROUTING_DIRECTORY}/{ROUTING_NAME}"
    ):
        raise ValueError("official evidence review session identity differs")
    if schema_version == _RENDITION_SCHEMA_VERSION:
        _verify_review_rendition_binding(
            root,
            manifest,
            queue_bytes=queue_bytes,
            catalog=catalog,
            routing=routing,
        )
    try:
        queue = pl.read_parquet(BytesIO(queue_bytes))
    except pl.exceptions.PolarsError as error:
        raise ValueError("official evidence review queue is invalid") from error
    if tuple(queue.columns) != _QUEUE_COLUMNS or queue.height != queue_record.get("row_count"):
        raise ValueError("official evidence review queue schema differs")
    if queue.select((pl.col("review_status") != "needs_review").any()).item():
        raise ValueError("official evidence review queue is unexpectedly ready")


def _verify_review_rendition_binding(
    session_root: Path,
    manifest: dict[str, object],
    *,
    queue_bytes: bytes,
    catalog: VerifiedAnnouncementCatalog,
    routing: VerifiedAnnouncementRouting,
) -> None:
    base = manifest.get("base_review_manifest")
    renditions = manifest.get("candidate_review_renditions")
    if (
        not isinstance(base, dict)
        or not isinstance(renditions, dict)
        or not isinstance(base.get("relative_path"), str)
        or not isinstance(base.get("sha256"), str)
        or not isinstance(renditions.get("relative_path"), str)
        or not isinstance(renditions.get("manifest_sha256"), str)
        or not isinstance(renditions.get("record_count"), int)
        or renditions["record_count"] <= 0
    ):
        raise ValueError("official evidence review rendition binding is invalid")
    destination = session_root.parents[2]
    base_relative = Path(str(base["relative_path"]))
    rendition_relative = Path(str(renditions["relative_path"]))
    if (
        base_relative.is_absolute()
        or len(base_relative.parts) != 4
        or base_relative.parts[:2]
        != ("official_document_workspace", REVIEW_SESSIONS_DIRECTORY)
        or _IMMUTABLE_ENTRY.fullmatch(base_relative.parts[2]) is None
        or base_relative.parts[3] != REVIEW_MANIFEST_NAME
        or rendition_relative.is_absolute()
        or rendition_relative.parts
        != (
            "official_candidate_review_renditions",
            str(renditions["manifest_sha256"]),
            "rendition_manifest.json",
        )
    ):
        raise ValueError(
            "official evidence review rendition path is not canonical"
        )
    base_path = destination / base_relative
    rendition_path = destination / rendition_relative
    _assert_safe_regular_file(
        base_path,
        label="base official evidence review manifest",
    )
    _assert_safe_regular_file(
        rendition_path,
        label="candidate review rendition manifest",
    )
    base_bytes = base_path.read_bytes()
    rendition_bytes = rendition_path.read_bytes()
    base_manifest = _canonical_object(
        base_bytes,
        label="base official evidence review manifest",
    )
    base_queue_record = base_manifest.get("review_queue")
    if (
        base_manifest.get("schema_version") != _SCHEMA_VERSION
        or _expected_review_session_id(base_manifest)
        != base_relative.parts[2]
        or hashlib.sha256(base_bytes).hexdigest() != base["sha256"]
        or not isinstance(base_queue_record, dict)
        or base.get("queue_sha256") != base_queue_record.get("sha256")
        or hashlib.sha256(rendition_bytes).hexdigest()
        != renditions["manifest_sha256"]
    ):
        raise ValueError("official evidence review rendition binding changed")
    try:
        _verify_review_session(
            base_path.parent,
            catalog=catalog,
            routing=routing,
        )
    except ValueError as error:
        raise ValueError(
            "base official evidence review session is invalid"
        ) from error
    base_queue_bytes = base_path.with_name(REVIEW_QUEUE_NAME).read_bytes()
    if queue_bytes != base_queue_bytes:
        raise ValueError("candidate rendition base review queue bytes differ")
    inherited_fields = (
        "role",
        "catalog",
        "routing",
        "documents",
        "quarantined_documents",
        "review_queue",
        "ready",
        "execution_coverage_published",
    )
    if any(manifest.get(key) != base_manifest.get(key) for key in inherited_fields):
        raise ValueError("candidate rendition base review manifest fields differ")


def _expected_review_session_id(manifest: dict[str, object]) -> str:
    schema_version = manifest.get("schema_version")
    if schema_version == _SCHEMA_VERSION:
        identity = {
            "catalog_sha256": (
                manifest.get("catalog", {}).get("sha256")
                if isinstance(manifest.get("catalog"), dict)
                else None
            ),
            "routing_sha256": (
                manifest.get("routing", {}).get("sha256")
                if isinstance(manifest.get("routing"), dict)
                else None
            ),
            "documents": manifest.get("documents"),
            "quarantined_documents": manifest.get("quarantined_documents"),
        }
    elif schema_version == _RENDITION_SCHEMA_VERSION:
        base = manifest.get("base_review_manifest")
        renditions = manifest.get("candidate_review_renditions")
        if not isinstance(base, dict) or not isinstance(renditions, dict):
            raise ValueError("official evidence review session identity differs")
        identity = {
            "base_review_manifest_sha256": base.get("sha256"),
            "base_review_queue_sha256": base.get("queue_sha256"),
            "rendition_manifest_sha256": renditions.get(
                "manifest_sha256"
            ),
        }
    else:
        raise ValueError("official evidence review session identity differs")
    return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


def _catalog_relative_path(catalog_path: Path) -> str:
    if catalog_path.name != "catalog.parquet" or catalog_path.parent.name != "official_announcement_catalog":
        raise ValueError("official evidence catalog path is not canonical")
    return "official_announcement_catalog/catalog.parquet"


def _cache_id(source_url: str) -> str:
    if not isinstance(source_url, str) or not source_url:
        raise ValueError("official document source URL is invalid")
    return hashlib.sha256(source_url.encode("utf-8")).hexdigest()


def _parquet_bytes(frame: pl.DataFrame) -> bytes:
    stream = BytesIO()
    frame.write_parquet(stream, compression="zstd")
    return stream.getvalue()


def _canonical_object(raw: bytes, *, label: str) -> dict[str, object]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is invalid") from error
    if not isinstance(payload, dict) or raw != canonical_json_bytes(payload):
        raise ValueError(f"{label} is invalid")
    return payload


def _assert_safe_directory(path: Path, *, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label} is missing or unsafe")
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise ValueError(f"{label} uses a symlink")


def _assert_safe_regular_file(path: Path, *, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is missing or unsafe")
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise ValueError(f"{label} uses a symlink")


def _open_or_create_directory(parent_fd: int, name: str, *, label: str) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    return open_directory_at(parent_fd, name, label=label)


def _assert_immutable_directory_inventory(directory_fd: int, *, label: str) -> None:
    for name in os.listdir(directory_fd):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            _IMMUTABLE_ENTRY.fullmatch(name) is None
            or stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            raise ValueError(f"{label} inventory is unsafe")
