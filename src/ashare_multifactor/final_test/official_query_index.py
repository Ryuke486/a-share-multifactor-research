"""Bind complete CNInfo query packages to one exact final-test scope."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
from typing import Any, Iterable

from ashare_multifactor.final_test.official_query_coverage import (
    OfficialQueryScope,
    VerifiedOfficialQueryPackage,
    canonical_json_bytes,
    validate_official_query_package,
)


_LEGACY_SCHEMA_VERSION = "1"
_SCHEMA_VERSION = "2"
_INDEX_FIELDS = frozenset({"schema_version", "role", "packages"})
_LEGACY_PACKAGE_FIELDS = frozenset(
    {
        "symbol",
        "market",
        "org_id",
        "category",
        "query_category",
        "relative_path",
        "request_sha256",
        "pages_sha256",
        "manifest_sha256",
        "total_pages",
        "total_results",
    }
)
_PACKAGE_FIELDS = _LEGACY_PACKAGE_FIELDS | frozenset({"start", "end"})
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class VerifiedOfficialQueryCoverageIndex:
    """Immutable identities for every expected official query package."""

    index_sha256: str
    packages: tuple[VerifiedOfficialQueryPackage, ...]


def official_query_coverage_schema_version(path: Path) -> str:
    """Return a canonical index version for controlled legacy migration."""
    index_path = _safe_index_path(path)
    payload = _decode_index(_read_canonical_index(index_path))
    version = payload.get("schema_version")
    if version not in {_LEGACY_SCHEMA_VERSION, _SCHEMA_VERSION}:
        raise ValueError("official query coverage index schema is invalid")
    return str(version)


def validate_official_query_coverage_index(
    path: Path,
    *,
    expected_scopes: Iterable[OfficialQueryScope],
) -> VerifiedOfficialQueryCoverageIndex:
    """Fail closed unless the index has exactly one valid package per scope."""
    index_path = _safe_index_path(path)
    raw = _read_canonical_index(index_path)
    payload = _decode_index(raw)
    scopes = _normalize_expected_scopes(expected_scopes)
    records = _validate_index_schema(payload)
    root = index_path.parent
    if payload["schema_version"] == _LEGACY_SCHEMA_VERSION:
        if len(records) != len(scopes):
            raise ValueError("official query coverage package scope is not exact")
        verified = tuple(
            _validate_legacy_record(root, record, scope)
            for record, scope in zip(records, scopes, strict=True)
        )
    else:
        verified = _validate_partitioned_records(root, records, scopes)
    return VerifiedOfficialQueryCoverageIndex(
        index_sha256=hashlib.sha256(raw).hexdigest(),
        packages=verified,
    )


def copy_validated_official_query_coverage(
    source_index: Path,
    *,
    expected_scopes: Iterable[OfficialQueryScope],
    destination_root: Path,
) -> None:
    """Copy one complete coverage index and its exact validated packages."""
    source = _safe_index_path(source_index)
    scopes = _normalize_expected_scopes(expected_scopes)
    schema_version = official_query_coverage_schema_version(source)
    verified = validate_official_query_coverage_index(
        source,
        expected_scopes=scopes,
    )
    destination_root.mkdir(parents=True, exist_ok=True)
    destination_index = destination_root / source.name
    if destination_index.exists() or destination_index.is_symlink():
        raise FileExistsError("official query coverage destination index already exists")
    shutil.copy2(source, destination_index)
    for package in verified.packages:
        scope = package.scope
        relative = PurePosixPath(
            _legacy_package_path(scope)
            if schema_version == _LEGACY_SCHEMA_VERSION
            else canonical_package_path(scope)
        )
        source_package = source.parent / relative
        destination_package = destination_root / relative
        if destination_package.exists() or destination_package.is_symlink():
            raise FileExistsError("official query coverage destination package already exists")
        destination_package.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_package, destination_package)
    validate_official_query_coverage_index(source, expected_scopes=scopes)
    validate_official_query_coverage_index(
        destination_index,
        expected_scopes=scopes,
    )


def _safe_index_path(path: Path) -> Path:
    absolute = (path if path.is_absolute() else Path.cwd() / path).absolute()
    if absolute.name != "official_query_coverage.json":
        raise ValueError("official query coverage index path is not canonical")
    for ancestor in (absolute, *absolute.parents):
        if ancestor.is_symlink():
            raise ValueError("official query coverage index uses a symlink")
    if not absolute.is_file():
        raise ValueError("official query coverage index is missing")
    return absolute


def _read_canonical_index(path: Path) -> bytes:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ValueError("official query coverage index is unreadable") from error
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("official query coverage index is invalid") from error
    if not isinstance(payload, dict) or raw != canonical_json_bytes(payload):
        raise ValueError("official query coverage index is not canonically encoded")
    return raw


def _decode_index(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("official query coverage index is invalid") from error
    if not isinstance(payload, dict):
        raise ValueError("official query coverage index is invalid")
    return payload


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _normalize_expected_scopes(
    expected_scopes: Iterable[OfficialQueryScope],
) -> tuple[OfficialQueryScope, ...]:
    scopes = tuple(sorted(expected_scopes, key=_scope_key))
    if not scopes or any(not isinstance(scope, OfficialQueryScope) for scope in scopes):
        raise ValueError("official query coverage expected scopes are invalid")
    if len({_scope_key(scope) for scope in scopes}) != len(scopes):
        raise ValueError("official query coverage expected scopes are duplicated")
    return scopes


def _validate_index_schema(
    payload: dict[str, Any],
) -> tuple[dict[str, object], ...]:
    if (
        set(payload) != _INDEX_FIELDS
        or payload.get("schema_version")
        not in {_LEGACY_SCHEMA_VERSION, _SCHEMA_VERSION}
        or payload.get("role") != "official_query_coverage"
        or not isinstance(payload.get("packages"), list)
    ):
        raise ValueError("official query coverage index schema is invalid")
    records = tuple(payload["packages"])
    fields = (
        _LEGACY_PACKAGE_FIELDS
        if payload["schema_version"] == _LEGACY_SCHEMA_VERSION
        else _PACKAGE_FIELDS
    )
    if not records or any(
        not isinstance(record, dict) or set(record) != fields
        for record in records
    ):
        raise ValueError("official query coverage package record is invalid")
    return records


def _validate_legacy_record(
    root: Path,
    record: dict[str, object],
    scope: OfficialQueryScope,
) -> VerifiedOfficialQueryPackage:
    if _record_scope_key(record) != _scope_key(scope):
        raise ValueError("official query coverage package scope is incomplete or differs")
    relative_path = record.get("relative_path")
    expected_path = _legacy_package_path(scope)
    if relative_path != expected_path:
        raise ValueError("official query coverage package path is not canonical")
    package = validate_official_query_package(
        root / PurePosixPath(expected_path),
        expected_scope=scope,
    )
    if (
        not isinstance(record.get("org_id"), str)
        or record["org_id"] != package.scope.org_id
        or (scope.org_id is not None and record["org_id"] != scope.org_id)
    ):
        raise ValueError("official query coverage package orgId differs from scope")
    if any(
        record.get(field) != value
        for field, value in (
            ("request_sha256", package.request_sha256),
            ("pages_sha256", package.pages_sha256),
            ("manifest_sha256", package.manifest_sha256),
            ("total_pages", package.total_pages),
            ("total_results", package.total_results),
        )
    ):
        raise ValueError("official query coverage package identity differs from index")
    if any(
        not isinstance(record.get(field), str)
        or _SHA256.fullmatch(str(record[field])) is None
        for field in ("request_sha256", "pages_sha256", "manifest_sha256")
    ):
        raise ValueError("official query coverage package hash is invalid")
    return package


def _validate_partitioned_records(
    root: Path,
    records: tuple[dict[str, object], ...],
    scopes: tuple[OfficialQueryScope, ...],
) -> tuple[VerifiedOfficialQueryPackage, ...]:
    expected = {_base_scope_key(scope): scope for scope in scopes}
    if len(expected) != len(scopes):
        raise ValueError("official query coverage expected scopes are duplicated")
    grouped: dict[
        tuple[str, str, str, str],
        list[VerifiedOfficialQueryPackage],
    ] = {key: [] for key in expected}
    verified: list[VerifiedOfficialQueryPackage] = []
    previous_key: tuple[str, str, str, str, date, date] | None = None
    for record in records:
        leaf = _record_scope(record)
        base_key = _base_scope_key(leaf)
        full = expected.get(base_key)
        if full is None or leaf.start < full.start or leaf.end > full.end:
            raise ValueError(
                "official query coverage package scope is incomplete or differs"
            )
        if full.org_id is not None and leaf.org_id != full.org_id:
            raise ValueError("official query coverage package orgId differs from scope")
        record_key = (*base_key, leaf.start, leaf.end)
        if previous_key is not None and record_key <= previous_key:
            raise ValueError("official query coverage packages are not uniquely ordered")
        previous_key = record_key
        package = _validate_record_for_scope(root, record, leaf)
        grouped[base_key].append(package)
        verified.append(package)
    for key, full in expected.items():
        packages = grouped[key]
        if not packages:
            raise ValueError("official query coverage package scope is not exact")
        cursor = full.start
        announcement_ids: set[str] = set()
        for package in packages:
            if package.scope.start != cursor:
                raise ValueError(
                    "official query coverage time partition has a gap or overlap"
                )
            if announcement_ids.intersection(package.announcement_ids):
                raise ValueError(
                    "official query coverage has a duplicate announcement across slices"
                )
            announcement_ids.update(package.announcement_ids)
            cursor = package.scope.end + timedelta(days=1)
        if cursor != full.end + timedelta(days=1):
            raise ValueError("official query coverage time partition is incomplete")
    return tuple(verified)


def _record_scope(record: dict[str, object]) -> OfficialQueryScope:
    values = (
        record.get("symbol"),
        record.get("market"),
        record.get("category"),
        record.get("query_category"),
        record.get("org_id"),
    )
    if not all(isinstance(value, str) for value in values):
        raise ValueError("official query coverage package scope is invalid")
    try:
        start = date.fromisoformat(str(record.get("start")))
        end = date.fromisoformat(str(record.get("end")))
    except ValueError as error:
        raise ValueError("official query coverage package date is invalid") from error
    if start > end:
        raise ValueError("official query coverage package date is invalid")
    symbol, market, category, query_category, org_id = values
    return OfficialQueryScope(
        symbol=symbol,
        market=market,
        category=category,
        query_category=query_category,
        start=start,
        end=end,
        org_id=org_id,
    )


def _validate_record_for_scope(
    root: Path,
    record: dict[str, object],
    scope: OfficialQueryScope,
) -> VerifiedOfficialQueryPackage:
    expected_path = canonical_package_path(scope)
    if record.get("relative_path") != expected_path:
        raise ValueError("official query coverage package path is not canonical")
    package = validate_official_query_package(
        root / PurePosixPath(expected_path),
        expected_scope=scope,
    )
    if any(
        record.get(field) != value
        for field, value in (
            ("org_id", package.scope.org_id),
            ("request_sha256", package.request_sha256),
            ("pages_sha256", package.pages_sha256),
            ("manifest_sha256", package.manifest_sha256),
            ("total_pages", package.total_pages),
            ("total_results", package.total_results),
        )
    ):
        raise ValueError("official query coverage package identity differs from index")
    if any(
        not isinstance(record.get(field), str)
        or _SHA256.fullmatch(str(record[field])) is None
        for field in ("request_sha256", "pages_sha256", "manifest_sha256")
    ):
        raise ValueError("official query coverage package hash is invalid")
    return package


def _record_scope_key(record: dict[str, object]) -> tuple[str, str, str, str]:
    values = (
        record.get("category"),
        record.get("market"),
        record.get("symbol"),
        record.get("query_category"),
    )
    if not all(isinstance(value, str) for value in values):
        raise ValueError("official query coverage package scope is invalid")
    category, market, symbol, query_category = values
    return category, market, symbol, query_category


def _scope_key(scope: OfficialQueryScope) -> tuple[str, str, str, str]:
    return scope.category, scope.market, scope.symbol, scope.query_category


def _base_scope_key(scope: OfficialQueryScope) -> tuple[str, str, str, str]:
    return scope.category, scope.market, scope.symbol, scope.query_category


def canonical_package_path(scope: OfficialQueryScope) -> str:
    """Return the only permitted relative package location for one scope."""
    period = f"{scope.start.isoformat()}_{scope.end.isoformat()}"
    return (
        f"packages/{scope.category}/{scope.market}/{scope.symbol}/"
        f"{period}/query-package"
    )


def _legacy_package_path(scope: OfficialQueryScope) -> str:
    return f"packages/{scope.category}/{scope.market}/{scope.symbol}/query-package"
