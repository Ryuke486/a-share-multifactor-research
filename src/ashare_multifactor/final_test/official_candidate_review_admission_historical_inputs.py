"""Verification of frozen historical inputs for candidate admission."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Any

import polars as pl

from ashare_multifactor.final_test.official_candidate_review_admission import (
    _normalized_pdf_text,
)
from ashare_multifactor.final_test.official_document_validation import (
    validate_official_document,
)
from ashare_multifactor.final_test.official_query_coverage import (
    canonical_json_bytes,
)


HISTORICAL_PERIOD = ["2017-01-01", "2021-12-31"]
_DERIVATION_COLUMNS = {
    "pair_id",
    "historical_candidate_id",
    "symbol",
    "market",
    "ex_date",
    "announcement_date",
    "lag_calendar_days",
    "catalog_id",
    "announcement_id",
    "announcement_title",
    "source_url",
    "pdf_path",
    "pdf_sha256",
    "pdf_size_bytes",
    "pdf_page_count",
}
_DISCOVERY_COLUMNS = {
    "catalog_id",
    "announcement_id",
    "symbol",
    "market",
    "announcement_title",
    "announcement_date",
    "source_url",
    "already_cached",
}


@dataclass(frozen=True)
class VerifiedHistoricalAdmissionInputs:
    """All frozen historical inputs after byte and closure verification."""

    derivation_bytes: bytes
    derivation_manifest_bytes: bytes
    discovery_bytes: bytes
    receipt_index_bytes: bytes
    existing_inventory_bytes: bytes
    derivation_manifest: dict[str, Any]
    receipt_index: dict[str, Any]
    existing_inventory: dict[str, Any]
    derivation: pl.DataFrame
    discovery: pl.DataFrame
    pdfs: dict[str, dict[str, object]]


def load_verified_historical_admission_inputs(
    *,
    date_rule: dict[str, Any],
    derivation_path: Path,
    derivation_manifest_path: Path,
    discovery_path: Path,
    receipt_index_path: Path,
    existing_inventory_path: Path,
    pdf_cache_root: Path,
) -> VerifiedHistoricalAdmissionInputs:
    """Load frozen Session 00A/00B/01 inputs and prove their exact closure."""
    derivation_bytes = _verified_file(derivation_path)
    derivation_manifest_bytes = _verified_file(derivation_manifest_path)
    discovery_bytes = _verified_file(discovery_path)
    receipt_index_bytes = _verified_file(receipt_index_path)
    existing_inventory_bytes = _verified_file(existing_inventory_path)
    _verify_bound_hashes(
        date_rule,
        derivation_bytes=derivation_bytes,
        derivation_manifest_bytes=derivation_manifest_bytes,
        discovery_bytes=discovery_bytes,
        receipt_index_bytes=receipt_index_bytes,
        existing_inventory_bytes=existing_inventory_bytes,
    )
    derivation_manifest = _canonical_object(derivation_manifest_bytes)
    receipt_index = _canonical_object(receipt_index_bytes)
    existing_inventory = _canonical_object(existing_inventory_bytes)
    try:
        derivation = pl.read_parquet(derivation_path)
        discovery = pl.read_parquet(discovery_path)
    except pl.exceptions.PolarsError as error:
        raise ValueError("historical candidate admission parquet is invalid") from error
    _verify_derivation(
        derivation,
        manifest=derivation_manifest,
        parquet_bytes=derivation_bytes,
    )
    _verify_discovery(discovery)
    pdfs = _verified_pdf_index(
        discovery,
        receipt_index=receipt_index,
        existing_inventory=existing_inventory,
        cache_root=pdf_cache_root,
    )
    return VerifiedHistoricalAdmissionInputs(
        derivation_bytes=derivation_bytes,
        derivation_manifest_bytes=derivation_manifest_bytes,
        discovery_bytes=discovery_bytes,
        receipt_index_bytes=receipt_index_bytes,
        existing_inventory_bytes=existing_inventory_bytes,
        derivation_manifest=derivation_manifest,
        receipt_index=receipt_index,
        existing_inventory=existing_inventory,
        derivation=derivation,
        discovery=discovery,
        pdfs=pdfs,
    )


def historical_file_record(
    path: Path,
    payload: bytes,
    *,
    role: str,
    row_count: int,
) -> dict[str, object]:
    """Describe one immutable file input in an admission manifest."""
    return {
        "path": str(path),
        "role": role,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "row_count": row_count,
    }


def historical_cache_record(
    root: Path,
    pdfs: dict[str, dict[str, object]],
) -> dict[str, object]:
    """Describe the verified PDF cache by its stable content inventory."""
    records = [
        {
            "source_url": source_url,
            "path": Path(str(record["pdf_path"])).name,
            "sha256": record["pdf_sha256"],
            "size_bytes": record["pdf_size_bytes"],
            "page_count": record["pdf_page_count"],
        }
        for source_url, record in sorted(pdfs.items())
    ]
    return {
        "path": str(root),
        "role": "historical_pdf_cache",
        "sha256": hashlib.sha256(canonical_json_bytes(records)).hexdigest(),
        "size_bytes": sum(int(record["size_bytes"]) for record in records),
        "row_count": len(records),
    }


def _verify_bound_hashes(
    date_rule: dict[str, Any],
    *,
    derivation_bytes: bytes,
    derivation_manifest_bytes: bytes,
    discovery_bytes: bytes,
    receipt_index_bytes: bytes,
    existing_inventory_bytes: bytes,
) -> None:
    derivation = date_rule.get("derivation")
    historical = date_rule.get("historical_input_sha256")
    if (
        not isinstance(derivation, dict)
        or not isinstance(historical, dict)
        or derivation.get("manifest_sha256")
        != hashlib.sha256(derivation_manifest_bytes).hexdigest()
        or derivation.get("parquet_sha256")
        != hashlib.sha256(derivation_bytes).hexdigest()
        or historical.get("historical_pdf_discovery_parquet")
        != hashlib.sha256(discovery_bytes).hexdigest()
        or historical.get("historical_pdf_receipt_index")
        != hashlib.sha256(receipt_index_bytes).hexdigest()
        or historical.get("historical_pdf_existing_inventory")
        != hashlib.sha256(existing_inventory_bytes).hexdigest()
    ):
        raise ValueError("historical candidate admission input hash differs")


def _verify_derivation(
    frame: pl.DataFrame,
    *,
    manifest: dict[str, Any],
    parquet_bytes: bytes,
) -> None:
    record = manifest.get("derivation")
    if (
        manifest.get("schema")
        != "stage9_candidate_review_admission_date_rule_derivation/v1"
        or manifest.get("role")
        != "candidate_review_admission_date_rule_derivation"
        or manifest.get("period") != HISTORICAL_PERIOD
        or manifest.get("final_test_candidate_gaps_read") is not False
        or manifest.get("final_test_strategy_outputs_read") is not False
        or not isinstance(record, dict)
        or record.get("sha256") != hashlib.sha256(parquet_bytes).hexdigest()
        or record.get("size_bytes") != len(parquet_bytes)
        or record.get("pair_count") != frame.height
        or not _DERIVATION_COLUMNS <= set(frame.columns)
        or frame.is_empty()
        or frame.get_column("pair_id").n_unique() != frame.height
        or frame.filter(
            (pl.col("ex_date") < date(2017, 1, 1))
            | (pl.col("ex_date") > date(2021, 12, 31))
            | (pl.col("announcement_date") < date(2017, 1, 1))
            | (pl.col("announcement_date") > date(2021, 12, 31))
        ).height
    ):
        raise ValueError("historical candidate derivation is invalid")


def _verify_discovery(frame: pl.DataFrame) -> None:
    if (
        not _DISCOVERY_COLUMNS <= set(frame.columns)
        or frame.is_empty()
        or frame.get_column("catalog_id").n_unique() != frame.height
        or frame.get_column("source_url").n_unique() != frame.height
    ):
        raise ValueError("historical PDF discovery is invalid")


def _verified_pdf_index(
    discovery: pl.DataFrame,
    *,
    receipt_index: dict[str, Any],
    existing_inventory: dict[str, Any],
    cache_root: Path,
) -> dict[str, dict[str, object]]:
    receipts = receipt_index.get("receipts")
    existing = existing_inventory.get("files")
    if (
        receipt_index.get("schema")
        != "stage9_historical_pdf_receipt_index/v1"
        or receipt_index.get("role")
        != "session00b_historical_pdf_receipt_index"
        or not isinstance(receipts, list)
        or receipt_index.get("receipt_count") != len(receipts)
        or existing_inventory.get("schema")
        != "stage9_existing_historical_pdf_inventory/v1"
        or existing_inventory.get("role") != "offline_existing_cache_inventory"
        or not isinstance(existing, list)
        or existing_inventory.get("file_count") != len(existing)
        or existing_inventory.get("valid_pdf_count") != len(existing)
        or cache_root.is_symlink()
        or not cache_root.is_dir()
    ):
        raise ValueError("historical PDF inventory is invalid")
    by_url: dict[str, dict[str, object]] = {}
    for record in receipts:
        if not isinstance(record, dict):
            raise ValueError("historical PDF receipt index is invalid")
        receipt_path = _cache_path(cache_root, record.get("receipt_path"))
        receipt_bytes = _verified_file(receipt_path)
        if (
            record.get("receipt_sha256")
            != hashlib.sha256(receipt_bytes).hexdigest()
        ):
            raise ValueError("historical PDF receipt changed")
        _add_pdf_record(
            by_url,
            record,
            receipt=_canonical_object(receipt_bytes),
            cache_root=cache_root,
        )
    discovery_urls = set(discovery.get_column("source_url").to_list())
    for record in existing:
        if (
            isinstance(record, dict)
            and record.get("source_url") in discovery_urls
        ):
            _add_pdf_record(
                by_url,
                record,
                receipt=None,
                cache_root=cache_root,
            )
    pending_urls = set(
        discovery.filter(~pl.col("already_cached")).get_column("source_url")
    )
    cached_urls = set(
        discovery.filter(pl.col("already_cached")).get_column("source_url")
    )
    receipt_urls = {
        str(record.get("source_url"))
        for record in receipts
        if isinstance(record, dict)
    }
    if (
        receipt_urls != pending_urls
        or set(by_url) != discovery_urls
        or not cached_urls
        or receipt_urls & cached_urls
    ):
        raise ValueError("historical PDF discovery/index closure differs")
    return by_url


def _add_pdf_record(
    by_url: dict[str, dict[str, object]],
    record: dict[str, object],
    *,
    receipt: dict[str, Any] | None,
    cache_root: Path,
) -> None:
    source_url = record.get("source_url")
    path_value = record.get("pdf_path", record.get("path"))
    pdf_path = _cache_path(cache_root, path_value)
    payload = _verified_file(pdf_path)
    validated = validate_official_document(payload, source_url=str(source_url))
    page_count = (
        receipt.get("pdf_page_count")
        if receipt is not None
        else record.get("page_count")
    )
    if (
        not isinstance(source_url, str)
        or not source_url
        or source_url in by_url
        or record.get("sha256", record.get("pdf_sha256"))
        != validated.sha256
        or record.get("size_bytes", record.get("pdf_size_bytes"))
        != validated.size_bytes
        or page_count != validated.page_count
        or (
            receipt is not None
            and (
                receipt.get("schema") != "stage9_historical_pdf_receipt/v1"
                or receipt.get("source_url") != source_url
                or receipt.get("catalog_id") != record.get("catalog_id")
                or receipt.get("symbol") != record.get("symbol")
                or receipt.get("pdf_sha256") != validated.sha256
                or receipt.get("pdf_size_bytes") != validated.size_bytes
                or receipt.get("final_test_strategy_outputs_read") is not False
            )
        )
    ):
        raise ValueError("historical cached PDF identity differs")
    by_url[source_url] = {
        **record,
        "pdf_path": pdf_path,
        "pdf_sha256": validated.sha256,
        "pdf_size_bytes": validated.size_bytes,
        "pdf_page_count": validated.page_count,
        "normalized_text": _normalized_pdf_text(payload),
    }


def _verified_file(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"historical admission input is missing: {path}")
    return path.read_bytes()


def _canonical_object(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("historical admission JSON is invalid") from error
    if not isinstance(value, dict) or canonical_json_bytes(value) != payload:
        raise ValueError("historical admission JSON is not canonical")
    return value


def _cache_path(root: Path, recorded: object) -> Path:
    if not isinstance(recorded, str) or not recorded:
        raise ValueError("historical cache path is invalid")
    path = root / Path(recorded).name
    if path.resolve().parent != root.resolve():
        raise ValueError("historical cache path escaped its root")
    return path
