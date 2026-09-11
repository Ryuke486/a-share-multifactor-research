from __future__ import annotations

from collections import Counter
import hashlib
from pathlib import Path
from typing import Any

import polars as pl


_CERTIFIED_RUN_IDS = (
    "stage7_controlled_65b19e1_1",
    "stage7_controlled_65b19e1_2",
)
_QUERY_YEARS = [2017, 2018, 2019, 2020, 2021]
_SYMBOL_COUNT = 2_718
_QUERY_COUNT = _SYMBOL_COUNT * len(_QUERY_YEARS)


class Task6RWaiverError(ValueError):
    """Reject an external-source waiver that broadens the approved exception."""


def _file_identity(path: Path) -> dict[str, object]:
    return {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": path.stat().st_size,
    }


def _frame_difference_count(expected: pl.DataFrame, actual: pl.DataFrame) -> int:
    if expected.columns != actual.columns or expected.schema != actual.schema:
        return max(expected.height, actual.height, 1)
    expected_rows = Counter(expected.iter_rows())
    actual_rows = Counter(actual.iter_rows())
    return sum((expected_rows - actual_rows).values()) + sum((actual_rows - expected_rows).values())


def _consumer_record(
    name: str,
    historical_roots: list[Path],
    regenerated_root: Path,
) -> dict[str, object]:
    regenerated_path = regenerated_root / name
    regenerated = pl.read_parquet(regenerated_path)
    matches: list[bool] = []
    historical_identities: list[dict[str, object]] = []
    for historical_root in historical_roots:
        historical_path = historical_root / name
        historical = pl.read_parquet(historical_path)
        difference_count = _frame_difference_count(historical, regenerated)
        if difference_count:
            raise Task6RWaiverError(f"consumer table differs: {name}")
        matches.append(True)
        historical_identities.append(_file_identity(historical_path))
    return {
        "columns": regenerated.columns,
        "schema": {name: str(dtype) for name, dtype in regenerated.schema.items()},
        "row_count": regenerated.height,
        "regenerated_identity": _file_identity(regenerated_path),
        "historical_identities": historical_identities,
        "historical_run_matches": matches,
        "semantic_difference_count": 0,
    }


def build_external_source_revision_waiver(
    *,
    historical_roots: list[Path],
    regenerated_root: Path,
    new_raw_path: Path,
    historical_raw_identity: dict[str, object],
    download_evidence: dict[str, object],
) -> dict[str, object]:
    actual_run_ids = tuple(root.parent.name for root in historical_roots)
    if (
        actual_run_ids != _CERTIFIED_RUN_IDS
        or any(root.name != "datasets" for root in historical_roots)
        or len({root.resolve() for root in historical_roots}) != 2
    ):
        raise Task6RWaiverError("two frozen certified historical runs are required")
    actual_new_identity = _file_identity(new_raw_path)
    serial_access = _validate_download_evidence(
        download_evidence,
        actual_new_identity,
    )
    if historical_raw_identity == actual_new_identity:
        raise Task6RWaiverError("historical and revised raw identities must differ")
    tables = {
        name: _consumer_record(name, historical_roots, regenerated_root)
        for name in ("corporate_actions.parquet", "security_events.parquet")
    }
    return {
        "schema_version": 1,
        "scope": "candidate1_task6_shadow_acceptance_only",
        "status": "passed",
        "historical_raw_identity": historical_raw_identity,
        "new_raw_identity": actual_new_identity,
        "historical_raw_bytes_recovered": False,
        "certified_run_ids": list(_CERTIFIED_RUN_IDS),
        "download_evidence": download_evidence,
        "serial_access": serial_access,
        "consumer_tables": tables,
        "consumer_semantic_differences": 0,
        "production_adapter_modified": False,
        "publication_performed": False,
    }


def map_verified_baostock_identity(
    actual_inputs: dict[str, Any],
    certified_inputs: dict[str, Any],
    waiver: dict[str, Any],
) -> dict[str, Any]:
    return _map_baostock_identity(
        actual_inputs, certified_inputs, waiver, production_adapter_modified=False
    )


