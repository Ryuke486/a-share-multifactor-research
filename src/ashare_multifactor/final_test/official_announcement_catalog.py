"""Create a provenance-only catalog from verified official query packages."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import polars as pl

from ashare_multifactor.audit.records import sha256_file
from ashare_multifactor.audit.secure_tree import (
    read_frozen_tree_at,
    write_frozen_tree_at,
)
from ashare_multifactor.final_test.action_source_contract import (
    OFFICIAL_EVIDENCE_URL_PREFIXES,
    OFFICIAL_MARKET_SOURCES,
    FinalActionSourceContract,
    evidence_url_matches_source,
)
from ashare_multifactor.final_test.gate import FinalTestAuthorization
from ashare_multifactor.final_test.official_query_collection_root import (
    IDENTITIES_DIRECTORY,
    opened_safe_directory,
)
from ashare_multifactor.final_test.official_query_collector import (
    expected_scopes,
    verify_official_query_collection_binding,
)
from ashare_multifactor.final_test.official_announcement_pages import (
    catalog_rows_from_packages,
)
from ashare_multifactor.final_test.official_query_coverage import canonical_json_bytes
from ashare_multifactor.final_test.official_query_index import (
    validate_official_query_coverage_index,
)
from ashare_multifactor.final_test.official_security_identity import (
    IDENTITY_INDEX_NAME,
    load_verified_cninfo_security_identities,
)
from ashare_multifactor.final_test.preparation import FinalTestPreparation
from ashare_multifactor.final_test.recovery_secure_fs import open_directory_at
from ashare_multifactor.final_test.resume import load_bound_symbol_scope


CATALOG_DIRECTORY = "official_announcement_catalog"
CATALOG_NAME = "catalog.parquet"
CATALOG_MANIFEST_NAME = "catalog_manifest.json"

_SCHEMA_VERSION = "1"
_CATALOG_COLUMNS = (
    "catalog_id",
    "announcement_id",
    "announcement_title",
    "announcement_time_raw",
    "source_url",
    "source",
    "symbol",
    "market",
    "category",
    "query_category",
    "source_response",
    "source_response_sha256",
    "source_response_size_bytes",
    "package_request_sha256",
    "package_pages_sha256",
    "package_manifest_sha256",
)
_CATALOG_SCHEMA = {
    "catalog_id": pl.String,
    "announcement_id": pl.String,
    "announcement_title": pl.String,
    "announcement_time_raw": pl.String,
    "source_url": pl.String,
    "source": pl.String,
    "symbol": pl.String,
    "market": pl.String,
    "category": pl.String,
    "query_category": pl.String,
    "source_response": pl.String,
    "source_response_sha256": pl.String,
    "source_response_size_bytes": pl.Int64,
    "package_request_sha256": pl.String,
    "package_pages_sha256": pl.String,
    "package_manifest_sha256": pl.String,
}


@dataclass(frozen=True)
class VerifiedAnnouncementCatalog:
    """A verified catalog that contains announcement provenance, never event facts."""

    path: Path
    root: Path
    frame: pl.DataFrame
    manifest: dict[str, object]
    catalog_sha256: str


def build_announcement_catalog(
    index_path: Path,
    *,
    preparation: FinalTestPreparation,
    authorization: FinalTestAuthorization,
    contract: FinalActionSourceContract,
    destination: Path,
) -> Path:
    """Publish a deterministic, fact-free catalog from complete query packages."""
    coverage_root = verify_official_query_collection_binding(
        destination,
        preparation=preparation,
        authorization=authorization,
        contract=contract,
    )
    expected_index = coverage_root / "official_query_coverage.json"
    if index_path.absolute() != expected_index.absolute():
        raise ValueError("official announcement catalog index path differs from collection")
    with opened_safe_directory(destination, label="official evidence destination") as root_fd:
        identities_fd = open_directory_at(
            root_fd,
            IDENTITIES_DIRECTORY,
            label="CNInfo security identity root",
        )
        try:
            identities = load_verified_cninfo_security_identities(
                identities_fd=identities_fd,
                identity_root=destination / IDENTITIES_DIRECTORY,
                symbols=load_bound_symbol_scope(preparation),
                preparation=preparation,
                authorization=authorization,
            )
        finally:
            os.close(identities_fd)
    scopes = expected_scopes(preparation, contract, identities=identities)
    verified_index = validate_official_query_coverage_index(
        expected_index,
        expected_scopes=scopes,
    )
    rows = catalog_rows_from_packages(
        coverage_root,
        scopes=scopes,
        contract=contract,
    )
    frame = pl.DataFrame(rows, schema=_CATALOG_SCHEMA).sort("catalog_id")
    if frame.select(pl.col("catalog_id").is_duplicated().any()).item():
        raise ValueError("official announcement catalog contains duplicate provenance")
    catalog_bytes = _parquet_bytes(frame)
    collection_path = destination / "official_query_collection.json"
    collection_sha256 = sha256_file(collection_path)
    manifest = _catalog_manifest(
        catalog_bytes,
        row_count=frame.height,
        index_sha256=verified_index.index_sha256,
        collection_sha256=collection_sha256,
        identity_index_sha256=identities.index_sha256,
        contract=contract,
    )
    with opened_safe_directory(destination, label="official evidence destination") as root_fd:
        write_frozen_tree_at(
            root_fd,
            CATALOG_DIRECTORY,
            {
                CATALOG_NAME: catalog_bytes,
                CATALOG_MANIFEST_NAME: canonical_json_bytes(manifest),
            },
            resumable=True,
            label="official announcement catalog",
        )
    catalog_path = destination / CATALOG_DIRECTORY / CATALOG_NAME
    loaded = load_verified_announcement_catalog(catalog_path)
    if loaded.catalog_sha256 != hashlib.sha256(catalog_bytes).hexdigest():
        raise ValueError("official announcement catalog changed during publication")
    return catalog_path


def load_verified_announcement_catalog(path: Path) -> VerifiedAnnouncementCatalog:
    """Load one immutable provenance catalog and reject any event-fact schema."""
    root = _catalog_root(path)
    with opened_safe_directory(root, label="official announcement catalog") as root_fd:
        files = read_frozen_tree_at(root_fd, label="official announcement catalog")
    if set(files) != {CATALOG_NAME, CATALOG_MANIFEST_NAME}:
        raise ValueError("official announcement catalog inventory is invalid")
    manifest = _canonical_object(
        files[CATALOG_MANIFEST_NAME],
        label="official announcement catalog manifest",
    )
    _validate_manifest(manifest, catalog_bytes=files[CATALOG_NAME])
    _validate_source_bindings(root.parent, manifest)
    frame = _read_catalog_frame(files[CATALOG_NAME])
    _validate_catalog_rows(frame, manifest)
    return VerifiedAnnouncementCatalog(
        path=root / CATALOG_NAME,
        root=root,
        frame=frame,
        manifest=manifest,
        catalog_sha256=hashlib.sha256(files[CATALOG_NAME]).hexdigest(),
    )


def _catalog_manifest(
    catalog_bytes: bytes,
    *,
    row_count: int,
    index_sha256: str,
    collection_sha256: str,
    identity_index_sha256: str,
    contract: FinalActionSourceContract,
) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "role": "official_announcement_catalog",
        "catalog": {
            "path": CATALOG_NAME,
            "sha256": hashlib.sha256(catalog_bytes).hexdigest(),
            "size_bytes": len(catalog_bytes),
        },
        "row_count": row_count,
        "official_query_coverage_sha256": index_sha256,
        "official_query_collection_sha256": collection_sha256,
        "cninfo_security_identities_sha256": identity_index_sha256,
        "allowed_url_prefixes": list(contract.allowed_url_prefixes),
        "market_sources": [
            {"market": market, "source": source}
            for market, source in contract.market_sources
        ],
        "contains_execution_event_facts": False,
    }


def _catalog_root(path: Path) -> Path:
    absolute = (path if path.is_absolute() else Path.cwd() / path).absolute()
    if absolute.name != CATALOG_NAME or absolute.parent.name != CATALOG_DIRECTORY:
        raise ValueError("official announcement catalog path is not canonical")
    if absolute.is_symlink() or not absolute.is_file():
        raise ValueError("official announcement catalog is missing or unsafe")
    for ancestor in (absolute, *absolute.parents):
        if ancestor.is_symlink():
            raise ValueError("official announcement catalog uses a symlink")
    return absolute.parent


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


def _parquet_bytes(frame: pl.DataFrame) -> bytes:
    stream = BytesIO()
    frame.write_parquet(stream, compression="zstd")
    return stream.getvalue()


def _validate_manifest(manifest: dict[str, object], *, catalog_bytes: bytes) -> None:
    required = {
        "schema_version",
        "role",
        "catalog",
        "row_count",
        "official_query_coverage_sha256",
        "official_query_collection_sha256",
        "cninfo_security_identities_sha256",
        "allowed_url_prefixes",
        "market_sources",
        "contains_execution_event_facts",
    }
    catalog = manifest.get("catalog")
    if (
        set(manifest) != required
        or manifest.get("schema_version") != _SCHEMA_VERSION
        or manifest.get("role") != "official_announcement_catalog"
        or not isinstance(catalog, dict)
        or catalog.get("path") != CATALOG_NAME
        or catalog.get("sha256") != hashlib.sha256(catalog_bytes).hexdigest()
        or catalog.get("size_bytes") != len(catalog_bytes)
        or not isinstance(manifest.get("row_count"), int)
        or manifest["row_count"] < 0
        or not _sha256(manifest.get("official_query_coverage_sha256"))
        or not _sha256(manifest.get("official_query_collection_sha256"))
        or not _sha256(manifest.get("cninfo_security_identities_sha256"))
        or not isinstance(manifest.get("allowed_url_prefixes"), list)
        or not isinstance(manifest.get("market_sources"), list)
        or manifest.get("contains_execution_event_facts") is not False
    ):
        raise ValueError("official announcement catalog manifest is invalid")


def _validate_source_bindings(destination: Path, manifest: dict[str, object]) -> None:
    collection = destination / "official_query_collection.json"
    index = destination / "official_query_coverage/official_query_coverage.json"
    identities = destination / IDENTITIES_DIRECTORY / IDENTITY_INDEX_NAME
    for path, expected, label in (
        (
            collection,
            manifest["official_query_collection_sha256"],
            "official query collection",
        ),
        (
            index,
            manifest["official_query_coverage_sha256"],
            "official query coverage",
        ),
        (
            identities,
            manifest["cninfo_security_identities_sha256"],
            "CNInfo security identity index",
        ),
    ):
        _assert_safe_regular_file(path, label=label)
        if sha256_file(path) != expected:
            raise ValueError(f"{label} identity differs from announcement catalog")


def _read_catalog_frame(catalog_bytes: bytes) -> pl.DataFrame:
    try:
        frame = pl.read_parquet(BytesIO(catalog_bytes))
    except pl.exceptions.PolarsError as error:
        raise ValueError("official announcement catalog data is invalid") from error
    if tuple(frame.columns) != _CATALOG_COLUMNS:
        raise ValueError("official announcement catalog schema contains event facts")
    try:
        return frame.cast(_CATALOG_SCHEMA, strict=True)
    except pl.exceptions.PolarsError as error:
        raise ValueError("official announcement catalog schema is invalid") from error


def _validate_catalog_rows(frame: pl.DataFrame, manifest: dict[str, object]) -> None:
    if frame.height != manifest["row_count"]:
        raise ValueError("official announcement catalog row count differs")
    if frame.select(pl.any_horizontal(pl.all().is_null()).any()).item():
        raise ValueError("official announcement catalog contains null provenance")
    if frame.select(pl.col("catalog_id").is_duplicated().any()).item():
        raise ValueError("official announcement catalog contains duplicate provenance")
    if frame.height and frame.get_column("catalog_id").to_list() != sorted(
        frame.get_column("catalog_id").to_list()
    ):
        raise ValueError("official announcement catalog is not stably ordered")
    prefixes = manifest["allowed_url_prefixes"]
    source_mapping = {
        item.get("market"): item.get("source")
        for item in manifest["market_sources"]
        if isinstance(item, dict)
    }
    if not all(isinstance(prefix, str) for prefix in prefixes):
        raise ValueError("official announcement catalog URL policy is invalid")
    expected_market_sources = [
        {"market": market, "source": source}
        for market, source in OFFICIAL_MARKET_SOURCES
    ]
    if (
        tuple(prefixes) != OFFICIAL_EVIDENCE_URL_PREFIXES
        or manifest["market_sources"] != expected_market_sources
        or source_mapping != dict(OFFICIAL_MARKET_SOURCES)
    ):
        raise ValueError("official announcement catalog URL policy is invalid")
    for row in frame.iter_rows(named=True):
        if (
            source_mapping.get(row["market"]) != row["source"]
            or not any(row["source_url"].startswith(prefix) for prefix in prefixes)
            or not _safe_catalog_url(row["source_url"])
            or not evidence_url_matches_source(row["source"], row["source_url"])
            or not _sha256(row["source_response_sha256"])
            or not _sha256(row["package_request_sha256"])
            or not _sha256(row["package_pages_sha256"])
            or not _sha256(row["package_manifest_sha256"])
            or row["source_response_size_bytes"] < 0
        ):
            raise ValueError("official announcement catalog provenance is invalid")


def _safe_catalog_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and ".." not in Path(parsed.path).parts
    )


def _assert_safe_regular_file(path: Path, *, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is missing or unsafe")
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise ValueError(f"{label} uses a symlink")


def _sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )
