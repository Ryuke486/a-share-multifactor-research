from __future__ import annotations

import json
from pathlib import Path
import shutil
from typing import Any

from ashare_multifactor.audit.records import (
    file_record,
    sha256_file,
    verify_file_record,
)


_PERFORMANCE_LIMIT_SECONDS = 36 * 60 * 60
_VALIDATION_PERIOD = ["2017-01-01", "2021-12-31"]
_KNOWN_EVENT_PERIOD = ["2005-01-01", "2021-12-31"]
_FILES = {
    "sample.parquet": "collector_rehearsal_sample",
    "catalog.parquet": "collector_rehearsal_catalog",
    "routing.parquet": "collector_rehearsal_routing",
    "official_query_coverage/official_query_coverage.json": (
        "collector_query_coverage"
    ),
    "rehearsal_report.json": "collector_rehearsal_report",
    "process_recovery_drill.json": "collector_process_recovery_drill",
    "routing_audit/audit_report_reviewed.json": "collector_routing_audit",
    "routing_audit/known_event_audit_2005_2021.json": (
        "collector_known_event_audit"
    ),
    "routing_audit/exclusion_reviewed.parquet": (
        "collector_exclusion_reviewed"
    ),
}


def write_collector_readiness_audit(
    source_root: Path,
    destination: Path,
) -> dict[str, object]:
    """Copy the bounded pre-2022 rehearsal evidence into one immutable audit."""
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise FileExistsError(
                f"collector readiness audit root is not empty: {destination}"
            )
    else:
        destination.mkdir(parents=True)
    for relative_path in _FILES:
        source = source_root / relative_path
        if not source.is_file() or source.is_symlink():
            raise ValueError(
                f"collector readiness audit source is invalid: {relative_path}"
            )
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    evidence = _validate_evidence(destination)
    records = [
        file_record(
            destination / relative_path,
            root=destination,
            role=role,
        ).to_dict()
        for relative_path, role in _FILES.items()
    ]
    manifest: dict[str, object] = {
        "status": "ready",
        "period": _VALIDATION_PERIOD,
        "implementation_sha256": evidence["implementation_sha256"],
        "files": records,
    }
    _write_json(destination / "manifest.json", manifest)
    return manifest


def verify_collector_readiness_audit(root: Path) -> str:
    """Fail closed on any changed file or relaxed Stage-9 readiness gate."""
    manifest_path = root / "manifest.json"
    try:
        manifest = _read_json(manifest_path)
    except ValueError as error:
        raise ValueError("invalid Stage-8 collector readiness manifest") from error
    records = manifest.get("files")
    if (
        manifest.get("status") != "ready"
        or manifest.get("period") != _VALIDATION_PERIOD
        or not isinstance(records, list)
        or len(records) != len(_FILES)
    ):
        raise ValueError("Stage-8 collector readiness audit is not ready")
    expected_roles = set(_FILES.values())
    actual_roles = {
        record.get("role") for record in records if isinstance(record, dict)
    }
    if actual_roles != expected_roles:
        raise ValueError("Stage-8 collector readiness file set is incomplete")
    for record in records:
        try:
            verify_file_record(record, root=root)
        except (FileNotFoundError, TypeError, ValueError) as error:
            raise ValueError("Stage-8 collector readiness audit hash changed") from error
    evidence = _validate_evidence(root)
    if manifest.get("implementation_sha256") != evidence["implementation_sha256"]:
        raise ValueError("Stage-8 collector implementation identity changed")
    return sha256_file(manifest_path)


def _validate_evidence(root: Path) -> dict[str, Any]:
    rehearsal = _read_json(root / "rehearsal_report.json")
    coverage = _read_json(
        root / "official_query_coverage/official_query_coverage.json"
    )
    routing = _read_json(root / "routing_audit/audit_report_reviewed.json")
    known = _read_json(
        root / "routing_audit/known_event_audit_2005_2021.json"
    )
    recovery = _read_json(root / "process_recovery_drill.json")
    implementation = str(rehearsal.get("implementation_sha256", ""))
    if (
        rehearsal.get("role") != "official_collection_validation_rehearsal"
        or rehearsal.get("period") != _VALIDATION_PERIOD
        or rehearsal.get("sample_count", 0) < 100
        or rehearsal.get("sample_sha256")
        != sha256_file(root / "sample.parquet")
        or rehearsal.get("coverage_index_sha256")
        != sha256_file(
            root / "official_query_coverage/official_query_coverage.json"
        )
        or rehearsal.get("split_count", 0) < 1
        or rehearsal.get("within_36_hour_budget") is not True
        or rehearsal.get("full_universe_eta_seconds", _PERFORMANCE_LIMIT_SECONDS + 1)
        > _PERFORMANCE_LIMIT_SECONDS
        or not _valid_sha256(implementation)
    ):
        raise ValueError("Stage-8 collector performance gate is incomplete")
    packages = coverage.get("packages")
    if (
        coverage.get("role") != "official_query_coverage"
        or not isinstance(packages, list)
        or not packages
        or any(
            not isinstance(package, dict)
            or package.get("category") != "announcements"
            or str(package.get("end", "")) > _VALIDATION_PERIOD[1]
            or not _valid_sha256(str(package.get("manifest_sha256", "")))
            for package in packages
        )
    ):
        raise ValueError("Stage-8 shared collector coverage is incomplete")
    if (
        routing.get("role") != "stage9_routing_exclusion_audit"
        or routing.get("catalog_sha256") != sha256_file(root / "catalog.parquet")
        or routing.get("routing_sha256") != sha256_file(root / "routing.parquet")
        or routing.get("reviewed_queue_sha256")
        != sha256_file(root / "routing_audit/exclusion_reviewed.parquet")
        or routing.get("exclusion_review_complete") is not True
        or routing.get("exclusion_sample_scope_complete") is not True
        or routing.get("exclusion_sample_count", 0) < 1000
        or routing.get("exclusion_confirmed_miss_count") != 0
        or routing.get("known_miss_count") != 0
        or routing.get("structured_candidate_date_miss_count") != 0
    ):
        raise ValueError("Stage-8 collector routing audit is incomplete")
    if (
        known.get("role") != "stage9_known_event_routing_audit"
        or known.get("status") != "passed"
        or known.get("period") != _KNOWN_EVENT_PERIOD
        or known.get("registered_title_miss_count") != 0
        or known.get("early_known_event_miss_count") != 0
        or known.get("structured_candidate_date_miss_count") != 0
        or known.get("validation_review_report_sha256")
        != sha256_file(root / "routing_audit/audit_report_reviewed.json")
    ):
        raise ValueError("Stage-8 collector known-event audit is incomplete")
    if (
        recovery.get("role") != "stage9_process_recovery_drill"
        or recovery.get("status") != "passed"
        or recovery.get("implementation_sha256") != implementation
        or recovery.get("maximum_restarts") != 2
        or recovery.get("restart_count", 3) > 2
        or recovery.get("identity_check_count", 0) < 1
    ):
        raise ValueError("Stage-8 collector recovery drill is incomplete")
    return {"implementation_sha256": implementation}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid collector readiness evidence: {path.name}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"invalid collector readiness evidence: {path.name}")
    return payload


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )
