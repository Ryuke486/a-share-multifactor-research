"""Read validated official-query pages into provenance-only announcement rows."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from ashare_multifactor.final_test.action_source_contract import (
    FinalActionSourceContract,
    assert_allowed_evidence_url,
    evidence_url_matches_source,
)
from ashare_multifactor.final_test.official_query_collection_root import (
    opened_safe_directory,
)
from ashare_multifactor.final_test.official_query_coverage import (
    OfficialQueryScope,
    VerifiedOfficialQueryPackage,
    canonical_json_bytes,
    validate_official_query_package,
)
from ashare_multifactor.final_test.official_query_index import canonical_package_path
from ashare_multifactor.final_test.recovery_secure_fs import (
    open_directory_at,
    read_bytes_at,
)


def catalog_rows_from_packages(
    coverage_root: Path,
    *,
    scopes: tuple[OfficialQueryScope, ...],
    contract: FinalActionSourceContract,
) -> list[dict[str, object]]:
    """Read only package bytes whose scope and page identities were validated."""
    rows: list[dict[str, object]] = []
    sources = dict(contract.market_sources)
    for scope in scopes:
        package_root = coverage_root / PurePosixPath(canonical_package_path(scope))
        package = _verified_package_pages(package_root, scope)
        source = sources[scope.market]
        for page in package["pages"]:
            rows.extend(
                _page_rows(
                    page,
                    scope=scope,
                    source=source,
                    contract=contract,
                    package=package,
                )
            )
    return rows


def _verified_package_pages(
    package_root: Path,
    scope: OfficialQueryScope,
) -> dict[str, object]:
    package = validate_official_query_package(package_root, expected_scope=scope)
    with opened_safe_directory(package_root, label="official query package") as package_fd:
        pages_bytes = read_bytes_at(
            package_fd,
            "pages.json",
            label="official query pages",
        )
        if hashlib.sha256(pages_bytes).hexdigest() != package.pages_sha256:
            raise ValueError("official query pages identity differs from package")
        pages_payload = _canonical_object(pages_bytes, label="official query pages")
        page_records = pages_payload.get("pages")
        if (
            pages_payload.get("schema_version") != "1"
            or not isinstance(page_records, list)
        ):
            raise ValueError("official query pages are invalid")
        pages_fd = open_directory_at(
            package_fd,
            "pages",
            label="official query page cache",
        )
        try:
            pages = tuple(
                _read_page_record(pages_fd, record, package=package)
                for record in page_records
            )
        finally:
            os.close(pages_fd)
    if len(pages) != max(1, package.total_pages):
        raise ValueError("official query page cache count is invalid")
    if validate_official_query_package(package_root, expected_scope=scope) != package:
        raise ValueError("official query package changed while building catalog")
    return {
        "request_sha256": package.request_sha256,
        "pages_sha256": package.pages_sha256,
        "manifest_sha256": package.manifest_sha256,
        "pages": pages,
    }


def _read_page_record(
    pages_fd: int,
    record: object,
    *,
    package: VerifiedOfficialQueryPackage,
) -> dict[str, object]:
    if not isinstance(record, dict):
        raise ValueError("official query page record is invalid")
    cache_file = record.get("cache_file")
    page = record.get("page")
    if (
        not isinstance(page, int)
        or isinstance(page, bool)
        or page <= 0
        or not isinstance(cache_file, str)
        or cache_file != f"pages/page-{page:04d}.json"
    ):
        raise ValueError("official query page record path is invalid")
    raw = read_bytes_at(
        pages_fd,
        Path(cache_file).name,
        label="official query page response",
    )
    if (
        len(raw) != record.get("size_bytes")
        or hashlib.sha256(raw).hexdigest() != record.get("sha256")
    ):
        raise ValueError("official query page response identity differs")
    payload = _json_object(raw, label="official query page response")
    announcements = payload.get("announcements")
    if announcements is None and payload.get("totalAnnouncement") == 0:
        announcements = []
    if not isinstance(announcements, list):
        raise ValueError("official query page announcements are invalid")
    expected_count = record.get("result_count")
    if not isinstance(expected_count, int) or len(announcements) != expected_count:
        raise ValueError("official query page announcement count differs")
    return {
        "request_sha256": package.request_sha256,
        "pages_sha256": package.pages_sha256,
        "manifest_sha256": package.manifest_sha256,
        "cache_file": cache_file,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "announcements": announcements,
    }


def _page_rows(
    page: dict[str, object],
    *,
    scope: OfficialQueryScope,
    source: str,
    contract: FinalActionSourceContract,
    package: dict[str, object],
) -> list[dict[str, object]]:
    announcements = page["announcements"]
    if not isinstance(announcements, list):
        raise ValueError("official query page announcements are invalid")
    rows: list[dict[str, object]] = []
    for ordinal, announcement in enumerate(announcements, start=1):
        if not isinstance(announcement, dict):
            raise ValueError("official query announcement is invalid")
        identifier = announcement.get("announcementId")
        if not isinstance(identifier, (str, int)) or isinstance(identifier, bool):
            raise ValueError("official query announcement identifier is invalid")
        announcement_id = str(identifier)
        if not announcement_id:
            raise ValueError("official query announcement identifier is invalid")
        raw_url = announcement.get("adjunctUrl")
        if not isinstance(raw_url, str) or not raw_url.strip():
            raise ValueError("official query announcement lacks an official document URL")
        source_url = _official_url(raw_url, contract)
        if not evidence_url_matches_source(source, source_url):
            raise ValueError("official query announcement URL differs from market source")
        title = announcement.get("announcementTitle")
        if title is not None and not isinstance(title, str):
            raise ValueError("official query announcement title is invalid")
        catalog_id = hashlib.sha256(
            canonical_json_bytes(
                {
                    "scope": {
                        "symbol": scope.symbol,
                        "market": scope.market,
                        "category": scope.category,
                        "query_category": scope.query_category,
                    },
                    "announcement_id": announcement_id,
                    "source_response": page["cache_file"],
                    "ordinal": ordinal,
                }
            )
        ).hexdigest()
        rows.append(
            {
                "catalog_id": catalog_id,
                "announcement_id": announcement_id,
                "announcement_title": title or "",
                "announcement_time_raw": json.dumps(
                    announcement.get("announcementTime"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                "source_url": source_url,
                "source": source,
                "symbol": scope.symbol,
                "market": scope.market,
                "category": scope.category,
                "query_category": scope.query_category,
                "source_response": (
                    f"{canonical_package_path(scope)}/{page['cache_file']}"
                ),
                "source_response_sha256": page["sha256"],
                "source_response_size_bytes": page["size_bytes"],
                "package_request_sha256": package["request_sha256"],
                "package_pages_sha256": package["pages_sha256"],
                "package_manifest_sha256": package["manifest_sha256"],
            }
        )
    return rows


def _official_url(raw_url: str, contract: FinalActionSourceContract) -> str:
    candidate = raw_url.strip()
    if candidate.startswith("finalpage/"):
        candidate = f"https://static.cninfo.com.cn/{candidate}"
    parsed = urlsplit(candidate)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or ".." in Path(parsed.path).parts
    ):
        raise ValueError("invalid official evidence URL")
    assert_allowed_evidence_url(candidate, contract)
    return candidate


def _canonical_object(raw: bytes, *, label: str) -> dict[str, object]:
    payload = _json_object(raw, label=label)
    if raw != canonical_json_bytes(payload):
        raise ValueError(f"{label} is not canonically encoded")
    return payload


def _json_object(raw: bytes, *, label: str) -> dict[str, object]:
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{label} is invalid") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{label} is invalid")
    return payload


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("official query response has duplicate keys")
        payload[key] = value
    return payload
