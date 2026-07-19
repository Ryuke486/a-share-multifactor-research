"""Pagination, staging, and immutable publication for official query packages."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from ashare_multifactor.audit.secure_tree import (
    atomic_rename_no_replace_at,
    write_frozen_tree_at,
)
from ashare_multifactor.final_test.official_query_client import (
    OfficialQueryTransport,
    RetryPolicy,
    fetch_with_retry,
)
from ashare_multifactor.final_test.official_query_collection_root import (
    entry_exists,
)
from ashare_multifactor.final_test.official_query_coverage import (
    OFFICIAL_QUERY_ENDPOINT,
    OfficialQueryScope,
    VerifiedOfficialQueryPackage,
    canonical_json_bytes,
    page_request_sha256,
    validate_official_query_package,
)
from ashare_multifactor.final_test.official_query_index import (
    canonical_package_path,
    validate_official_query_coverage_index,
)
from ashare_multifactor.final_test.recovery_secure_fs import (
    open_directory_at,
    read_bytes_at,
    write_bytes_exclusive_at,
)


INDEX_NAME = "official_query_coverage.json"
_SCHEMA_VERSION = "1"
_PAGE_SIZE = 30
_MAX_PAGES_PER_SCOPE = 1_000


@dataclass(frozen=True)
class CollectionProgress:
    """The completed part of an exact, immutable query scope set."""

    completed_scopes: int
    index_path: Path | None


def collect_coverage(
    *,
    coverage_fd: int,
    coverage_root: Path,
    scopes: tuple[OfficialQueryScope, ...],
    transport: OfficialQueryTransport,
    policy: RetryPolicy,
    max_scopes: int | None,
    created_at: str,
) -> CollectionProgress:
    """Publish complete packages and atomically add an index only at completion."""
    index_path = coverage_root / INDEX_NAME
    if entry_exists(coverage_fd, INDEX_NAME):
        validated = validate_official_query_coverage_index(
            index_path,
            expected_scopes=scopes,
        )
        if len(validated.packages) != len(scopes):
            raise ValueError("official query coverage index is incomplete")
        return CollectionProgress(len(scopes), index_path)

    completed = 0
    for scope in scopes:
        package_root = coverage_root / package_relative_path(scope)
        if package_root.exists() or package_root.is_symlink():
            validate_official_query_package(package_root, expected_scope=scope)
            completed += 1
            continue
        if max_scopes is not None and completed >= max_scopes:
            break
        publish_package(
            coverage_fd=coverage_fd,
            coverage_root=coverage_root,
            scope=scope,
            transport=transport,
            policy=policy,
            created_at=created_at,
        )
        completed += 1

    if completed != len(scopes):
        if entry_exists(coverage_fd, INDEX_NAME):
            raise ValueError("official query coverage index appeared before completion")
        return CollectionProgress(completed, None)
    publish_index(coverage_fd=coverage_fd, coverage_root=coverage_root, scopes=scopes)
    validate_official_query_coverage_index(index_path, expected_scopes=scopes)
    return CollectionProgress(completed, index_path)


def publish_package(
    *,
    coverage_fd: int,
    coverage_root: Path,
    scope: OfficialQueryScope,
    transport: OfficialQueryTransport,
    policy: RetryPolicy,
    created_at: str,
) -> VerifiedOfficialQueryPackage:
    """Publish one package from complete response bytes, never a partial package."""
    parent_fd, parent_root = _open_package_parent(coverage_fd, coverage_root, scope)
    try:
        if entry_exists(parent_fd, "query-package"):
            return validate_official_query_package(
                parent_root / "query-package",
                expected_scope=scope,
            )
        request = _request_payload(scope)
        staging_name = _staging_name(request)
        staging_root = parent_root / staging_name
        if entry_exists(parent_fd, staging_name):
            try:
                validate_official_query_package(staging_root, expected_scope=scope)
            except ValueError:
                pass
            else:
                _rename_staging(parent_fd, staging_name)
                return _validate_published_package(parent_root, scope)

        files = _package_files(
            request,
            transport=transport,
            policy=policy,
            created_at=created_at,
        )
        write_frozen_tree_at(
            parent_fd,
            staging_name,
            files,
            resumable=True,
            label="official query package staging",
        )
        validate_official_query_package(staging_root, expected_scope=scope)
        _rename_staging(parent_fd, staging_name)
        return _validate_published_package(parent_root, scope)
    finally:
        os.close(parent_fd)


def publish_index(
    *,
    coverage_fd: int,
    coverage_root: Path,
    scopes: tuple[OfficialQueryScope, ...],
) -> None:
    """Atomically publish one exact package index after full validation."""
    records = []
    for scope in scopes:
        package = validate_official_query_package(
            coverage_root / package_relative_path(scope),
            expected_scope=scope,
        )
        records.append(_index_record(scope, package))
    payload = canonical_json_bytes(
        {
            "schema_version": _SCHEMA_VERSION,
            "role": "official_query_coverage",
            "packages": records,
        }
    )
    if entry_exists(coverage_fd, INDEX_NAME):
        existing = read_bytes_at(
            coverage_fd,
            INDEX_NAME,
            label="official query coverage index",
        )
        if existing != payload:
            raise ValueError("official query coverage index identity differs")
        return
    temporary = f".{INDEX_NAME}.{uuid4().hex}.tmp"
    try:
        write_bytes_exclusive_at(coverage_fd, temporary, payload)
        atomic_rename_no_replace_at(coverage_fd, temporary, INDEX_NAME)
        os.fsync(coverage_fd)
    except FileExistsError:
        if not entry_exists(coverage_fd, INDEX_NAME):
            raise
        existing = read_bytes_at(
            coverage_fd,
            INDEX_NAME,
            label="official query coverage index",
        )
        if existing != payload:
            raise ValueError("official query coverage index identity differs") from None
    finally:
        try:
            os.unlink(temporary, dir_fd=coverage_fd)
        except FileNotFoundError:
            pass


def package_relative_path(scope: OfficialQueryScope) -> Path:
    """Return the index-defined package location as a filesystem path."""
    return Path(canonical_package_path(scope))


def _open_package_parent(
    coverage_fd: int,
    coverage_root: Path,
    scope: OfficialQueryScope,
) -> tuple[int, Path]:
    descriptors: list[int] = []
    path = coverage_root
    try:
        current = _open_or_create_directory(
            coverage_fd,
            "packages",
            label="official query packages",
        )
        descriptors.append(current)
        path /= "packages"
        for name in (scope.category, scope.market, scope.symbol):
            next_fd = _open_or_create_directory(
                current,
                name,
                label="official query package parent",
            )
            os.close(current)
            descriptors.pop()
            current = next_fd
            descriptors.append(current)
            path /= name
        descriptors.pop()
        return current, path
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _package_files(
    request: dict[str, object],
    *,
    transport: OfficialQueryTransport,
    policy: RetryPolicy,
    created_at: str,
) -> dict[str, bytes]:
    page_size = int(request["page_size"])
    first = _fetch_page(
        request,
        page=1,
        page_size=page_size,
        transport=transport,
        policy=policy,
    )
    total_pages, total_results, first_count, first_ids = _response_summary(first)
    if total_pages > _MAX_PAGES_PER_SCOPE:
        raise ValueError("official query page count exceeds the frozen collector bound")
    if total_results > 0 and total_pages == 0:
        raise ValueError("official query response declares results without pages")
    page_count = max(1, total_pages)
    page_payloads = [first]
    records = [
        _page_record(
            request,
            page=1,
            payload=first,
            total_pages=total_pages,
            total_results=total_results,
            result_count=first_count,
        )
    ]
    seen_ids = set(first_ids)
    for page in range(2, page_count + 1):
        payload = _fetch_page(
            request,
            page=page,
            page_size=page_size,
            transport=transport,
            policy=policy,
        )
        observed_pages, observed_results, result_count, identifiers = _response_summary(payload)
        if (observed_pages, observed_results) != (total_pages, total_results):
            raise ValueError("official query response totals differ between pages")
        if seen_ids.intersection(identifiers):
            raise ValueError("official query pagination repeats an announcement")
        seen_ids.update(identifiers)
        page_payloads.append(payload)
        records.append(
            _page_record(
                request,
                page=page,
                payload=payload,
                total_pages=total_pages,
                total_results=total_results,
                result_count=result_count,
            )
        )
    if sum(int(record["result_count"]) for record in records) != total_results:
        raise ValueError("official query response count differs from declared total")
    request_bytes = canonical_json_bytes(request)
    pages_bytes = canonical_json_bytes({"schema_version": _SCHEMA_VERSION, "pages": records})
    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
        "pages_sha256": hashlib.sha256(pages_bytes).hexdigest(),
        "total_pages": total_pages,
        "total_results": total_results,
        "page_count": page_count,
        "completed": True,
        "created_at": created_at,
    }
    return {
        "request.json": request_bytes,
        **{
            f"pages/page-{page:04d}.json": payload
            for page, payload in enumerate(page_payloads, start=1)
        },
        "pages.json": pages_bytes,
        "query_manifest.json": canonical_json_bytes(manifest),
    }


def _request_payload(scope: OfficialQueryScope) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "endpoint": OFFICIAL_QUERY_ENDPOINT,
        "method": "POST",
        "scope": {
            "symbol": scope.symbol,
            "market": scope.market,
            "category": scope.category,
            "query_category": scope.query_category,
            "start": scope.start.isoformat(),
            "end": scope.end.isoformat(),
        },
        "page_size": _PAGE_SIZE,
        "form": {
            "category": scope.query_category,
            "column": {"sh": "sse", "sz": "szse"}[scope.market],
            "plate": scope.market,
            "searchkey": "",
            "seDate": f"{scope.start.isoformat()}~{scope.end.isoformat()}",
            "stock": scope.symbol,
            "tabName": "fulltext",
            "trade": "",
        },
    }


def _fetch_page(
    request: dict[str, object],
    *,
    page: int,
    page_size: int,
    transport: OfficialQueryTransport,
    policy: RetryPolicy,
) -> bytes:
    form = request["form"]
    if not isinstance(form, dict):
        raise ValueError("official query request form is invalid")
    page_form = {str(key): str(value) for key, value in form.items()}
    page_form.update({"pageNum": str(page), "pageSize": str(page_size)})
    return fetch_with_retry(
        transport,
        endpoint=str(request["endpoint"]),
        form=page_form,
        policy=policy,
    )


def _response_summary(payload: bytes) -> tuple[int, int, int, tuple[str, ...]]:
    try:
        decoded = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("official query response is invalid") from error
    if not isinstance(decoded, dict):
        raise ValueError("official query response is invalid")
    total_pages = decoded.get("totalpages")
    total_results = decoded.get("totalAnnouncement")
    announcements = decoded.get("announcements")
    if not _nonnegative_int(total_pages) or not _nonnegative_int(total_results):
        raise ValueError("official query response totals are invalid")
    if announcements is None and total_results == 0:
        announcements = []
    if not isinstance(announcements, list):
        raise ValueError("official query response announcements are invalid")
    identifiers: list[str] = []
    for item in announcements:
        if not isinstance(item, dict):
            raise ValueError("official query announcement is invalid")
        identifier = item.get("announcementId")
        if not isinstance(identifier, (str, int)) or isinstance(identifier, bool):
            raise ValueError("official query announcement identifier is invalid")
        normalized = str(identifier)
        if not normalized:
            raise ValueError("official query announcement identifier is invalid")
        identifiers.append(normalized)
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("official query page repeats an announcement")
    return total_pages, total_results, len(announcements), tuple(identifiers)


def _page_record(
    request: dict[str, object],
    *,
    page: int,
    payload: bytes,
    total_pages: int,
    total_results: int,
    result_count: int,
) -> dict[str, object]:
    return {
        "page": page,
        "cache_file": f"pages/page-{page:04d}.json",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "request_sha256": page_request_sha256(request, page),
        "total_pages": total_pages,
        "total_results": total_results,
        "result_count": result_count,
    }


def _index_record(
    scope: OfficialQueryScope,
    package: VerifiedOfficialQueryPackage,
) -> dict[str, object]:
    return {
        "symbol": scope.symbol,
        "market": scope.market,
        "category": scope.category,
        "query_category": scope.query_category,
        "relative_path": package_relative_path(scope).as_posix(),
        "request_sha256": package.request_sha256,
        "pages_sha256": package.pages_sha256,
        "manifest_sha256": package.manifest_sha256,
        "total_pages": package.total_pages,
        "total_results": package.total_results,
    }


def _validate_published_package(
    parent_root: Path,
    scope: OfficialQueryScope,
) -> VerifiedOfficialQueryPackage:
    return validate_official_query_package(
        parent_root / "query-package",
        expected_scope=scope,
    )


def _rename_staging(parent_fd: int, staging_name: str) -> None:
    try:
        atomic_rename_no_replace_at(parent_fd, staging_name, "query-package")
    except FileExistsError:
        if not entry_exists(parent_fd, "query-package"):
            raise
    os.fsync(parent_fd)


def _staging_name(request: Mapping[str, object]) -> str:
    digest = hashlib.sha256(canonical_json_bytes(dict(request))).hexdigest()
    return f".query-package-{digest}.staging"


def _open_or_create_directory(parent_fd: int, name: str, *, label: str) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    return open_directory_at(parent_fd, name, label=label)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("official query response has duplicate keys")
        value[key] = item
    return value


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
