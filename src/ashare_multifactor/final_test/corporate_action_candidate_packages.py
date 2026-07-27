"""Immutable per-query packages for corporate-action candidate collection."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import stat
from typing import Any

import polars as pl

from ashare_multifactor.audit.secure_tree import (
    read_frozen_tree_at,
    write_frozen_tree_at,
)
from ashare_multifactor.final_test.action_source_contract import FinalActionSourceContract
from ashare_multifactor.final_test.corporate_action_candidate_contract import (
    REQUIRED_BAOSTOCK_FIELDS,
    normalize_corporate_action_candidates,
)
from ashare_multifactor.final_test.gate import FINAL_TEST_END, FINAL_TEST_START
from ashare_multifactor.final_test.official_query_coverage import canonical_json_bytes
from ashare_multifactor.final_test.recovery_secure_fs import open_directory_at


PACKAGE_SCHEMA_VERSION = "1"
RESPONSE_NAME = "response.json"
PACKAGE_MANIFEST_NAME = "query_manifest.json"


@dataclass(frozen=True)
class CandidateQueryScope:
    """One immutable provider query."""

    symbol: str
    code: str
    year: int
    year_type: str


def expected_candidate_query_scopes(
    symbols: list[str],
    contract: FinalActionSourceContract,
) -> tuple[CandidateQueryScope, ...]:
    """Expand the sealed provider contract into its exact query scope."""
    if (
        contract.provider != "baostock_query_dividend_data"
        or contract.query_year_type != "operate"
        or contract.query_years
        != tuple(range(FINAL_TEST_START.year, FINAL_TEST_END.year + 1))
    ):
        raise ValueError("corporate-action candidate contract is invalid")
    return tuple(
        CandidateQueryScope(
            symbol=symbol,
            code=f"{'sh' if symbol.startswith('6') else 'sz'}.{symbol}",
            year=year,
            year_type=contract.query_year_type,
        )
        for symbol in symbols
        for year in contract.query_years
    )


def candidate_query_package_id(scope: CandidateQueryScope) -> str:
    """Derive a stable directory name from the query identity."""
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "code": scope.code,
                "year": scope.year,
                "year_type": scope.year_type,
            }
        )
    ).hexdigest()


def candidate_query_package_exists(parent_fd: int, package_id: str) -> bool:
    """Return whether a safe package directory already exists."""
    try:
        metadata = os.stat(package_id, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("corporate-action candidate package is unsafe")
    return True


def write_candidate_query_package(
    queries_fd: int,
    *,
    package_id: str,
    scope: CandidateQueryScope,
    fields: list[str],
    rows: list[list[str]],
) -> None:
    """Freeze one validated provider response."""
    response = canonical_json_bytes({"fields": fields, "rows": rows})
    manifest = canonical_json_bytes(
        {
            "schema_version": PACKAGE_SCHEMA_VERSION,
            "role": "baostock_corporate_action_query",
            "symbol": scope.symbol,
            "code": scope.code,
            "year": scope.year,
            "year_type": scope.year_type,
            "row_count": len(rows),
            "response_sha256": hashlib.sha256(response).hexdigest(),
        }
    )
    write_frozen_tree_at(
        queries_fd,
        package_id,
        {
            RESPONSE_NAME: response,
            PACKAGE_MANIFEST_NAME: manifest,
        },
        resumable=False,
        label="corporate-action candidate query package",
    )


def read_candidate_query_package(
    queries_fd: int,
    *,
    package_id: str,
    scope: CandidateQueryScope,
) -> tuple[list[str], list[list[str]]]:
    """Read and fully revalidate one frozen provider response."""
    package_fd = open_directory_at(
        queries_fd,
        package_id,
        label="corporate-action candidate query package",
    )
    try:
        files = read_frozen_tree_at(
            package_fd,
            label="corporate-action candidate query package",
        )
    finally:
        os.close(package_fd)
    if set(files) != {RESPONSE_NAME, PACKAGE_MANIFEST_NAME}:
        raise ValueError("corporate-action candidate query package inventory changed")
    try:
        response = _canonical_object(files[RESPONSE_NAME])
        manifest = _canonical_object(files[PACKAGE_MANIFEST_NAME])
        fields = response["fields"]
        rows = response["rows"]
    except (KeyError, TypeError) as error:
        raise ValueError("corporate-action candidate query package is invalid") from error
    expected_manifest = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "role": "baostock_corporate_action_query",
        "symbol": scope.symbol,
        "code": scope.code,
        "year": scope.year,
        "year_type": scope.year_type,
        "row_count": len(rows),
        "response_sha256": hashlib.sha256(files[RESPONSE_NAME]).hexdigest(),
    }
    if manifest != expected_manifest:
        raise ValueError("corporate-action candidate query package identity changed")
    validate_candidate_query_response(fields, rows, scope=scope)
    return fields, rows


def validate_candidate_query_response(
    fields: object,
    rows: object,
    *,
    scope: CandidateQueryScope,
) -> None:
    """Reject provider schema, row-width, or scope drift."""
    if (
        not isinstance(fields, list)
        or not fields
        or any(not isinstance(field, str) or not field for field in fields)
        or len(fields) != len(set(fields))
        or not REQUIRED_BAOSTOCK_FIELDS.issubset(fields)
        or not isinstance(rows, list)
    ):
        raise ValueError("BaoStock dividend response schema changed")
    if any(
        not isinstance(row, list)
        or len(row) != len(fields)
        or any(not isinstance(value, str) for value in row)
        for row in rows
    ):
        raise ValueError("BaoStock dividend response row width changed")
    code_index = fields.index("code")
    if any(row[code_index] != scope.code for row in rows):
        raise ValueError("BaoStock response symbol escapes the query scope")
    frame = pl.DataFrame(
        [
            {
                **dict(zip(fields, row, strict=True)),
                "query_year": scope.year,
                "query_year_type": scope.year_type,
            }
            for row in rows
        ],
        schema={
            **{field: pl.String for field in fields},
            "query_year": pl.Int64,
            "query_year_type": pl.String,
        },
        strict=False,
    )
    normalize_corporate_action_candidates(frame, symbols={scope.symbol})


def _canonical_object(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("candidate package JSON is invalid") from error
    if not isinstance(value, dict) or payload != canonical_json_bytes(value):
        raise ValueError("candidate package JSON is not canonical")
    return value
