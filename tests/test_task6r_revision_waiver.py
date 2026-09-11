from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from task6r_revision_waiver import (
    Task6RWaiverError,
    build_external_source_revision_waiver,
    map_verified_baostock_identity,
)


OLD_HASH = "f" * 64
NEW_HASH = "4" * 64
NEW_BYTES_HASH = "21660f861aadc5731025b92effc05351d10444636aa4fbdb6e1c78b7a9f40324"
CERTIFIED_RUN_IDS = (
    "stage7_controlled_65b19e1_1",
    "stage7_controlled_65b19e1_2",
)


def _write_consumer_tables(root: Path, *, changed: bool = False) -> None:
    root.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "symbol": ["000001", "000001"],
            "effective_date": ["2021-06-01", "2021-06-02"],
            "cash_per_share": [0.1, 0.2 if not changed else 0.3],
        }
    ).write_parquet(root / "corporate_actions.parquet")
    pl.DataFrame(
        {
            "source_symbol": ["000979"],
            "event_type": ["write_off"],
        }
    ).write_parquet(root / "security_events.parquet")


def _historical_roots(tmp_path: Path) -> tuple[Path, Path]:
    roots = tuple(tmp_path / run_id / "datasets" for run_id in CERTIFIED_RUN_IDS)
    for root in roots:
        _write_consumer_tables(root)
    return roots


def _download_evidence(
    *,
    sha256: str = NEW_BYTES_HASH,
    size: int = len(b"new-provider-bytes"),
    query_count: int = 13_590,
) -> dict[str, object]:
    serial_access = {
        "process_count": 1,
        "session_count": 1,
        "query_count": query_count,
        "nonzero_error_count": 0,
        "login_succeeded": True,
        "logout_succeeded": True,
    }
    return {
        "source": "baostock_query_dividend_data",
        "baostock_version": "00.9.30",
        "query_year_type": "operate",
        "query_years": [2017, 2018, 2019, 2020, 2021],
        "symbol_count": 2_718,
        "successful_query_count": query_count,
        "row_count": 9_516,
        "column_count": 16,
        "new_raw_identity": {"sha256": sha256, "size": size},
        "coverage_sha256": "1" * 64,
        "download_log_sha256": "2" * 64,
        "serial_access": serial_access,
    }


def _complete_waiver(
    actual: dict[str, object],
    certified: dict[str, object],
) -> dict[str, object]:
    evidence = _download_evidence(
        sha256=str(actual["baostock_dividends"]["sha256"]),
        size=int(actual["baostock_dividends"]["size"]),
    )
    return {
        "schema_version": 1,
        "scope": "candidate1_task6_shadow_acceptance_only",
        "status": "passed",
        "consumer_semantic_differences": 0,
        "historical_raw_bytes_recovered": False,
        "publication_performed": False,
        "production_adapter_modified": False,
        "historical_raw_identity": certified["baostock_dividends"],
        "new_raw_identity": actual["baostock_dividends"],
        "certified_run_ids": list(CERTIFIED_RUN_IDS),
        "download_evidence": evidence,
        "serial_access": evidence["serial_access"],
        "consumer_tables": {
            name: {
                "historical_run_matches": [True, True],
                "semantic_difference_count": 0,
            }
            for name in ("corporate_actions.parquet", "security_events.parquet")
        },
    }


def test_task6r_builds_a_waiver_only_for_exact_consumer_multisets(
    tmp_path: Path,
) -> None:
    historical, historical_two = _historical_roots(tmp_path)
    regenerated = tmp_path / "regenerated"
    _write_consumer_tables(regenerated)
    raw = tmp_path / "new.parquet"
    raw.write_bytes(b"new-provider-bytes")

    waiver = build_external_source_revision_waiver(
        historical_roots=[historical, historical_two],
        regenerated_root=regenerated,
        new_raw_path=raw,
        historical_raw_identity={"sha256": OLD_HASH, "size": 177_037},
        download_evidence=_download_evidence(),
    )

    assert waiver["status"] == "passed"
    assert waiver["consumer_semantic_differences"] == 0
    assert waiver["historical_raw_bytes_recovered"] is False
    assert waiver["publication_performed"] is False
    assert waiver["consumer_tables"]["corporate_actions.parquet"]["historical_run_matches"] == [
        True,
        True,
    ]