def map_task6s_baostock_identity(
    actual_inputs: dict[str, Any],
    certified_inputs: dict[str, Any],
    waiver: dict[str, Any],
) -> dict[str, Any]:
    """Replay the recorded 06S shadow mapping, not a new equivalence measurement.

    This test-only gate consumes the audited historical receipt. A new real run
    still needs regenerated consumer multiset evidence and its own code identity.
    Never use this mapping to publish or to rewrite a historical certificate.
    """
    remediation = {
        "public_seam": "load_validation_corporate_actions",
        "behavior": "deduplicate by economic event after preferring the strongest frozen official provenance",
        "cash_correction_provenance_covered": True,
        "share_correction_provenance_covered": True,
        "dates_amounts_share_ratios_and_accounting_unchanged": True,
        "post_review_regenerated_row_count": 26445,
        "post_review_certified_run_semantic_differences": [0, 0],
    }
    shadow = {
        "input_identity_difference_keys": ["baostock_dividends"],
        "verified_identity_mapping_equal_to_certificate": True,
        "final_test_current_absent": True,
        "stage7": {
            "publish": False,
            "staged": True,
            "core_file_count": 98,
            "core_hash_mismatches": 0,
        },
        "stage8": {
            "publish": False,
            "staged": True,
            "core_hash_matches_certificate": {
                "robustness_results.parquet": True,
                "report.md": True,
                "protocol_gate.json": True,
            },
            "action_coverage_tree_equal": True,
            "collector_readiness_tree_equal": True,
            "opening_token_status": "closed",
        },
    }

    def require_fields(actual: object, expected: dict[str, Any]) -> None:
        if not isinstance(actual, dict):
            raise Task6RWaiverError("06S remediation receipt is incomplete")
        for key, value in expected.items():
            if isinstance(value, dict):
                require_fields(actual.get(key), value)
            elif type(actual.get(key)) is not type(value) or actual[key] != value:
                raise Task6RWaiverError("06S remediation receipt is incomplete")

    require_fields(waiver.get("remediation"), remediation)
    require_fields(waiver.get("shadow_acceptance"), shadow)
    return _map_baostock_identity(
        actual_inputs, certified_inputs, waiver, production_adapter_modified=True
    )


def _map_baostock_identity(
    actual_inputs: dict[str, Any],
    certified_inputs: dict[str, Any],
    waiver: dict[str, Any],
    *,
    production_adapter_modified: bool,
) -> dict[str, Any]:
    required = {
        "schema_version": 1,
        "scope": "candidate1_task6_shadow_acceptance_only",
        "status": "passed",
        "consumer_semantic_differences": 0,
        "historical_raw_bytes_recovered": False,
        "production_adapter_modified": production_adapter_modified,
        "publication_performed": False,
    }
    if any(waiver.get(key) != value for key, value in required.items()):
        raise Task6RWaiverError("external source revision waiver is not approved")
    if waiver.get("certified_run_ids") != list(_CERTIFIED_RUN_IDS):
        raise Task6RWaiverError("external source revision waiver is not approved")
    try:
        serial_access = _validate_download_evidence(
            waiver["download_evidence"],
            waiver["new_raw_identity"],
        )
    except (KeyError, Task6RWaiverError) as error:
        raise Task6RWaiverError("external source revision waiver is not approved") from error
    if waiver.get("serial_access") != serial_access:
        raise Task6RWaiverError("external source revision waiver is not approved")
    tables = waiver.get("consumer_tables")
    required_tables = {"corporate_actions.parquet", "security_events.parquet"}
    if not isinstance(tables, dict) or set(tables) != required_tables:
        raise Task6RWaiverError("external source revision waiver is not approved")
    for record in tables.values():
        if (
            not isinstance(record, dict)
            or record.get("historical_run_matches") != [True, True]
            or record.get("semantic_difference_count") != 0
        ):
            raise Task6RWaiverError("external source revision waiver is not approved")
    if set(actual_inputs) != set(certified_inputs):
        raise Task6RWaiverError("certified input identity keys differ")
    if actual_inputs.get("baostock_dividends") != waiver.get("new_raw_identity"):
        raise Task6RWaiverError("current BaoStock identity differs from waiver")
    if certified_inputs.get("baostock_dividends") != waiver.get("historical_raw_identity"):
        raise Task6RWaiverError("certified BaoStock identity differs from waiver")
    for name in certified_inputs:
        if name == "baostock_dividends":
            continue
        if actual_inputs[name] != certified_inputs[name]:
            raise Task6RWaiverError("non-BaoStock certified input identity differs")
    return certified_inputs


def _validate_download_evidence(
    evidence: dict[str, object],
    actual_new_identity: dict[str, object],
) -> dict[str, object]:
    if evidence.get("new_raw_identity") != actual_new_identity:
        raise Task6RWaiverError("new BaoStock raw identity differs from download evidence")
    serial_access = evidence.get("serial_access")
    hashes = (evidence.get("coverage_sha256"), evidence.get("download_log_sha256"))
    if (
        evidence.get("source") != "baostock_query_dividend_data"
        or not isinstance(evidence.get("baostock_version"), str)
        or not evidence["baostock_version"]
        or evidence.get("query_year_type") != "operate"
        or evidence.get("query_years") != _QUERY_YEARS
        or evidence.get("symbol_count") != _SYMBOL_COUNT
        or evidence.get("successful_query_count") != _QUERY_COUNT
        or evidence.get("row_count") != 9_516
        or evidence.get("column_count") != 16
        or any(
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in hashes
        )
        or not isinstance(serial_access, dict)
        or serial_access.get("process_count") != 1
        or serial_access.get("session_count") != 1
        or serial_access.get("query_count") != _QUERY_COUNT
        or serial_access.get("nonzero_error_count") != 0
        or serial_access.get("login_succeeded") is not True
        or serial_access.get("logout_succeeded") is not True
    ):
        raise Task6RWaiverError("BaoStock download evidence is incomplete")
    return serial_access
