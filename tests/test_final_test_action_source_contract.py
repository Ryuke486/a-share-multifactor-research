from datetime import date
import hashlib
import json
from pathlib import Path

import polars as pl
import pytest

from ashare_multifactor.final_test.action_source_contract import (
    assert_allowed_evidence_url,
    build_execution_input_manifest,
    load_action_source_contract,
    official_query_scope,
    verify_execution_input_manifest,
)
from ashare_multifactor.final_test.gate import FinalTestAuthorization
from ashare_multifactor.final_test.official_query_coverage import (
    OFFICIAL_QUERY_ENDPOINT,
    OfficialQueryScope,
)


def _authorization() -> FinalTestAuthorization:
    return FinalTestAuthorization(
        attempt_id="attempt-001",
        approval_id="approved-stage9",
        registered_at="2026-07-16T00:00:00+00:00",
        git_commit="a" * 40,
        git_tree="b" * 40,
        sealed_protocol_sha256="c" * 64,
        robustness_release="stage8-successor",
        test_period=(date(2022, 1, 1), date(2025, 12, 31)),
    )


def _write_inputs(root: Path) -> dict[str, Path]:
    action = root / "corporate_actions.parquet"
    event = root / "security_events.parquet"
    pl.DataFrame(
        {"effective_date": [date(2022, 6, 1)], "symbol": ["000001"]}
    ).write_parquet(action)
    pl.DataFrame(
        schema={"effective_date": pl.Date, "source_symbol": pl.String}
    ).write_parquet(event)
    return {
        "corporate_actions.parquet": action,
        "security_events.parquet": event,
    }


def _execution_identity() -> dict[str, str]:
    return {
        "attempt_id": "attempt-001",
        "execution_id": "execution-001",
        "sealed_protocol_sha256": "c" * 64,
        "prepare_manifest_sha256": "d" * 64,
        "security_event_coverage_sha256": "e" * 64,
        "corporate_action_coverage_sha256": "f" * 64,
        "coverage_snapshot_manifest_sha256": "0" * 64,
    }


def test_manifest_binds_attempt_seal_period_and_hashed_files(tmp_path: Path) -> None:
    files = _write_inputs(tmp_path)
    manifest_path = tmp_path / "manifest.json"

    manifest = build_execution_input_manifest(
        manifest_path,
        authorization=_authorization(),
        files=files,
    )
    verified = verify_execution_input_manifest(manifest_path, _authorization())

    assert manifest["attempt_id"] == "attempt-001"
    assert manifest["sealed_protocol_sha256"] == "c" * 64
    assert manifest["period"] == ["2022-01-01", "2025-12-31"]
    assert {item["path"] for item in manifest["files"]} == set(files)
    assert all(len(item["sha256"]) == 64 for item in manifest["files"])
    assert verified == {name: path.resolve() for name, path in files.items()}


def test_manifest_requires_exact_bound_execution_identity(tmp_path: Path) -> None:
    files = _write_inputs(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    identity = _execution_identity()
    build_execution_input_manifest(
        manifest_path,
        authorization=_authorization(),
        files=files,
        execution_identity=identity,
    )

    assert verify_execution_input_manifest(
        manifest_path,
        _authorization(),
        execution_identity=identity,
    ) == {name: path.resolve() for name, path in files.items()}

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["coverage_snapshot_manifest_sha256"] = "1" * 64
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="execution identity"):
        verify_execution_input_manifest(
            manifest_path,
            _authorization(),
            execution_identity=identity,
        )


def test_manifest_rejects_hash_drift_and_path_escape(tmp_path: Path) -> None:
    files = _write_inputs(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    build_execution_input_manifest(
        manifest_path,
        authorization=_authorization(),
        files=files,
    )
    files["corporate_actions.parquet"].write_bytes(b"tampered")

    with pytest.raises(ValueError, match="digest mismatch"):
        verify_execution_input_manifest(manifest_path, _authorization())

    outside_root = tmp_path.parent / "outside"
    outside_root.mkdir()
    outside = outside_root / "corporate_actions.parquet"
    outside.write_bytes(b"outside")
    with pytest.raises(ValueError, match="inside execution-input root"):
        build_execution_input_manifest(
            manifest_path,
            authorization=_authorization(),
            files={
                "corporate_actions.parquet": outside,
                "security_events.parquet": files["security_events.parquet"],
            },
        )


def test_source_contract_freezes_scope_and_official_domains() -> None:
    contract = load_action_source_contract(
        Path("configs/final_execution_sources.yaml")
    )

    assert contract.start == date(2022, 1, 1)
    assert contract.end == date(2025, 12, 31)
    assert contract.query_years == (2022, 2023, 2024, 2025)
    assert contract.query_year_type == "operate"
    assert dict(contract.market_sources) == {
        "sh": "sse",
        "sz": "szse",
    }
    assert contract.supported_markets == ("sh", "sz")
    assert contract.official_query_endpoint == OFFICIAL_QUERY_ENDPOINT
    assert contract.official_query_method == "POST"
    assert dict(contract.official_query_categories) == {
        "corporate_actions": "",
        "security_events": "",
    }
    assert official_query_scope(
        contract,
        symbol="000001",
        category="corporate_actions",
    ) == OfficialQueryScope(
        symbol="000001",
        market="sz",
        category="corporate_actions",
        query_category="",
        start=date(2022, 1, 1),
        end=date(2025, 12, 31),
    )
    with pytest.raises(ValueError, match="query category"):
        official_query_scope(
            contract,
            symbol="000001",
            category="other_events",
        )
    assert_allowed_evidence_url(
        "https://static.cninfo.com.cn/finalpage/2024-05-10/notice.PDF",
        contract,
    )
    assert_allowed_evidence_url(
        "https://www.sse.com.cn/assortment/stock/list/info/profit/"
        "index.shtml?COMPANY_CODE=600276",
        contract,
    )
    with pytest.raises(ValueError, match="official evidence URL"):
        assert_allowed_evidence_url(
            "https://www.bse.cn/disclosure/2025/2025-01-15/notice.pdf",
            contract,
        )
    with pytest.raises(ValueError, match="official evidence URL"):
        assert_allowed_evidence_url("https://example.com/notice.pdf", contract)
    with pytest.raises(ValueError, match="official evidence URL"):
        assert_allowed_evidence_url("https://www.bse.cn.evil/disclosure/a.pdf", contract)
    with pytest.raises(ValueError, match="official evidence URL"):
        assert_allowed_evidence_url(OFFICIAL_QUERY_ENDPOINT, contract)


def test_supported_market_contract_is_protocol_hash_bound(tmp_path: Path) -> None:
    source = Path("configs/final_execution_sources.yaml")
    original = source.read_bytes()
    without_scope = original.replace(b"supported_markets: [sh, sz]\n", b"")
    altered = tmp_path / "final_execution_sources.yaml"
    altered.write_bytes(without_scope)

    assert hashlib.sha256(original).digest() != hashlib.sha256(without_scope).digest()
    with pytest.raises(ValueError, match="frozen final execution source contract"):
        load_action_source_contract(altered)
