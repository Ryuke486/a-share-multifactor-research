from __future__ import annotations

from dataclasses import replace
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_build import _config

from ashare_multifactor.final_test import data_reuse as data_reuse_module
from ashare_multifactor.final_test import preparation as preparation_module
from ashare_multifactor.final_test.data_reuse import bind_final_test_data_panel
from ashare_multifactor.final_test.data_reuse_contract import (
    validate_data_reuse_receipt,
)
from ashare_multifactor.final_test.final_root_binding import FinalRootBinding
from ashare_multifactor.final_test.gate import FinalTestAuthorization
from ashare_multifactor.robustness.protocol_identities import identity_payload


def _authorization() -> FinalTestAuthorization:
    return FinalTestAuthorization(
        attempt_id="destination-attempt",
        approval_id="destination-approval",
        registered_at="2026-07-27T12:00:00+08:00",
        git_commit="1" * 40,
        git_tree="2" * 40,
        sealed_protocol_sha256="3" * 64,
        robustness_release="stage8-v4",
        robustness_manifest_sha256="4" * 64,
        robustness_lineage_sha256="5" * 64,
        test_period=(date(2022, 1, 1), date(2025, 12, 31)),
    )


def _identities() -> dict[str, dict[str, object]]:
    return {
        "research": identity_payload("research_result_identity", {"result": "same"}),
        "final_execution": identity_payload("final_execution_identity", {"code": "same"}),
        "evidence_workflow": identity_payload(
            "evidence_workflow_identity",
            {"code": "v4"},
        ),
    }


def _receipt() -> dict[str, object]:
    authorization = _authorization()
    identities = _identities()
    return {
        "schema_version": "1",
        "role": "final_test_data_reuse",
        "status": "reused_verified_immutable_panel",
        "attempt_id": authorization.attempt_id,
        "approval_id": authorization.approval_id,
        "git_commit": authorization.git_commit,
        "git_tree": authorization.git_tree,
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "robustness_release": authorization.robustness_release,
        "robustness_manifest_sha256": authorization.robustness_manifest_sha256,
        "robustness_lineage_sha256": authorization.robustness_lineage_sha256,
        "source_attempt_id": "source-attempt",
        "source_claim_sha256": "6" * 64,
        "source_claim_size_bytes": 123,
        "data_manifest_sha256": "7" * 64,
        "input_inventory_sha256": "8" * 64,
        "input_inventory_size_bytes": 456,
        "protocol_version": 4,
        "protocol_identity_sha256": {
            domain: str(identity["sha256"])
            for domain, identity in identities.items()
        },
        "change_impact_audit_sha256": "9" * 64,
        "created_at": authorization.registered_at,
    }


def test_data_reuse_receipt_binds_source_claim_panel_and_v4_protocol() -> None:
    receipt = _receipt()

    verified = validate_data_reuse_receipt(
        receipt,
        authorization=_authorization(),
        source_attempt_id="source-attempt",
        source_claim_sha256="6" * 64,
        source_claim_size_bytes=123,
        data_manifest_sha256="7" * 64,
        input_inventory_sha256="8" * 64,
        input_inventory_size_bytes=456,
        sealed_protocol={
            "protocol_version": 4,
            "protocol_identities": _identities(),
            "change_impact_audit_sha256": "9" * 64,
        },
    )

    assert verified == receipt


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("source_claim_sha256", "a" * 64, "source claim"),
        ("data_manifest_sha256", "b" * 64, "data manifest"),
        ("input_inventory_sha256", "c" * 64, "input inventory"),
        ("protocol_version", 3, "protocol v4"),
    ],
)
def test_data_reuse_receipt_rejects_identity_drift(
    field: str,
    replacement: object,
    message: str,
) -> None:
    receipt = _receipt()
    receipt[field] = replacement

    with pytest.raises(ValueError, match=message):
        validate_data_reuse_receipt(
            receipt,
            authorization=_authorization(),
            source_attempt_id="source-attempt",
            source_claim_sha256="6" * 64,
            source_claim_size_bytes=123,
            data_manifest_sha256="7" * 64,
            input_inventory_sha256="8" * 64,
            input_inventory_size_bytes=456,
            sealed_protocol={
                "protocol_version": 4,
                "protocol_identities": _identities(),
                "change_impact_audit_sha256": "9" * 64,
            },
        )


