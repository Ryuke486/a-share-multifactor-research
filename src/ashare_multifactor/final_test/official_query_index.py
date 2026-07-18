"""Bind complete CNInfo query packages to one exact final-test scope."""

from __future__ import annotations

from dataclasses import dataclass
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


_SCHEMA_VERSION = "1"
_INDEX_FIELDS = frozenset({"schema_version", "role", "packages"})
_PACKAGE_FIELDS = frozenset(
    {
        "symbol",
        "market",
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
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class VerifiedOfficialQueryCoverageIndex:
    """Immutable identities for every expected official query package."""

    index_sha256: str
    packages: tuple[VerifiedOfficialQueryPackage, ...]


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
    records = _validate_index_schema(payload, expected_count=len(scopes))
    root = index_path.parent
    verified = tuple(
        _validate_record(
            root,
            record,
            scope,
        )
        for record, scope in zip(records, scopes, strict=True)
    )
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
    validate_official_query_coverage_index(source, expected_scopes=scopes)
    destination_root.mkdir(parents=True, exist_ok=True)
    destination_index = destination_root / source.name
    if destination_index.exists() or destination_index.is_symlink():
        raise FileExistsError("official query coverage destination index already exists")
    shutil.copy2(source, destination_index)
    for scope in scopes:
        relative = PurePosixPath(_canonical_package_path(scope))
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
    *,
    expected_count: int,
) -> tuple[dict[str, object], ...]:
    if (
        set(payload) != _INDEX_FIELDS
        or payload.get("schema_version") != _SCHEMA_VERSION
        or payload.get("role") != "official_query_coverage"
        or not isinstance(payload.get("packages"), list)
    ):
        raise ValueError("official query coverage index schema is invalid")
    if len(payload["packages"]) != expected_count:
        raise ValueError("official query coverage package scope is not exact")
    records = tuple(payload["packages"])
    if any(not isinstance(record, dict) or set(record) != _PACKAGE_FIELDS for record in records):
        raise ValueError("official query coverage package record is invalid")
    return records


def _validate_record(
    root: Path,
    record: dict[str, object],
    scope: OfficialQueryScope,
) -> VerifiedOfficialQueryPackage:
    if _record_scope_key(record) != _scope_key(scope):
        raise ValueError("official query coverage package scope is incomplete or differs")
    relative_path = record.get("relative_path")
    expected_path = _canonical_package_path(scope)
    if relative_path != expected_path:
        raise ValueError("official query coverage package path is not canonical")
    package = validate_official_query_package(
        root / PurePosixPath(expected_path),
        expected_scope=scope,
    )
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


def _canonical_package_path(scope: OfficialQueryScope) -> str:
    return f"packages/{scope.category}/{scope.market}/{scope.symbol}/query-package"