def test_task6r_rejects_any_consumer_field_difference(tmp_path: Path) -> None:
    historical, historical_two = _historical_roots(tmp_path)
    regenerated = tmp_path / "regenerated"
    _write_consumer_tables(regenerated, changed=True)
    raw = tmp_path / "new.parquet"
    raw.write_bytes(b"new-provider-bytes")

    with pytest.raises(
        Task6RWaiverError,
        match="consumer table differs: corporate_actions.parquet",
    ):
        build_external_source_revision_waiver(
            historical_roots=[historical, historical_two],
            regenerated_root=regenerated,
            new_raw_path=raw,
            historical_raw_identity={"sha256": OLD_HASH, "size": 177_037},
            download_evidence=_download_evidence(),
        )


def test_task6r_rejects_a_new_raw_hash_that_differs_from_download_evidence(
    tmp_path: Path,
) -> None:
    historical, historical_two = _historical_roots(tmp_path)
    regenerated = tmp_path / "regenerated"
    _write_consumer_tables(regenerated)
    raw = tmp_path / "new.parquet"
    raw.write_bytes(b"new-provider-bytes")

    with pytest.raises(
        Task6RWaiverError,
        match="new BaoStock raw identity differs from download evidence",
    ):
        build_external_source_revision_waiver(
            historical_roots=[historical, historical_two],
            regenerated_root=regenerated,
            new_raw_path=raw,
            historical_raw_identity={"sha256": OLD_HASH, "size": 177_037},
            download_evidence=_download_evidence(sha256="0" * 64),
        )


def test_task6r_rejects_incomplete_query_coverage(tmp_path: Path) -> None:
    historical = _historical_roots(tmp_path)
    regenerated = tmp_path / "regenerated"
    _write_consumer_tables(regenerated)
    raw = tmp_path / "new.parquet"
    raw.write_bytes(b"new-provider-bytes")

    with pytest.raises(
        Task6RWaiverError,
        match="BaoStock download evidence is incomplete",
    ):
        build_external_source_revision_waiver(
            historical_roots=list(historical),
            regenerated_root=regenerated,
            new_raw_path=raw,
            historical_raw_identity={"sha256": OLD_HASH, "size": 177_037},
            download_evidence=_download_evidence(query_count=1),
        )


def test_task6r_rejects_missing_login_success_evidence(tmp_path: Path) -> None:
    historical = _historical_roots(tmp_path)
    regenerated = tmp_path / "regenerated"
    _write_consumer_tables(regenerated)
    raw = tmp_path / "new.parquet"
    raw.write_bytes(b"new-provider-bytes")
    evidence = _download_evidence()
    del evidence["serial_access"]["login_succeeded"]

    with pytest.raises(
        Task6RWaiverError,
        match="BaoStock download evidence is incomplete",
    ):
        build_external_source_revision_waiver(
            historical_roots=list(historical),
            regenerated_root=regenerated,
            new_raw_path=raw,
            historical_raw_identity={"sha256": OLD_HASH, "size": 177_037},
            download_evidence=evidence,
        )


def test_task6r_identity_mapping_rejects_non_baostock_drift() -> None:
    certified = {
        "baostock_dividends": {"sha256": OLD_HASH, "size": 177_037},
        "research_protocol": {"sha256": "a" * 64, "size": 10},
    }
    actual = {
        "baostock_dividends": {"sha256": NEW_HASH, "size": 175_512},
        "research_protocol": {"sha256": "b" * 64, "size": 10},
    }
    waiver = _complete_waiver(actual, certified)

    with pytest.raises(
        Task6RWaiverError,
        match="non-BaoStock certified input identity differs",
    ):
        map_verified_baostock_identity(actual, certified, waiver)


