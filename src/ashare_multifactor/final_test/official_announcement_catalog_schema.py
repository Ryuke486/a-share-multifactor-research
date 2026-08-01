"""Shared deterministic schema for official announcement catalog rows."""

from __future__ import annotations

import hashlib
from io import BytesIO
import json
from pathlib import Path
from typing import Mapping, Protocol

import polars as pl


SUPPLEMENTS_DIRECTORY = "official_query_supplements"
SUPPLEMENT_MANIFEST_NAME = "supplement_manifest.json"
SUPPLEMENT_CATALOG_NAME = "catalog.parquet"

CATALOG_COLUMNS = (
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
CATALOG_SCHEMA = {
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
_SEMANTIC_COLUMNS = (
    "announcement_title",
    "announcement_time_raw",
    "source",
    "symbol",
    "market",
    "category",
    "query_category",
    "source_url",
)
_KEY_COLUMNS = ("source", "symbol", "announcement_id", "source_url")


class VerifiedCandidateQueryCatalogSupplement(Protocol):
    """Minimal verified value consumed by the catalog merge boundary."""

    manifest_path: Path
    manifest_sha256: str
    manifest: dict[str, object]
    catalog: pl.DataFrame
    packages: tuple[object, ...]
    inventory_sha256: str
    inventory_file_count: int
    inventory_size_bytes: int


def supplement_inventory(files: Mapping[str, bytes]) -> dict[str, object]:
    """Bind every supplement payload except the self-addressing manifest."""
    records = [
        {
            "path": path,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }
        for path, payload in sorted(files.items())
    ]
    core = {
        "files": records,
        "file_count": len(records),
        "size_bytes": sum(record["size_bytes"] for record in records),
    }
    canonical = json.dumps(
        core,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {
        **core,
        "sha256": hashlib.sha256(canonical).hexdigest(),
    }


def announcement_catalog_frame(
    rows: list[dict[str, object]],
) -> pl.DataFrame:
    """Normalize provenance rows through the single catalog schema."""
    frame = pl.DataFrame(rows, schema=CATALOG_SCHEMA).sort("catalog_id")
    if frame.select(pl.col("catalog_id").is_duplicated().any()).item():
        raise ValueError(
            "official announcement catalog contains duplicate provenance"
        )
    return frame


def catalog_parquet_bytes(frame: pl.DataFrame) -> bytes:
    """Encode one canonical, stably ordered catalog frame."""
    normalized = announcement_catalog_frame(frame.to_dicts())
    stream = BytesIO()
    normalized.write_parquet(stream, compression="zstd")
    return stream.getvalue()


def read_catalog_frame(payload: bytes) -> pl.DataFrame:
    """Decode catalog bytes through the exact shared schema."""
    try:
        frame = pl.read_parquet(BytesIO(payload))
        return frame.cast(CATALOG_SCHEMA, strict=True)
    except pl.exceptions.PolarsError as error:
        raise ValueError("official announcement catalog is unreadable") from error


def merge_candidate_query_supplement_rows(
    base: pl.DataFrame,
    supplement: VerifiedCandidateQueryCatalogSupplement,
) -> tuple[pl.DataFrame, int]:
    """Merge an already-verified supplement without importing its workflow."""
    normalized_base = announcement_catalog_frame(base.to_dicts())
    base_bytes = catalog_parquet_bytes(normalized_base)
    bindings = supplement.manifest.get("bindings")
    binding = (
        bindings.get("base_catalog")
        if isinstance(bindings, dict)
        else None
    )
    if (
        not isinstance(binding, dict)
        or binding.get("sha256") != hashlib.sha256(base_bytes).hexdigest()
        or binding.get("size_bytes") != len(base_bytes)
    ):
        raise ValueError("candidate query supplement base catalog identity differs")
    base_rows = {
        tuple(row[column] for column in _KEY_COLUMNS): row
        for row in normalized_base.iter_rows(named=True)
    }
    added: list[dict[str, object]] = []
    for row in supplement.catalog.iter_rows(named=True):
        key = tuple(row[column] for column in _KEY_COLUMNS)
        existing = base_rows.get(key)
        if existing is None:
            added.append(row)
            continue
        if any(existing[column] != row[column] for column in _SEMANTIC_COLUMNS):
            raise ValueError("candidate query supplement semantic drift")
    merged = announcement_catalog_frame([*normalized_base.to_dicts(), *added])
    return merged, len(added)
