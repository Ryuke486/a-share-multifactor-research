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
)
from ashare_multifactor.final_test.official_query_coverage import canonical_json_bytes
from ashare_multifactor.final_test.recovery_secure_fs import open_directory_at


WORKSPACE_DIRECTORY = "official_document_workspace"
DOCUMENTS_DIRECTORY = "documents"
REVIEW_SESSIONS_DIRECTORY = "review_sessions"
REVIEW_QUEUE_NAME = "review_queue.parquet"
REVIEW_MANIFEST_NAME = "review_manifest.json"
DOCUMENT_NAME = "document.bin"
DOCUMENT_MANIFEST_NAME = "document_manifest.json"

_SCHEMA_VERSION = "1"
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
    "source_response",
    "document_cache_path",
    "document_sha256",
    "document_size_bytes",
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
    "source_response": pl.String,
    "document_cache_path": pl.String,
    "document_sha256": pl.String,
    "document_size_bytes": pl.Int64,
    "document_status": pl.String,
    "review_status": pl.String,
}


@dataclass(frozen=True)
class EvidenceWorkspace:
    """Review-only artifacts; ``ready`` can never be inferred from raw documents."""

    root: Path
    catalog_path: Path
    review_queue_path: Path
    ready: bool


@dataclass(frozen=True)
class CachedOfficialDocument:
    """Exact bytes cached for one approved official document URL."""

    source_url: str
    cache_path: str
    sha256: str
    size_bytes: int


@contextmanager
def open_workspace(destination: Path) -> Iterator[tuple[Path, int, int, int]]:
    """Open or create the sole review workspace below a safe evidence root."""
    _assert_safe_directory(destination, label="official evidence destination")
    root_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    workspace_fd: int | None = None
    documents_fd: int | None = None
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
        sessions_fd = _open_or_create_directory(
            workspace_fd,
            REVIEW_SESSIONS_DIRECTORY,
            label="official review sessions",
        )
        if set(os.listdir(workspace_fd)) != {
            DOCUMENTS_DIRECTORY,
            REVIEW_SESSIONS_DIRECTORY,
        }:
            raise ValueError("official document workspace inventory is unsafe")
        _assert_immutable_directory_inventory(
            documents_fd,
            label="official document cache",
        )
        _assert_immutable_directory_inventory(
            sessions_fd,
            label="official review sessions",
        )
        yield destination / WORKSPACE_DIRECTORY, workspace_fd, documents_fd, sessions_fd
    finally:
        if sessions_fd is not None:
            os.close(sessions_fd)
        if documents_fd is not None:
            os.close(documents_fd)
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
    if not isinstance(payload, bytes) or not payload:
        raise ValueError("official document response is empty")
    cache_id = _cache_id(source_url)
    digest = hashlib.sha256(payload).hexdigest()
    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "role": "official_document_cache",
        "source_url": source_url,
        "cache_path": f"{DOCUMENTS_DIRECTORY}/{cache_id}/{DOCUMENT_NAME}",
        "sha256": digest,
        "size_bytes": len(payload),
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
    if cached is None or cached.sha256 != digest or cached.size_bytes != len(payload):
        raise ValueError("official document cache identity differs after publication")
    return cached


def publish_review_workspace(
    *,
    workspace_root: Path,
    sessions_fd: int,
    catalog: VerifiedAnnouncementCatalog,
    cached_documents: Mapping[str, CachedOfficialDocument],
) -> EvidenceWorkspace:
    """Publish one immutable review snapshot and deliberately leave it unready."""
    rows = _review_rows(catalog.frame, cached_documents)
    queue = pl.DataFrame(rows, schema=_QUEUE_SCHEMA).sort("catalog_id")
    queue_bytes = _parquet_bytes(queue)
    document_records = [
        {
            "source_url": item.source_url,
            "cache_path": item.cache_path,
            "sha256": item.sha256,
            "size_bytes": item.size_bytes,
        }
        for item in sorted(cached_documents.values(), key=lambda value: value.source_url)
    ]
    session_id = hashlib.sha256(
        canonical_json_bytes(
            {
                "catalog_sha256": catalog.catalog_sha256,
                "documents": document_records,
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
        "documents": document_records,
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
    _verify_review_session(session_root, catalog=catalog)
    return EvidenceWorkspace(
        root=workspace_root,
        catalog_path=catalog.path,
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
    expected = {
        "schema_version": _SCHEMA_VERSION,
        "role": "official_document_cache",
        "source_url": source_url,
        "cache_path": f"{DOCUMENTS_DIRECTORY}/{cache_id}/{DOCUMENT_NAME}",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }
    if manifest != expected:
        raise ValueError("official document cache identity differs")
    return CachedOfficialDocument(
        source_url=source_url,
        cache_path=str(manifest["cache_path"]),
        sha256=str(manifest["sha256"]),
        size_bytes=int(manifest["size_bytes"]),
    )


def _review_rows(
    catalog: pl.DataFrame,
    cached_documents: Mapping[str, CachedOfficialDocument],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for row in catalog.iter_rows(named=True):
        cached = cached_documents.get(row["source_url"])
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
                "source_response": row["source_response"],
                "document_cache_path": cached.cache_path if cached else "",
                "document_sha256": cached.sha256 if cached else "",
                "document_size_bytes": cached.size_bytes if cached else 0,
                "document_status": "cached" if cached else "not_fetched",
                "review_status": "needs_review",
            }
        )
    return rows


def _verify_review_session(
    root: Path,
    *,
    catalog: VerifiedAnnouncementCatalog,
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
    queue_record = manifest.get("review_queue")
    catalog_record = manifest.get("catalog")
    if (
        manifest.get("schema_version") != _SCHEMA_VERSION
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
    ):
        raise ValueError("official evidence review session identity differs")
    try:
        queue = pl.read_parquet(BytesIO(queue_bytes))
    except pl.exceptions.PolarsError as error:
        raise ValueError("official evidence review queue is invalid") from error
    if tuple(queue.columns) != _QUEUE_COLUMNS or queue.height != queue_record.get("row_count"):
        raise ValueError("official evidence review queue schema differs")
    if queue.select((pl.col("review_status") != "needs_review").any()).item():
        raise ValueError("official evidence review queue is unexpectedly ready")


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
