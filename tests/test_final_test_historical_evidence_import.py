from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import polars as pl
import pytest

from ashare_multifactor.audit.publication import (
    PublishedRelease,
    publish_release,
    resolve_current,
)
from ashare_multifactor.audit.records import sha256_file
from ashare_multifactor.final_test.data_reuse_contract import (
    authorization_identity,
    canonical_reuse_json_bytes,
    validate_data_reuse_receipt,
)
from ashare_multifactor.final_test.final_root_binding import FinalRootBinding
from ashare_multifactor.final_test.gate import FinalTestAuthorization
from ashare_multifactor.final_test.official_evidence_import import (
    verify_historical_evidence_source_preparation,
)
from ashare_multifactor.final_test.preparation import (
    FinalTestPreparation,
    _publish_preparation,
    verify_preparation,
)
from ashare_multifactor.final_test.registry import (
    append_attempt_state,
    register_attempt,
)
from ashare_multifactor.robustness.change_impact import write_change_impact_audit
from ashare_multifactor.robustness.protocol_identities import identity_payload
from test_final_test_resume import PreparedAttempt


pytest_plugins = ("test_final_test_resume",)


@dataclass(frozen=True)
class HistoricalReuseScenario:
    final_root: Path
    robustness_root: Path
    source_release: PublishedRelease
    source_authorization: FinalTestAuthorization
    source_preparation: FinalTestPreparation
    destination_release: PublishedRelease
    destination_authorization: FinalTestAuthorization


