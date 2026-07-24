"""Validate immutable CNInfo announcement-query evidence packages."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterator

from ashare_multifactor.data.security import market_for_symbol
from ashare_multifactor.final_test.incident_snapshot import (
    copied_directory_snapshot,
    working_directory_at,
)
from ashare_multifactor.final_test.recovery_secure_fs import (
    assert_directory_entry,
    directory_identity,
    open_directory_at,
    opened_directory,
)


OFFICIAL_QUERY_ENDPOINT = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
_SCHEMA_VERSION = "1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CATEGORY = re.compile(r"[a-z][a-z0-9_]{0,63}")
_REQUEST_FIELDS = frozenset({"schema_version", "endpoint", "method", "scope", "page_size", "form"})
_SCOPE_FIELDS = frozenset(
    {"symbol", "market", "category", "query_category", "start", "end", "org_id"}
)
_PAGE_FIELDS = frozenset(
    {
        "page",
        "cache_file",
        "sha256",
        "size_bytes",
        "request_sha256",
        "total_pages",
        "total_results",
        "result_count",
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "request_sha256",
        "pages_sha256",
        "total_pages",
        "total_results",
        "page_count",
        "completed",
        "created_at",
    }
)
_QUERY_FORM_FIELDS = frozenset(
    {
        "category",
        "column",
        "plate",
        "searchkey",
        "seDate",
        "stock",
        "tabName",
        "trade",
    }
)


@dataclass(frozen=True)
class OfficialQueryScope:
    """One official query's security, category, and time boundaries."""

    symbol: str
    market: str
    category: str
    query_category: str
    start: date
    end: date
    org_id: str | None = None


@dataclass(frozen=True)
class VerifiedOfficialQueryPackage:
    """Verified immutable identity for one cached official query."""

    scope: OfficialQueryScope
    request_sha256: str
    pages_sha256: str
    manifest_sha256: str
    total_pages: int
    total_results: int
    page_count: int
    announcement_ids: tuple[str, ...]


def canonical_json_bytes(payload: object) -> bytes:
    """Return the sole canonical encoding permitted for local package metadata."""
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def page_request_sha256(request: dict[str, Any], page: int) -> str:
    """Hash the exact page-specific POST body derived from one base request."""
    form = request.get("form")
    page_size = request.get("page_size")
    endpoint = request.get("endpoint")
    method = request.get("method")
    if (
        not isinstance(form, dict)
        or not _positive_int(page)
        or not _positive_int(page_size)
        or not isinstance(endpoint, str)
        or not isinstance(method, str)
    ):
        raise ValueError("official query page request is invalid")
    page_form = dict(form)
    page_form.update({"pageNum": str(page), "pageSize": str(page_size)})
    return hashlib.sha256(
        canonical_json_bytes({"endpoint": endpoint, "method": method, "form": page_form})
    ).hexdigest()


def validate_official_query_package(
    root: Path,
    *,
    expected_scope: OfficialQueryScope | None = None,
) -> VerifiedOfficialQueryPackage:
    """Fail closed unless one complete official POST query package is intact."""
    package_root = _package_root(root)
    with _opened_anchored_absolute_directory(
        package_root.parent,
        label="official query package parent",
    ) as (parent_fd, parent_chain):
        source_fd = open_directory_at(
            parent_fd,
            package_root.name,
            label="official query package root",
        )
        try:
            source_identity = directory_identity(source_fd)
            with copied_directory_snapshot(
                source_fd,
                parent_fd,
                label="official query package",
            ) as snapshot:
                with working_directory_at(snapshot.descriptor):
                    verified = _validate_package_tree(
                        Path("."),
                        expected_scope=expected_scope,
                    )
            assert_directory_entry(
                parent_fd,
                package_root.name,
                expected=source_identity,
                label="official query package root",
            )
            if _directory_path_identity(package_root.parent) != parent_chain:
                raise ValueError("official query package path identity changed")
            return verified
        finally:
            os.close(source_fd)


