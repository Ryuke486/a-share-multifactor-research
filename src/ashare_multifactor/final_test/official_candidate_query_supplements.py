"""Immutable candidate-scoped query packages for announcement catalog closure."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import stat

import polars as pl

from ashare_multifactor.audit.secure_tree import (
    read_frozen_tree_at,
    write_frozen_tree_at,
)
from ashare_multifactor.final_test.action_source_contract import (
    FinalActionSourceContract,
)
from ashare_multifactor.final_test.official_announcement_catalog_schema import (
    SUPPLEMENT_CATALOG_NAME,
    SUPPLEMENT_MANIFEST_NAME,
    SUPPLEMENTS_DIRECTORY,
    announcement_catalog_frame,
    catalog_parquet_bytes,
    merge_candidate_query_supplement_rows,
    supplement_inventory,
)
from ashare_multifactor.final_test.official_candidate_query_supplement_snapshot import (
    load_verified_candidate_query_supplement_snapshot,
)
from ashare_multifactor.final_test.official_announcement_pages import (
    catalog_rows_from_packages,
)
from ashare_multifactor.final_test.official_candidate_query_authorization import (
    load_candidate_query_network_authorization,
)
from ashare_multifactor.final_test.official_query_client import (
    OfficialQueryTransport,
    RetryPolicy,
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
from ashare_multifactor.final_test.official_query_packages import (
    package_relative_path,
    publish_package,
)


__all__ = ["merge_candidate_query_supplement_rows"]

_SCHEMA_VERSION = "1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FROZEN_EXTERNAL_OBJECTS = frozenset(
    {
        "candidate_snapshot",
        "basis_candidate_snapshot",
        "blocked_admission",
        "causal_topology",
        "base_review_queue",
    }
)


@dataclass(frozen=True)
class CandidateQuerySupplementPackage:
    """One standard package selected by an already-verified request identity."""

    request_id: str
    package_path: Path


@dataclass(frozen=True)
class VerifiedCandidateQuerySupplement:
    """A content-addressed set of verified query packages and derived rows."""

    root: Path
    manifest_path: Path
    manifest_sha256: str
    manifest: dict[str, object]
    catalog: pl.DataFrame
    packages: tuple[VerifiedOfficialQueryPackage, ...]
    inventory_sha256: str
    inventory_file_count: int
    inventory_size_bytes: int


def collect_authorized_candidate_query_package(
    *,
    destination: Path,
    candidate_manifest_path: Path,
    blocked_admission_path: Path,
    network_authorization_path: Path,
    request_id: str,
    coverage_fd: int,
    coverage_root: Path,
    transport: OfficialQueryTransport,
    policy: RetryPolicy,
    created_at: str,
    contract: FinalActionSourceContract,
) -> CandidateQuerySupplementPackage:
    """Validate immutable authorization before the standard writer may fetch."""
    authorization = load_candidate_query_network_authorization(
        destination=destination,
        candidate_manifest_path=candidate_manifest_path,
        blocked_admission_path=blocked_admission_path,
        network_authorization_path=network_authorization_path,
        contract=contract,
        require_catalog_absent=True,
    )
    request = authorization.request(request_id)
    if coverage_root.absolute() != authorization.coverage_root:
        raise ValueError("candidate query authorization coverage root differs")
    descriptor_stat = os.fstat(coverage_fd)
    root_stat = os.stat(coverage_root, follow_symlinks=False)
    if (
        not stat.S_ISDIR(descriptor_stat.st_mode)
        or not stat.S_ISDIR(root_stat.st_mode)
        or (descriptor_stat.st_dev, descriptor_stat.st_ino)
        != (root_stat.st_dev, root_stat.st_ino)
    ):
        raise ValueError(
            "candidate query coverage root descriptor differs"
        )
    if policy.attempts > authorization.max_attempts_per_request:
        raise ValueError("candidate query authorization attempt limit differs")
    package = publish_package(
        coverage_fd=coverage_fd,
        coverage_root=coverage_root,
        scope=request.scope,
        transport=transport,
        policy=policy,
        created_at=created_at,
    )
    if package.request_sha256 != request.request_sha256:
        raise ValueError("candidate query authorized package identity differs")
    return CandidateQuerySupplementPackage(
        request_id=request.request_id,
        package_path=coverage_root / package_relative_path(request.scope),
    )


def publish_candidate_query_supplement(
    *,
    destination: Path,
    candidate_manifest_path: Path,
    blocked_admission_path: Path,
    network_authorization_path: Path,
    packages: tuple[CandidateQuerySupplementPackage, ...],
    contract: FinalActionSourceContract,
) -> VerifiedCandidateQuerySupplement:
    """Freeze only rows derived from standard, revalidated query packages."""
    if not packages:
        raise ValueError("candidate query supplement package set is empty")
    authorization = load_candidate_query_network_authorization(
        destination=destination,
        candidate_manifest_path=candidate_manifest_path,
        blocked_admission_path=blocked_admission_path,
        network_authorization_path=network_authorization_path,
        contract=contract,
        require_catalog_absent=True,
    )
    by_request_id = {
        request.request_id: request
        for request in authorization.requests
    }
    if (
        len({item.request_id for item in packages}) != len(packages)
        or set(by_request_id) != {item.request_id for item in packages}
    ):
        raise ValueError(
            "candidate query supplement packages differ from authorization"
        )
    records: list[dict[str, object]] = []
    files: dict[str, bytes] = {}
    rows: list[dict[str, object]] = []
    seen_scopes: set[tuple[object, ...]] = set()
    for item in sorted(packages, key=lambda value: value.request_id):
        request = by_request_id[item.request_id]
        scope_key = _scope_key(request.scope)
        if scope_key in seen_scopes:
            raise ValueError("candidate query supplement scope is invalid")
        seen_scopes.add(scope_key)
        verified = validate_official_query_package(
            item.package_path,
            expected_scope=request.scope,
        )
        if verified.request_sha256 != request.request_sha256:
            raise ValueError(
                "candidate query supplement package authorization differs"
            )
        coverage_root = _coverage_root(item.package_path, request.scope)
        package_rows = catalog_rows_from_packages(
            coverage_root,
            packages=(verified,),
            contract=contract,
        )
        for row in package_rows:
            row["source_response"] = (
                f"candidate_query_supplement/packages/{row['source_response']}"
            )
        rows.extend(package_rows)
        relative = package_relative_path(request.scope).as_posix()
        package_files = _read_package_tree(item.package_path)
        for name, payload in package_files.items():
            files[f"packages/{relative}/{name}"] = payload
        records.append(
            {
                "request_id": request.request_id,
                "scope": _scope_record(request.scope),
                "candidate_ids": list(request.candidate_ids),
                "relative_path": f"packages/{relative}",
                "request_sha256": verified.request_sha256,
                "pages_sha256": verified.pages_sha256,
                "manifest_sha256": verified.manifest_sha256,
                "total_pages": verified.total_pages,
                "total_results": verified.total_results,
                "page_count": verified.page_count,
            }
        )
    catalog = announcement_catalog_frame(rows)
    catalog_bytes = catalog_parquet_bytes(catalog)
    files[SUPPLEMENT_CATALOG_NAME] = catalog_bytes
    bindings = _standalone_replay_bindings(authorization.bindings)
    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "role": "official_candidate_query_supplement",
        "attempt_id": destination.name,
        "bindings": bindings,
        "packages": records,
        "package_count": len(records),
        "derived_row_count": catalog.height,
        "catalog": {
            "path": SUPPLEMENT_CATALOG_NAME,
            "sha256": hashlib.sha256(catalog_bytes).hexdigest(),
            "size_bytes": len(catalog_bytes),
            "row_count": catalog.height,
        },
        "inventory": supplement_inventory(files),
        "final_test_strategy_outputs_read": False,
    }
    manifest_bytes = canonical_json_bytes(manifest)
    supplement_id = hashlib.sha256(manifest_bytes).hexdigest()
    files[SUPPLEMENT_MANIFEST_NAME] = manifest_bytes
    with opened_safe_directory(
        destination,
        label="official evidence destination",
    ) as root_fd:
        try:
            os.mkdir(SUPPLEMENTS_DIRECTORY, mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        supplements_fd = os.open(
            SUPPLEMENTS_DIRECTORY,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        try:
            write_frozen_tree_at(
                supplements_fd,
                supplement_id,
                files,
                resumable=True,
                label="candidate query supplement",
            )
        finally:
            os.close(supplements_fd)
    return load_verified_candidate_query_supplement(
        destination
        / SUPPLEMENTS_DIRECTORY
        / supplement_id
        / SUPPLEMENT_MANIFEST_NAME,
        contract=contract,
    )


def load_verified_candidate_query_supplement(
    manifest_path: Path,
    *,
    contract: FinalActionSourceContract,
) -> VerifiedCandidateQuerySupplement:
    """Reload every binding, package byte, and derived catalog row."""
    absolute = manifest_path.absolute()
    root = absolute.parent
    if (
        absolute.name != SUPPLEMENT_MANIFEST_NAME
        or root.parent.name != SUPPLEMENTS_DIRECTORY
        or _SHA256.fullmatch(root.name) is None
    ):
        raise ValueError("candidate query supplement path is invalid")
    snapshot = load_verified_candidate_query_supplement_snapshot(
        root.parent.parent,
        manifest_sha256=root.name,
    )
    files = snapshot.files
    manifest = snapshot.manifest
    records = manifest.get("packages")
    catalog_record = manifest.get("catalog")
    if (
        manifest.get("schema_version") != _SCHEMA_VERSION
        or manifest.get("role") != "official_candidate_query_supplement"
        or manifest.get("attempt_id") != root.parent.parent.name
        or manifest.get("final_test_strategy_outputs_read") is not False
        or not isinstance(records, list)
        or not records
        or manifest.get("package_count") != len(records)
        or not isinstance(catalog_record, dict)
    ):
        raise ValueError("candidate query supplement manifest is invalid")
    authorization = _authorization_from_bindings(
        root.parent.parent,
        manifest.get("bindings"),
        contract=contract,
    )
    authorized = {
        request.request_id: request
        for request in authorization.requests
    }
    if {
        record.get("request_id")
        for record in records
        if isinstance(record, dict)
    } != set(authorized):
        raise ValueError(
            "candidate query supplement packages differ from authorization"
        )
    verified_packages: list[VerifiedOfficialQueryPackage] = []
    rows: list[dict[str, object]] = []
    expected_prefixes: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("candidate query supplement package is invalid")
        request = authorized.get(str(record.get("request_id", "")))
        if request is None:
            raise ValueError(
                "candidate query supplement package authorization differs"
            )
        scope = _scope_from_record(record)
        if (
            scope != request.scope
            or record.get("candidate_ids") != list(request.candidate_ids)
        ):
            raise ValueError(
                "candidate query supplement package authorization differs"
            )
        relative = f"packages/{package_relative_path(scope).as_posix()}"
        if record.get("relative_path") != relative:
            raise ValueError("candidate query supplement package path differs")
        package = validate_official_query_package(
            root / relative,
            expected_scope=scope,
        )
        if (
            record.get("request_sha256") != package.request_sha256
            or record.get("pages_sha256") != package.pages_sha256
            or record.get("manifest_sha256") != package.manifest_sha256
            or record.get("total_pages") != package.total_pages
            or record.get("total_results") != package.total_results
            or record.get("page_count") != package.page_count
        ):
            raise ValueError("candidate query supplement package identity differs")
        package_rows = catalog_rows_from_packages(
            root / "packages",
            packages=(package,),
            contract=contract,
        )
        for row in package_rows:
            row["source_response"] = (
                f"candidate_query_supplement/packages/{row['source_response']}"
            )
        rows.extend(package_rows)
        verified_packages.append(package)
        expected_prefixes.add(f"{relative}/")
    catalog = announcement_catalog_frame(rows)
    catalog_bytes = catalog_parquet_bytes(catalog)
    if (
        catalog_record.get("path") != SUPPLEMENT_CATALOG_NAME
        or catalog_record.get("sha256")
        != hashlib.sha256(catalog_bytes).hexdigest()
        or catalog_record.get("size_bytes") != len(catalog_bytes)
        or catalog_record.get("row_count") != catalog.height
        or manifest.get("derived_row_count") != catalog.height
        or files.get(SUPPLEMENT_CATALOG_NAME) != catalog_bytes
        or any(
            name not in {SUPPLEMENT_MANIFEST_NAME, SUPPLEMENT_CATALOG_NAME}
            and not any(name.startswith(prefix) for prefix in expected_prefixes)
            for name in files
        )
    ):
        raise ValueError("candidate query supplement catalog identity differs")
    return VerifiedCandidateQuerySupplement(
        root=root,
        manifest_path=absolute,
        manifest_sha256=root.name,
        manifest=manifest,
        catalog=catalog,
        packages=tuple(verified_packages),
        inventory_sha256=snapshot.inventory_sha256,
        inventory_file_count=snapshot.inventory_file_count,
        inventory_size_bytes=snapshot.inventory_size_bytes,
    )


def discover_candidate_query_supplement(
    destination: Path,
    *,
    contract: FinalActionSourceContract,
) -> VerifiedCandidateQuerySupplement | None:
    """Return the sole frozen supplement, or fail closed on ambiguous inventory."""
    root = destination / SUPPLEMENTS_DIRECTORY
    if not root.exists():
        return None
    if root.is_symlink() or not root.is_dir():
        raise ValueError("candidate query supplement root is unsafe")
    entries = sorted(root.iterdir())
    if len(entries) != 1 or _SHA256.fullmatch(entries[0].name) is None:
        raise ValueError("candidate query supplement inventory is ambiguous")
    return load_verified_candidate_query_supplement(
        entries[0] / SUPPLEMENT_MANIFEST_NAME,
        contract=contract,
    )


def _read_package_tree(path: Path) -> dict[str, bytes]:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return read_frozen_tree_at(descriptor, label="official query package")
    finally:
        os.close(descriptor)


def _coverage_root(path: Path, scope: OfficialQueryScope) -> Path:
    absolute = path.absolute()
    relative = package_relative_path(scope)
    root = absolute
    for _ in relative.parts:
        root = root.parent
    if root / relative != absolute:
        raise ValueError("candidate query supplement package path is not canonical")
    return root


def _scope_key(scope: OfficialQueryScope) -> tuple[object, ...]:
    return (
        scope.symbol,
        scope.market,
        scope.category,
        scope.query_category,
        scope.start,
        scope.end,
        scope.org_id,
    )


def _scope_record(scope: OfficialQueryScope) -> dict[str, object]:
    return {
        "symbol": scope.symbol,
        "market": scope.market,
        "category": scope.category,
        "query_category": scope.query_category,
        "start": scope.start.isoformat(),
        "end": scope.end.isoformat(),
        "org_id": scope.org_id,
    }


def _scope_from_record(record: object) -> OfficialQueryScope:
    if not isinstance(record, dict) or not isinstance(record.get("scope"), dict):
        raise ValueError("candidate query supplement scope is invalid")
    value = record["scope"]
    try:
        return OfficialQueryScope(
            symbol=str(value["symbol"]),
            market=str(value["market"]),
            category=str(value["category"]),
            query_category=str(value["query_category"]),
            start=date.fromisoformat(str(value["start"])),
            end=date.fromisoformat(str(value["end"])),
            org_id=str(value["org_id"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("candidate query supplement scope is invalid") from error


def _authorization_from_bindings(
    destination: Path,
    value: object,
    *,
    contract: FinalActionSourceContract,
):
    required = {
        "candidate_snapshot",
        "basis_candidate_snapshot",
        "base_coverage",
        "base_catalog",
        "base_query_collection",
        "base_routing",
        "base_date_rule",
        "blocked_admission",
        "blocked_unresolved",
        "network_authorization",
        "base_review_queue",
        "causal_topology",
    }
    if not isinstance(value, dict) or not required.issubset(value):
        raise ValueError("candidate query supplement binding is invalid")
    paths = {
        name: _binding_path(record)
        for name, record in value.items()
        if name in required
    }
    authorization = load_candidate_query_network_authorization(
        destination=destination,
        candidate_manifest_path=paths["candidate_snapshot"],
        blocked_admission_path=paths["blocked_admission"],
        network_authorization_path=paths["network_authorization"],
        contract=contract,
        require_catalog_absent=False,
    )
    if _standalone_replay_bindings(authorization.bindings) != value:
        raise ValueError("candidate query supplement binding changed")
    return authorization


def _standalone_replay_bindings(
    bindings: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    """Expand each frozen external object into deterministic leaf bindings."""
    expanded = dict(bindings)
    for name in sorted(_FROZEN_EXTERNAL_OBJECTS):
        record = bindings.get(name)
        path = _binding_path(record)
        descriptor = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        try:
            files = read_frozen_tree_at(
                descriptor,
                label=f"candidate query supplement {name}",
            )
        finally:
            os.close(descriptor)
        manifest_bytes = files.get(path.name)
        if (
            manifest_bytes is None
            or hashlib.sha256(manifest_bytes).hexdigest()
            != record["sha256"]
            or len(manifest_bytes) != record["size_bytes"]
        ):
            raise ValueError(
                "candidate query supplement external object differs"
            )
        for relative, payload in sorted(files.items()):
            if relative == path.name:
                continue
            expanded[f"tree:{name}:{relative}"] = {
                "path": str((path.parent / relative).absolute()),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
    return dict(sorted(expanded.items()))


def _binding_path(value: object) -> Path:
    if not isinstance(value, dict) or set(value) != {
        "path",
        "sha256",
        "size_bytes",
    }:
        raise ValueError("candidate query supplement binding is invalid")
    path = Path(str(value.get("path", "")))
    if path.is_symlink() or not path.is_file():
        raise ValueError("candidate query supplement binding is missing or unsafe")
    raw = path.read_bytes()
    if (
        value.get("sha256") != hashlib.sha256(raw).hexdigest()
        or value.get("size_bytes") != len(raw)
    ):
        raise ValueError("candidate query supplement binding changed")
    return path


def _canonical_object(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("candidate query supplement manifest is invalid") from error
    if not isinstance(value, dict) or raw != canonical_json_bytes(value):
        raise ValueError("candidate query supplement manifest is invalid")
    return value