def test_foreign_published_claim_gets_v4_reuse_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    final_root = config.paths.processed / "final_test"
    (final_root / "attempts").mkdir(parents=True)
    panel = final_root / "daily_panel"
    panel.mkdir()
    inventory = {
        "schema_version": "1",
        "period": ["2022-01-01", "2025-12-31"],
        "pairs": [{"trade_date": "2022-01-04", "files": []}],
    }
    (panel / "input_files.json").write_text(
        json.dumps(inventory),
        encoding="utf-8",
    )
    destination = _authorization()
    source = replace(
        destination,
        attempt_id="source-attempt",
        approval_id="source-approval",
        git_commit="a" * 40,
        git_tree="b" * 40,
        sealed_protocol_sha256="c" * 64,
        robustness_release="stage8-v3",
        robustness_manifest_sha256="d" * 64,
        robustness_lineage_sha256="e" * 64,
    )
    resolution = SimpleNamespace(
        root=panel,
        claim_status="published",
        requires_recovery=False,
        data_manifest_sha256="7" * 64,
    )
    (final_root / "data-build-claim.json").write_text(
        json.dumps(
            {
                "attempt_id": source.attempt_id,
                "approval_id": source.approval_id,
                "git_commit": source.git_commit,
                "git_tree": source.git_tree,
                "sealed_protocol_sha256": source.sealed_protocol_sha256,
                "robustness_release": source.robustness_release,
                "robustness_manifest_sha256": source.robustness_manifest_sha256,
                "robustness_lineage_sha256": source.robustness_lineage_sha256,
                "status": "published",
                "data_manifest": {
                    "relative_path": "daily_panel/data_manifest.json",
                    "sha256": resolution.data_manifest_sha256,
                    "size_bytes": 10,
                },
            }
        ),
        encoding="utf-8",
    )
    sealed = {
        "protocol_version": 4,
        "protocol_identities": _identities(),
        "change_impact_audit_sha256": "9" * 64,
    }
    monkeypatch.setattr(
        data_reuse_module,
        "load_historical_attempt_authorization",
        lambda **_kwargs: source,
    )
    monkeypatch.setattr(
        data_reuse_module,
        "_load_destination_protocol",
        lambda *_args, **_kwargs: sealed,
    )
    monkeypatch.setattr(
        data_reuse_module,
        "build_input_inventory",
        lambda *_args, **_kwargs: inventory,
    )

    with FinalRootBinding.open(final_root) as root_binding:
        binding = bind_final_test_data_panel(
            config,
            destination,
            resolution,
            code_root=tmp_path,
            root_binding=root_binding,
        )

    receipt_path = final_root / str(binding["relative_path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert binding["mode"] == "verified_reuse"
    assert receipt["source_attempt_id"] == source.attempt_id
    assert receipt["attempt_id"] == destination.attempt_id
    assert receipt["protocol_version"] == 4


def test_preparation_reuses_foreign_published_panel_instead_of_rebuilding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    final_root = config.paths.processed / "final_test"
    (final_root / "attempts").mkdir(parents=True)
    (final_root / "data-build-claim.json").write_text(
        '{"attempt_id":"source-attempt","status":"published"}\n',
        encoding="utf-8",
    )
    destination = _authorization()
    resolution = SimpleNamespace(
        root=final_root / "daily_panel",
        claim_status="published",
        requires_recovery=False,
        data_manifest_sha256="7" * 64,
    )
    binding = {
        "mode": "verified_reuse",
        "relative_path": "data-reuse/destination-attempt.json",
        "sha256": "8" * 64,
        "size_bytes": 1,
    }
    monkeypatch.setattr(
        preparation_module,
        "resolve_final_test_data_panel_at",
        lambda *_args, **_kwargs: resolution,
    )
    monkeypatch.setattr(
        preparation_module,
        "recover_final_test_daily_panel",
        lambda *_args, **_kwargs: pytest.fail("foreign claim must not be recovered"),
    )
    monkeypatch.setattr(
        preparation_module,
        "build_final_test_daily_panel",
        lambda *_args, **_kwargs: pytest.fail("published panel must not be rebuilt"),
    )
    monkeypatch.setattr(
        preparation_module,
        "bind_final_test_data_panel",
        lambda *_args, **_kwargs: binding,
    )

    with FinalRootBinding.open(final_root) as root_binding:
        actual = preparation_module._resolve_or_build_data_panel(
            config,
            destination,
            code_root=tmp_path,
            final_root=final_root,
            root_binding=root_binding,
        )

    assert actual == (resolution, binding)