def _validate_package_tree(
    package_root: Path,
    *,
    expected_scope: OfficialQueryScope | None,
) -> VerifiedOfficialQueryPackage:
    _assert_exact_entries(
        package_root,
        {"request.json", "pages.json", "query_manifest.json", "pages"},
        label="official query package",
    )
    request, request_bytes = _load_canonical_json(
        package_root / "request.json",
        label="official query request",
    )
    scope = _validate_request(request)
    if expected_scope is not None and not _scope_matches(scope, expected_scope):
        raise ValueError("official query scope differs from expected scope")

    pages_payload, pages_bytes = _load_canonical_json(
        package_root / "pages.json",
        label="official query pages",
    )
    manifest, manifest_bytes = _load_canonical_json(
        package_root / "query_manifest.json",
        label="official query manifest",
    )
    totals = _validate_manifest(
        manifest,
        request=request,
        request_bytes=request_bytes,
        pages_bytes=pages_bytes,
    )
    announcement_ids = _validate_pages(
        package_root,
        pages_payload,
        request=request,
        total_pages=totals[0],
        total_results=totals[1],
        page_count=totals[2],
    )
    return VerifiedOfficialQueryPackage(
        scope=scope,
        request_sha256=hashlib.sha256(request_bytes).hexdigest(),
        pages_sha256=hashlib.sha256(pages_bytes).hexdigest(),
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        total_pages=totals[0],
        total_results=totals[1],
        page_count=totals[2],
        announcement_ids=announcement_ids,
    )


def _package_root(root: Path) -> Path:
    absolute = (root if root.is_absolute() else Path.cwd() / root).absolute()
    if absolute.is_symlink() or not absolute.is_dir():
        raise ValueError("official query package root is unsafe")
    for ancestor in (absolute, *absolute.parents):
        if ancestor.is_symlink():
            raise ValueError("official query package root uses a symlink")
    return absolute


def _directory_path_identity(path: Path) -> tuple[tuple[int, int], ...]:
    with _opened_anchored_absolute_directory(
        path,
        label="official query package path",
    ) as (_, identities):
        return identities


@contextmanager
def _opened_anchored_absolute_directory(
    path: Path,
    *,
    label: str,
) -> Iterator[tuple[int, tuple[tuple[int, int], ...]]]:
    """Open an absolute directory once through no-follow descriptor hops."""
    if not path.is_absolute():
        raise ValueError("official query package path is not absolute")
    anchor = Path(path.anchor)
    with opened_directory(anchor, label=f"{label} root") as anchor_fd:
        descriptor = os.dup(anchor_fd)
        identities = [directory_identity(descriptor)]
        try:
            for part in path.parts[1:]:
                child_fd = open_directory_at(
                    descriptor,
                    part,
                    label=label,
                )
                os.close(descriptor)
                descriptor = child_fd
                identities.append(directory_identity(descriptor))
            yield descriptor, tuple(identities)
        finally:
            os.close(descriptor)


def _load_canonical_json(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    raw = _read_regular_file(path, label=label)
    payload = _decode_json(raw, label=label)
    canonical = canonical_json_bytes(payload)
    if raw != canonical:
        raise ValueError(f"{label} is not canonically encoded")
    return payload, raw


def _decode_json(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{label} is invalid") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{label} is invalid")
    return payload


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _validate_request(payload: dict[str, Any]) -> OfficialQueryScope:
    if set(payload) != _REQUEST_FIELDS:
        raise ValueError("official query request schema is invalid")
    if payload.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("official query request schema is invalid")
    if payload.get("endpoint") != OFFICIAL_QUERY_ENDPOINT:
        raise ValueError("official query endpoint is invalid")
    if payload.get("method") != "POST":
        raise ValueError("official query method is invalid")
    if not _positive_int(payload.get("page_size")):
        raise ValueError("official query page size is invalid")
    form = payload.get("form")
    if (
        not isinstance(form, dict)
        or set(form) != _QUERY_FORM_FIELDS
        or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in form.items()
        )
    ):
        raise ValueError("official query form is invalid")
    scope = payload.get("scope")
    if not isinstance(scope, dict) or set(scope) != _SCOPE_FIELDS:
        raise ValueError("official query scope is invalid")
    symbol = scope.get("symbol")
    market = scope.get("market")
    category = scope.get("category")
    query_category = scope.get("query_category")
    org_id = scope.get("org_id")
    start = _parse_date(scope.get("start"), label="official query scope")
    end = _parse_date(scope.get("end"), label="official query scope")
    if (
        not isinstance(symbol, str)
        or not isinstance(market, str)
        or not isinstance(category, str)
        or not isinstance(query_category, str)
        or not isinstance(org_id, str)
        or not _valid_org_id(org_id)
        or _CATEGORY.fullmatch(category) is None
        or "\n" in query_category
        or start > end
    ):
        raise ValueError("official query scope is invalid")
    try:
        actual_market = market_for_symbol(symbol)
    except ValueError as error:
        raise ValueError("official query scope symbol is invalid") from error
    if market not in {"sh", "sz"} or actual_market != market:
        raise ValueError("official query scope market is invalid")
    if form.get("stock") != f"{symbol},{org_id}":
        raise ValueError("official query form symbol differs from query scope")
    if form.get("column") != {"sh": "sse", "sz": "szse"}[market]:
        raise ValueError("official query form market differs from query scope")
    if form.get("plate") != market:
        raise ValueError("official query form market differs from query scope")
    if form.get("seDate") != f"{start.isoformat()}~{end.isoformat()}":
        raise ValueError("official query form date differs from query scope")
    if form.get("category") != query_category:
        raise ValueError("official query form category differs from query scope")
    if form.get("tabName") != "fulltext" or form.get("searchkey") != "" or form.get("trade") != "":
        raise ValueError("official query form is invalid")
    return OfficialQueryScope(
        symbol=symbol,
        market=market,
        category=category,
        query_category=query_category,
        start=start,
        end=end,
        org_id=org_id,
    )