def test_task6r_identity_mapping_changes_only_the_approved_raw_identity() -> None:
    certified = {
        "baostock_dividends": {"sha256": OLD_HASH, "size": 177_037},
        "research_protocol": {"sha256": "a" * 64, "size": 10},
    }
    actual = {
        "baostock_dividends": {"sha256": NEW_HASH, "size": 175_512},
        "research_protocol": {"sha256": "a" * 64, "size": 10},
    }
    waiver = _complete_waiver(actual, certified)

    assert map_verified_baostock_identity(actual, certified, waiver) == certified


def test_task6r_identity_mapping_rejects_an_incomplete_waiver() -> None:
    certified = {
        "baostock_dividends": {"sha256": OLD_HASH, "size": 177_037},
        "research_protocol": {"sha256": "a" * 64, "size": 10},
    }
    actual = {
        "baostock_dividends": {"sha256": NEW_HASH, "size": 175_512},
        "research_protocol": {"sha256": "a" * 64, "size": 10},
    }
    incomplete = {
        "status": "passed",
        "consumer_semantic_differences": 0,
        "historical_raw_bytes_recovered": False,
        "publication_performed": False,
        "historical_raw_identity": certified["baostock_dividends"],
        "new_raw_identity": actual["baostock_dividends"],
    }

    with pytest.raises(
        Task6RWaiverError,
        match="external source revision waiver is not approved",
    ):
        map_verified_baostock_identity(actual, certified, incomplete)


def _task6s_receipt():
    import json

    return json.loads(
        (
            Path(__file__).parents[1] / "docs/audits/"
            "2026-08-27-candidate1-task6s-external-source-revision-waiver.json"
        ).read_text()
    )


def test_task6s_delivered_receipt_has_its_own_mapping_gate():
    from copy import deepcopy
    from task6r_revision_waiver import map_task6s_baostock_identity

    waiver = _task6s_receipt()
    before = deepcopy(waiver)
    actual = {"baostock_dividends": waiver["new_raw_identity"], "other": "frozen"}
    certified = {"baostock_dividends": waiver["historical_raw_identity"], "other": "frozen"}
    with pytest.raises(Task6RWaiverError):
        map_verified_baostock_identity(actual, certified, waiver)
    assert map_task6s_baostock_identity(actual, certified, waiver) == certified
    assert waiver == before
    actual["other"] = "drift"
    with pytest.raises(Task6RWaiverError):
        map_task6s_baostock_identity(actual, certified, waiver)


@pytest.mark.parametrize(
    "path,value",
    [
        (("production_adapter_modified",), False),
        (("publication_performed",), True),
        (("consumer_semantic_differences",), 2),
        (("remediation", "cash_correction_provenance_covered"), False),
        (("remediation", "share_correction_provenance_covered"), False),
        (("remediation", "dates_amounts_share_ratios_and_accounting_unchanged"), False),
        (("remediation", "post_review_certified_run_semantic_differences"), [0, 1]),
        (("shadow_acceptance", "stage7", "publish"), True),
        (("shadow_acceptance", "stage7", "core_hash_mismatches"), 1),
        (("shadow_acceptance", "stage8", "opening_token_status"), "open"),
        (("shadow_acceptance", "stage8", "action_coverage_tree_equal"), False),
        (("shadow_acceptance", "stage8", "collector_readiness_tree_equal"), False),
        (("shadow_acceptance", "input_identity_difference_keys"), ["other"]),
        (("consumer_tables", "corporate_actions.parquet", "historical_run_matches"), [True, False]),
        (("download_evidence", "successful_query_count"), 1),
    ],
)
def test_task6s_rejects_broadened_or_incomplete_receipt(path, value):
    from task6r_revision_waiver import map_task6s_baostock_identity

    waiver = _task6s_receipt()
    node = waiver
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(Task6RWaiverError):
        map_task6s_baostock_identity(
            {"baostock_dividends": waiver["new_raw_identity"]},
            {"baostock_dividends": waiver["historical_raw_identity"]},
            waiver,
        )
