import json
from pathlib import Path

from ashare_multifactor.audit.records import sha256_file
from ashare_multifactor.robustness.collector_readiness import (
    verify_collector_readiness_audit,
    write_collector_readiness_audit,
)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _source(root: Path) -> Path:
    sample = root / "sample.parquet"
    catalog = root / "catalog.parquet"
    routing = root / "routing.parquet"
    reviewed = root / "routing_audit/exclusion_reviewed.parquet"
    for path, content in (
        (sample, b"sample"),
        (catalog, b"catalog"),
        (routing, b"routing"),
        (reviewed, b"reviewed"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    coverage = root / "official_query_coverage/official_query_coverage.json"
    _write_json(
        coverage,
        {
            "role": "official_query_coverage",
            "schema_version": "2",
            "packages": [
                {
                    "symbol": "600009",
                    "category": "announcements",
                    "start": "2017-01-01",
                    "end": "2021-12-31",
                    "manifest_sha256": "1" * 64,
                }
            ],
        },
    )
    implementation = "a" * 64
    review = root / "routing_audit/audit_report_reviewed.json"
    _write_json(
        review,
        {
            "role": "stage9_routing_exclusion_audit",
            "schema_version": "1",
            "catalog_sha256": sha256_file(catalog),
            "routing_sha256": sha256_file(routing),
            "reviewed_queue_sha256": sha256_file(reviewed),
            "exclusion_review_complete": True,
            "exclusion_sample_scope_complete": True,
            "exclusion_sample_count": 1000,
            "exclusion_confirmed_miss_count": 0,
            "known_miss_count": 0,
            "structured_candidate_date_miss_count": 0,
        },
    )
    _write_json(
        root / "routing_audit/known_event_audit_2005_2021.json",
        {
            "role": "stage9_known_event_routing_audit",
            "schema_version": "1",
            "status": "passed",
            "period": ["2005-01-01", "2021-12-31"],
            "registered_title_miss_count": 0,
            "early_known_event_miss_count": 0,
            "structured_candidate_date_miss_count": 0,
            "validation_review_report_sha256": sha256_file(review),
        },
    )
    _write_json(
        root / "rehearsal_report.json",
        {
            "role": "official_collection_validation_rehearsal",
            "schema_version": "1",
            "period": ["2017-01-01", "2021-12-31"],
            "implementation_sha256": implementation,
            "sample_count": 100,
            "sample_sha256": sha256_file(sample),
            "coverage_index_sha256": sha256_file(coverage),
            "split_count": 1,
            "full_universe_eta_seconds": 129599,
            "within_36_hour_budget": True,
        },
    )
    _write_json(
        root / "process_recovery_drill.json",
        {
            "role": "stage9_process_recovery_drill",
            "schema_version": "1",
            "status": "passed",
            "implementation_sha256": implementation,
            "maximum_restarts": 2,
            "restart_count": 1,
            "identity_check_count": 1,
        },
    )
    return root


def test_readiness_audit_binds_performance_routing_and_recovery(tmp_path: Path) -> None:
    destination = tmp_path / "audit"
    manifest = write_collector_readiness_audit(
        _source(tmp_path / "source"),
        destination,
    )

    assert manifest["status"] == "ready"
    assert manifest["implementation_sha256"] == "a" * 64
    assert verify_collector_readiness_audit(destination) == sha256_file(
        destination / "manifest.json"
    )


def test_readiness_audit_rejects_performance_budget_drift(tmp_path: Path) -> None:
    destination = tmp_path / "audit"
    write_collector_readiness_audit(_source(tmp_path / "source"), destination)
    report = destination / "rehearsal_report.json"
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["within_36_hour_budget"] = False
    _write_json(report, payload)

    try:
        verify_collector_readiness_audit(destination)
    except ValueError as error:
        assert "readiness audit hash changed" in str(error)
    else:
        raise AssertionError("drifted performance gate was accepted")