def _scope_matches(
    actual: OfficialQueryScope,
    expected: OfficialQueryScope,
) -> bool:
    """Allow legacy readers to omit orgId, never package writers or collectors."""
    return (
        actual.symbol == expected.symbol
        and actual.market == expected.market
        and actual.category == expected.category
        and actual.query_category == expected.query_category
        and actual.start == expected.start
        and actual.end == expected.end
        and (expected.org_id is None or actual.org_id == expected.org_id)
    )


def _valid_org_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value))


def _validate_manifest(
    payload: dict[str, Any],
    *,
    request: dict[str, Any],
    request_bytes: bytes,
    pages_bytes: bytes,
) -> tuple[int, int, int]:
    if set(payload) != _MANIFEST_FIELDS or payload.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("official query manifest schema is invalid")
    if payload.get("completed") is not True:
        raise ValueError("official query pagination is incomplete")
    created_at = payload.get("created_at")
    if not isinstance(created_at, str):
        raise ValueError("official query manifest creation time is invalid")
    try:
        parsed_created_at = datetime.fromisoformat(created_at)
    except ValueError as error:
        raise ValueError("official query manifest creation time is invalid") from error
    if parsed_created_at.tzinfo is None:
        raise ValueError("official query manifest creation time is invalid")
    if (
        payload.get("request_sha256") != hashlib.sha256(request_bytes).hexdigest()
        or payload.get("pages_sha256") != hashlib.sha256(pages_bytes).hexdigest()
    ):
        raise ValueError("official query manifest hash differs from package")
    totals = (
        payload.get("total_pages"),
        payload.get("total_results"),
        payload.get("page_count"),
    )
    if not all(_nonnegative_int(value) for value in totals):
        raise ValueError("official query manifest totals are invalid")
    total_pages, total_results, page_count = totals
    page_size = request.get("page_size")
    if not _positive_int(page_size):
        raise ValueError("official query page size is invalid")
    expected_count = max(
        1,
        total_pages,
        (total_results + page_size - 1) // page_size,
    )
    if page_count != expected_count:
        raise ValueError("official query pagination is incomplete")
    return total_pages, total_results, page_count


def _validate_pages(
    root: Path,
    payload: dict[str, Any],
    *,
    request: dict[str, Any],
    total_pages: int,
    total_results: int,
    page_count: int,
) -> tuple[str, ...]:
    if (
        set(payload) != {"schema_version", "pages"}
        or payload.get("schema_version") != _SCHEMA_VERSION
    ):
        raise ValueError("official query pages schema is invalid")
    records = payload.get("pages")
    if not isinstance(records, list) or len(records) != page_count:
        raise ValueError("official query pagination is incomplete")
    expected_pages = list(range(1, page_count + 1))
    result_counts: list[int] = []
    cache_names: set[str] = set()
    announcement_ids: set[str] = set()
    ordered_announcement_ids: list[str] = []
    for expected_page, record in zip(expected_pages, records, strict=True):
        if not isinstance(record, dict) or set(record) != _PAGE_FIELDS:
            raise ValueError("official query page record is invalid")
        if record.get("page") != expected_page:
            raise ValueError("official query page sequence is incomplete or duplicated")
        cache_file = record.get("cache_file")
        if not isinstance(cache_file, str) or cache_file in cache_names:
            raise ValueError("official query cache path is invalid")
        expected_cache = f"pages/page-{expected_page:04d}.json"
        if cache_file != expected_cache:
            raise ValueError("official query cache path is not canonical")
        cache_names.add(cache_file)
        if (
            not isinstance(record.get("sha256"), str)
            or _SHA256.fullmatch(record["sha256"]) is None
            or not _nonnegative_int(record.get("size_bytes"))
            or not isinstance(record.get("request_sha256"), str)
            or _SHA256.fullmatch(record["request_sha256"]) is None
            or record.get("total_pages") != total_pages
            or record.get("total_results") != total_results
            or not _nonnegative_int(record.get("result_count"))
        ):
            raise ValueError("official query page metadata is invalid")
        if record["request_sha256"] != page_request_sha256(request, expected_page):
            raise ValueError("official query page request differs from base request")
        response_path = root / PurePosixPath(cache_file)
        response_bytes = _read_regular_file(response_path, label="official query response")
        if (
            len(response_bytes) != record["size_bytes"]
            or hashlib.sha256(response_bytes).hexdigest() != record["sha256"]
        ):
            raise ValueError("official query response hash differs from page record")
        response_totals, response_count, response_ids = _response_summary(response_bytes)
        if (
            response_totals != (total_pages, total_results)
            or response_count != record["result_count"]
        ):
            raise ValueError("official query response totals differ from page record")
        if announcement_ids.intersection(response_ids):
            raise ValueError("official query pagination has duplicate announcements")
        announcement_ids.update(response_ids)
        ordered_announcement_ids.extend(response_ids)
        result_counts.append(response_count)
    if sum(result_counts) != total_results:
        raise ValueError("official query response count differs from declared total")
    pages_root = root / "pages"
    _assert_exact_entries(
        pages_root,
        {PurePosixPath(cache_name).name for cache_name in cache_names},
        label="official query page cache",
    )
    return tuple(ordered_announcement_ids)


def _response_summary(
    response_bytes: bytes,
) -> tuple[tuple[int, int], int, tuple[str, ...]]:
    payload = _decode_json(response_bytes, label="official query response")
    total_pages = payload.get("totalpages")
    total_results = payload.get("totalAnnouncement")
    announcements = payload.get("announcements")
    if not _nonnegative_int(total_pages) or not _nonnegative_int(total_results):
        raise ValueError("official query response schema is invalid")
    if announcements is None and total_results == 0:
        announcements = []
    if not isinstance(announcements, list):
        raise ValueError("official query response schema is invalid")
    identifiers = []
    for announcement in announcements:
        if not isinstance(announcement, dict):
            raise ValueError("official query announcement is invalid")
        identifier = announcement.get("announcementId")
        if not isinstance(identifier, (str, int)) or isinstance(identifier, bool):
            raise ValueError("official query announcement identifier is invalid")
        normalized = str(identifier)
        if not normalized:
            raise ValueError("official query announcement identifier is invalid")
        identifiers.append(normalized)
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("official query page has duplicate announcements")
    return (total_pages, total_results), len(announcements), tuple(identifiers)


def _read_regular_file(path: Path, *, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is missing or unsafe")
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError(f"{label} uses a symlink")
    try:
        return path.read_bytes()
    except OSError as error:
        raise ValueError(f"{label} is unreadable") from error


def _assert_exact_entries(root: Path, expected: set[str], *, label: str) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"{label} is missing or unsafe")
    actual = {entry.name for entry in root.iterdir()}
    if actual != expected:
        raise ValueError(f"{label} entries are incomplete or unexpected")


def _parse_date(value: object, *, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{label} date is invalid")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{label} date is invalid") from error


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