def test_import_evidence_accepts_historical_v4_reused_panel_after_current_advances(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    scenario = _historical_reuse_scenario(prepared_attempt, tmp_path)

    source = verify_historical_evidence_source_preparation(
        scenario.final_root,
        attempt_id=scenario.source_authorization.attempt_id,
        authorization=scenario.source_authorization,
    )
    destination = verify_preparation(
        scenario.final_root,
        attempt_id=scenario.destination_authorization.attempt_id,
        authorization=scenario.destination_authorization,
    )

    assert source.attempt_id == "historical-reused-panel"
    assert destination.attempt_id == "current-destination"


def test_import_evidence_still_requires_destination_stage8_to_be_current(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    scenario = _historical_reuse_scenario(prepared_attempt, tmp_path)
    (scenario.robustness_root / "CURRENT.json").write_text(
        json.dumps(
            {
                "run_id": scenario.source_release.run_id,
                "manifest_sha256": scenario.source_release.manifest_sha256,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="final-test data reuse Stage-8 identity differs",
    ):
        verify_preparation(
            scenario.final_root,
            attempt_id=scenario.destination_authorization.attempt_id,
            authorization=scenario.destination_authorization,
        )


@pytest.mark.parametrize(
    "relative_path",
    (
        "manifest.json",
        "lineage.json",
        "artifacts/sealed_test_protocol.json",
    ),
)
def test_import_evidence_rejects_historical_source_bound_release_identity_drift(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
    relative_path: str,
) -> None:
    scenario = _historical_reuse_scenario(prepared_attempt, tmp_path)
    target = scenario.source_release.root / relative_path
    target.write_bytes(target.read_bytes() + b" ")

    with pytest.raises(ValueError):
        verify_historical_evidence_source_preparation(
            scenario.final_root,
            attempt_id=scenario.source_authorization.attempt_id,
            authorization=scenario.source_authorization,
        )


def test_import_evidence_rejects_unregistered_historical_source(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    scenario = _historical_reuse_scenario(prepared_attempt, tmp_path)
    unregistered = replace(
        scenario.source_authorization,
        attempt_id="unregistered-historical-source",
    )

    with pytest.raises(ValueError):
        verify_historical_evidence_source_preparation(
            scenario.final_root,
            attempt_id=unregistered.attempt_id,
            authorization=unregistered,
        )


def test_import_evidence_rejects_mismatched_historical_source_authorization(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    scenario = _historical_reuse_scenario(prepared_attempt, tmp_path)
    mismatched = replace(
        scenario.source_authorization,
        approval_id="different-approval",
    )

    with pytest.raises(ValueError, match="preparation authorization identity differs"):
        verify_historical_evidence_source_preparation(
            scenario.final_root,
            attempt_id=mismatched.attempt_id,
            authorization=mismatched,
        )


def _historical_reuse_scenario(
    attempt: PreparedAttempt,
    tmp_path: Path,
) -> HistoricalReuseScenario:
    final_root = attempt.data_root / "processed/final_test"
    robustness_root = attempt.data_root / "processed/robustness"
    owned_authorization = _authorization_from_registration(attempt)
    current = resolve_current(robustness_root)
    historical_release = _publish_v4_release(
        robustness_root,
        predecessor=current,
        staging_root=tmp_path / "historical-stage8",
        run_id="historical-v4",
    )
    historical_authorization = _register_authorization(
        final_root,
        release=historical_release,
        attempt_id="historical-reused-panel",
        approval_id="historical-reuse-approval",
        git_commit=owned_authorization.git_commit,
        git_tree=owned_authorization.git_tree,
        historical=True,
    )
    historical_preparation = _publish_reused_preparation(
        final_root,
        authorization=historical_authorization,
        release=historical_release,
    )
    assert verify_preparation(
        final_root,
        attempt_id=historical_authorization.attempt_id,
        authorization=historical_authorization,
    ) == historical_preparation

    destination_release = _clone_release(
        robustness_root,
        source=historical_release,
        staging_root=tmp_path / "destination-stage8",
        run_id="current-v4",
    )
    destination_authorization = _register_authorization(
        final_root,
        release=destination_release,
        attempt_id="current-destination",
        approval_id="current-destination-approval",
        git_commit=owned_authorization.git_commit,
        git_tree=owned_authorization.git_tree,
        historical=False,
    )
    _publish_reused_preparation(
        final_root,
        authorization=destination_authorization,
        release=destination_release,
    )
    return HistoricalReuseScenario(
        final_root=final_root,
        robustness_root=robustness_root,
        source_release=historical_release,
        source_authorization=historical_authorization,
        source_preparation=historical_preparation,
        destination_release=destination_release,
        destination_authorization=destination_authorization,
    )


def _authorization_from_registration(
    attempt: PreparedAttempt,
) -> FinalTestAuthorization:
    registry = attempt.data_root / "processed/final_test/attempts"
    record = json.loads(
        (registry / f"{attempt.attempt_id}.json").read_text(encoding="utf-8")
    )
    return FinalTestAuthorization(
        attempt_id=attempt.attempt_id,
        approval_id=str(record["approval_id"]),
        registered_at=str(record["registered_at"]),
        git_commit=str(record["git_commit"]),
        git_tree=str(record["git_tree"]),
        sealed_protocol_sha256=str(record["sealed_protocol_sha256"]),
        robustness_release=str(record["robustness_release"]),
        robustness_manifest_sha256=str(record["robustness_manifest_sha256"]),
        robustness_lineage_sha256=str(record["robustness_lineage_sha256"]),
        test_period=(date(2022, 1, 1), date(2025, 12, 31)),
    )


def _publish_v4_release(
    robustness_root: Path,
    *,
    predecessor: PublishedRelease,
    staging_root: Path,
    run_id: str,
) -> PublishedRelease:
    datasets = staging_root / "datasets"
    artifacts = staging_root / "artifacts"
    shutil.copytree(predecessor.datasets, datasets)
    shutil.copytree(predecessor.artifacts, artifacts)
    research = identity_payload("research_result_identity", {"fixture": "same"})
    final_execution = identity_payload(
        "final_execution_identity",
        {"fixture": "same"},
    )
    predecessor_identities = {
        "research": research,
        "final_execution": final_execution,
        "evidence_workflow": identity_payload(
            "evidence_workflow_identity",
            {"fixture": "owned"},
        ),
    }
    current_identities = {
        **predecessor_identities,
        "evidence_workflow": identity_payload(
            "evidence_workflow_identity",
            {"fixture": "historical-reuse"},
        ),
    }
    change_impact = artifacts / "change_impact_audit.json"
    write_change_impact_audit(
        change_impact,
        predecessor_identities=predecessor_identities,
        current_identities=current_identities,
    )
    seal_path = artifacts / "sealed_test_protocol.json"
    sealed = json.loads(seal_path.read_text(encoding="utf-8"))
    sealed.update(
        {
            "protocol_version": 4,
            "protocol_identities": current_identities,
            "predecessor_final_execution_identity": final_execution,
            "change_impact_audit_sha256": sha256_file(change_impact),
            "evidence_workflow_readiness_audit_sha256": "f" * 64,
        }
    )
    sealed.pop("sealed_protocol_sha256", None)
    canonical = json.dumps(
        sealed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    sealed["sealed_protocol_sha256"] = hashlib.sha256(canonical).hexdigest()
    seal_path.write_text(
        json.dumps(sealed, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lineage = json.loads(predecessor.lineage.read_text(encoding="utf-8"))
    return publish_release(
        robustness_root,
        run_id=run_id,
        staged_datasets=datasets,
        staged_artifacts=artifacts,
        lineage=lineage,
    )


def _clone_release(
    robustness_root: Path,
    *,
    source: PublishedRelease,
    staging_root: Path,
    run_id: str,
) -> PublishedRelease:
    datasets = staging_root / "datasets"
    artifacts = staging_root / "artifacts"
    shutil.copytree(source.datasets, datasets)
    shutil.copytree(source.artifacts, artifacts)
    return publish_release(
        robustness_root,
        run_id=run_id,
        staged_datasets=datasets,
        staged_artifacts=artifacts,
        lineage=json.loads(source.lineage.read_text(encoding="utf-8")),
    )


def _register_authorization(
    final_root: Path,
    *,
    release: PublishedRelease,
    attempt_id: str,
    approval_id: str,
    git_commit: str,
    git_tree: str,
    historical: bool,
) -> FinalTestAuthorization:
    registry = final_root / "attempts"
    sealed = json.loads(
        (release.artifacts / "sealed_test_protocol.json").read_text(encoding="utf-8")
    )
    token = f"{attempt_id}\n".encode()
    token_sha256 = hashlib.sha256(token).hexdigest()
    if historical:
        (registry / f"{attempt_id}.token").write_bytes(token)
    record = register_attempt(
        registry,
        attempt_id=attempt_id,
        git_commit=git_commit,
        git_tree=git_tree,
        token_sha256=token_sha256,
        sealed_protocol_sha256=str(sealed["sealed_protocol_sha256"]),
        robustness_release=release.run_id,
        robustness_manifest_sha256=release.manifest_sha256,
        robustness_lineage_sha256=sha256_file(release.lineage),
        approval_id=approval_id,
    )
    authorization = FinalTestAuthorization(
        attempt_id=attempt_id,
        approval_id=approval_id,
        registered_at=str(record["registered_at"]),
        git_commit=git_commit,
        git_tree=git_tree,
        sealed_protocol_sha256=str(sealed["sealed_protocol_sha256"]),
        robustness_release=release.run_id,
        robustness_manifest_sha256=release.manifest_sha256,
        robustness_lineage_sha256=sha256_file(release.lineage),
        test_period=(date(2022, 1, 1), date(2025, 12, 31)),
    )
    if historical:
        ledger_root = Path(str(sealed["opening_ledger_root"]))
        ledger_root.mkdir(parents=True, exist_ok=True)
        (ledger_root / f"{authorization.sealed_protocol_sha256}.json").write_text(
            json.dumps(
                {
                    "status": "consumed",
                    "attempt_id": attempt_id,
                    "approval_id": approval_id,
                    "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
                    "robustness_release": release.run_id,
                    "robustness_manifest_sha256": release.manifest_sha256,
                    "robustness_lineage_sha256": sha256_file(release.lineage),
                    "token_sha256": token_sha256,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return authorization


def _publish_reused_preparation(
    final_root: Path,
    *,
    authorization: FinalTestAuthorization,
    release: PublishedRelease,
) -> FinalTestPreparation:
    panel_root = final_root / "daily_panel"
    data_manifest = panel_root / "data_manifest.json"
    input_inventory = panel_root / "input_files.json"
    if not input_inventory.exists():
        input_inventory.write_text(
            '{"fixture":"immutable-panel-inventory"}\n',
            encoding="utf-8",
        )
    claim_bytes = (final_root / "data-build-claim.json").read_bytes()
    claim = json.loads(claim_bytes)
    sealed = json.loads(
        (release.artifacts / "sealed_test_protocol.json").read_text(encoding="utf-8")
    )
    identities = sealed["protocol_identities"]
    resolution = SimpleNamespace(
        root=panel_root,
        claim_status="published",
        requires_recovery=False,
        data_manifest_sha256=sha256_file(data_manifest),
    )
    receipt = {
        "schema_version": "1",
        "role": "final_test_data_reuse",
        "status": "reused_verified_immutable_panel",
        **authorization_identity(authorization),
        "source_attempt_id": str(claim["attempt_id"]),
        "source_claim_sha256": hashlib.sha256(claim_bytes).hexdigest(),
        "source_claim_size_bytes": len(claim_bytes),
        "data_manifest_sha256": resolution.data_manifest_sha256,
        "input_inventory_sha256": sha256_file(input_inventory),
        "input_inventory_size_bytes": input_inventory.stat().st_size,
        "protocol_version": 4,
        "protocol_identity_sha256": {
            domain: str(identity["sha256"])
            for domain, identity in identities.items()
        },
        "change_impact_audit_sha256": str(
            sealed["change_impact_audit_sha256"]
        ),
        "created_at": authorization.registered_at,
    }
    validate_data_reuse_receipt(
        receipt,
        authorization=authorization,
        source_attempt_id=str(claim["attempt_id"]),
        source_claim_sha256=hashlib.sha256(claim_bytes).hexdigest(),
        source_claim_size_bytes=len(claim_bytes),
        data_manifest_sha256=resolution.data_manifest_sha256,
        input_inventory_sha256=sha256_file(input_inventory),
        input_inventory_size_bytes=input_inventory.stat().st_size,
        sealed_protocol=sealed,
    )
    receipt_bytes = canonical_reuse_json_bytes(receipt)
    receipt_path = final_root / "data-reuse" / f"{authorization.attempt_id}.json"
    receipt_path.parent.mkdir(exist_ok=True)
    receipt_path.write_bytes(receipt_bytes)
    symbols = (
        pl.read_parquet(final_root / "preparations/attempt-001/symbol_scope.parquet")
        .get_column("symbol")
        .cast(pl.String)
        .to_list()
    )
    with FinalRootBinding.open(final_root) as binding:
        preparation = _publish_preparation(
            final_root,
            authorization=authorization,
            resolution=resolution,
            symbols=symbols,
            data_binding={
                "mode": "verified_reuse",
                "relative_path": (
                    f"data-reuse/{authorization.attempt_id}.json"
                ),
                "sha256": hashlib.sha256(receipt_bytes).hexdigest(),
                "size_bytes": len(receipt_bytes),
            },
            root_binding=binding,
        )
    registry = final_root / "attempts"
    append_attempt_state(registry, attempt_id=authorization.attempt_id, state="preparing")
    append_attempt_state(
        registry,
        attempt_id=authorization.attempt_id,
        state="awaiting_official_evidence",
        identities={"prepare_manifest_sha256": preparation.manifest_sha256},
    )
    return preparation
