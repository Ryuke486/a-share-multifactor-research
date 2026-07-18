from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
import inspect
import json
import hashlib
from io import BytesIO
from pathlib import Path
import shutil
from types import SimpleNamespace

import polars as pl
import pytest

from test_final_test_resume import PreparedAttempt

from ashare_multifactor.audit.publication import publish_release, resolve_current
from ashare_multifactor.audit.records import file_record, sha256_file
from ashare_multifactor.data.security import market_for_symbol
from ashare_multifactor.final_test.backtest import FinalTestBacktestResult
from ashare_multifactor.final_test.action_source_contract import (
    build_execution_input_manifest,
)
from ashare_multifactor.final_test import pipeline as pipeline_module
from ashare_multifactor.final_test import secure_attempt_staging as staging_module
from ashare_multifactor.final_test import coverage_snapshot as coverage_module
from ashare_multifactor.final_test import interrupted_recovery as recovery_module
from ashare_multifactor.final_test.coverage_snapshot import (
    snapshot_execution_coverages,
)
from ashare_multifactor.final_test.gate import FinalTestAuthorization
from ashare_multifactor.final_test.execution_binding import BoundExecutionInputs
from ashare_multifactor.final_test.execution_sources import (
    _normalize_final_dividends,
    _validate_coverage_markets,
    _validate_event_counts,
    validate_security_event_coverage,
)
from ashare_multifactor.final_test.pipeline import (
    _assert_reusable_data_claim,
    _assert_release_date_bounds,
    _assert_safe_roots,
    _copy_release_inputs,
    _recover_or_archive_data_claim,
    _slice_audit_frame,
    _validate_publication_id,
    run_final_test_release,
)
from ashare_multifactor.final_test.registry import (
    append_attempt_state,
    append_prepared_publication,
    begin_execution_recovery,
    bind_execution_identity,
    recover_prepared_publication,
    register_attempt,
    resolve_attempt_state,
    resolve_execution_binding,
    resolve_execution_input_manifest_hash,
)
from ashare_multifactor.final_test.official_query_coverage import OfficialQueryScope
from ashare_multifactor.final_test.resume import preflight_resume
from ashare_multifactor.final_test.release_outputs import (
    _historical_backtest_root,
    _resolve_historical_releases,
    _resolve_authorized_robustness,
    _slice_backtest_period,
)
from ashare_multifactor.final_test.signals import FinalTestSignals
from test_final_test_official_query_index import _write_index


pytest_plugins = ("test_final_test_resume",)


@pytest.mark.parametrize("changed", ["manifest.json", "lineage.json"])
def test_authorized_stage8_identity_rejects_post_authorization_change(
    tmp_path: Path, changed: str
) -> None:
    datasets = tmp_path / "datasets"
    artifacts = tmp_path / "artifacts"
    datasets.mkdir()
    artifacts.mkdir()
    (artifacts / "sealed_test_protocol.json").write_text("{}")
    release = publish_release(
        tmp_path / "processed/robustness", run_id="stage8-successor",
        staged_datasets=datasets, staged_artifacts=artifacts,
        lineage={"stage": "robustness"},
    )
    authorization = _authorization()
    authorization = type(authorization)(
        **{
            **authorization.__dict__,
            "robustness_manifest_sha256": release.manifest_sha256,
            "robustness_lineage_sha256": hashlib.sha256(
                release.lineage.read_bytes()
            ).hexdigest(),
        }
    )
    (release.root / changed).write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="authorized Stage-8"):
        _resolve_authorized_robustness(tmp_path, authorization)


def _authorization(attempt_id: str = "attempt-001") -> FinalTestAuthorization:
    return FinalTestAuthorization(
        attempt_id=attempt_id,
        approval_id="approval-001",
        registered_at="2026-07-16T00:00:00+00:00",
        git_commit="a" * 40,
        git_tree="b" * 40,
        sealed_protocol_sha256="c" * 64,
        robustness_release="stage8-successor",
        test_period=(date(2022, 1, 1), date(2025, 12, 31)),
    )


def _signals() -> FinalTestSignals:
    bounded = pl.DataFrame({"date": [date(2022, 1, 4)]})
    return FinalTestSignals(bounded, bounded, bounded, bounded)


def _prepared_publication_identity() -> dict[str, object]:
    return {
        "attempt_manifest_sha256": "1" * 64,
        "lineage_preview_sha256": "2" * 64,
        "execution_id": "execution-001",
        "prepare_manifest_sha256": "a" * 64,
        "security_event_coverage_sha256": "b" * 64,
        "corporate_action_coverage_sha256": "c" * 64,
        "coverage_snapshot_manifest_sha256": "3" * 64,
        "staging_files_sha256": "4" * 64,
        "staging_file_count": 1,
    }


def _execution_coverage_snapshot(
    prepared_attempt: PreparedAttempt, preflight: object
):
    return snapshot_execution_coverages(
        prepared_attempt.data_root / "processed/final_test",
        attempt_id=prepared_attempt.attempt_id,
        preparation=preflight.preparation,
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
        expected_security_sha256=preflight.security_event_coverage_sha256,
        expected_corporate_sha256=preflight.corporate_action_coverage_sha256,
    )


def test_materialized_coverage_uses_one_frozen_descriptor_snapshot(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight = preflight_resume(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        approval_key=prepared_attempt.approval_key,
        attempt_id=prepared_attempt.attempt_id,
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
    )
    snapshot = _execution_coverage_snapshot(prepared_attempt, preflight)
    source_file = next(
        path
        for path in sorted(snapshot.root.rglob("*"))
        if path.is_file() and path.name != "snapshot_manifest.json"
    )
    relative = source_file.relative_to(snapshot.root)
    original = source_file.read_bytes()
    write_tree = coverage_module.write_frozen_tree_at
    replaced = False

    def replace_after_freeze(*args: object, **kwargs: object) -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            source_file.write_bytes(b"replacement after validation")
        write_tree(*args, **kwargs)

    monkeypatch.setattr(
        coverage_module,
        "write_frozen_tree_at",
        replace_after_freeze,
    )
    destination = (
        prepared_attempt.data_root
        / "processed/final_test/materialized-coverage"
    )
    coverage_module.materialize_bound_coverage_snapshot(
        prepared_attempt.data_root / "processed/final_test",
        attempt_id=prepared_attempt.attempt_id,
        destination=destination,
        expected_manifest_sha256=snapshot.manifest_sha256,
    )

    assert (destination / relative).read_bytes() == original


def test_materialized_coverage_rejects_destination_parent_replacement(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight = preflight_resume(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        approval_key=prepared_attempt.approval_key,
        attempt_id=prepared_attempt.attempt_id,
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
    )
    snapshot = _execution_coverage_snapshot(prepared_attempt, preflight)
    final_root = prepared_attempt.data_root / "processed/final_test"
    destination_parent = final_root / "materialization-parent"
    destination_parent.mkdir()
    displaced = final_root / "materialization-parent.displaced"
    write_tree = coverage_module.write_frozen_tree_at

    def replace_parent(*args: object, **kwargs: object) -> None:
        destination_parent.rename(displaced)
        destination_parent.mkdir()
        write_tree(*args, **kwargs)

    monkeypatch.setattr(coverage_module, "write_frozen_tree_at", replace_parent)

    with pytest.raises(ValueError, match="destination parent identity changed"):
        coverage_module.materialize_bound_coverage_snapshot(
            final_root,
            attempt_id=prepared_attempt.attempt_id,
            destination=destination_parent / "coverage_snapshot",
            expected_manifest_sha256=snapshot.manifest_sha256,
        )

    assert (displaced / "coverage_snapshot/snapshot_manifest.json").is_file()
    assert not (destination_parent / "coverage_snapshot").exists()


def _patch_steps(monkeypatch: pytest.MonkeyPatch, *, publishable: bool) -> None:
    def frozen_parquets(**_kwargs: object) -> dict[str, bytes]:
        buffer = BytesIO()
        pl.DataFrame({"placeholder": [1]}).write_parquet(buffer)
        payload = buffer.getvalue()
        return {
            "corporate_actions.parquet": payload,
            "security_events.parquet": payload,
        }

    def build_execution_inputs(
        authorization: FinalTestAuthorization,
        *_args: object,
        **kwargs: object,
    ) -> dict[str, object]:
        final_root = Path(str(kwargs["final_root"]))
        manifest = (
            final_root
            / "attempt_inputs"
            / authorization.attempt_id
            / "manifest.json"
        )
        manifest.parent.mkdir(parents=True)
        actions = manifest.parent / "corporate_actions.parquet"
        events = manifest.parent / "security_events.parquet"
        expected = kwargs["expected_parquet_bytes"]
        assert isinstance(expected, dict)
        actions.write_bytes(expected["corporate_actions.parquet"])
        events.write_bytes(expected["security_events.parquet"])
        snapshot = manifest.parent / "coverage_snapshot"
        shutil.copytree(
            final_root
            / "execution_coverage_snapshots"
            / authorization.attempt_id,
            snapshot,
        )
        payload = build_execution_input_manifest(
            manifest,
            authorization=authorization,
            files={
                "corporate_actions.parquet": actions,
                "security_events.parquet": events,
            },
            execution_identity=kwargs["execution_identity"],
        )
        payload["coverage_snapshot_files"] = [
            file_record(
                path,
                root=manifest.parent,
                role="official_coverage_snapshot",
            ).to_dict()
            for path in sorted(item for item in snapshot.rglob("*") if item.is_file())
        ]
        manifest.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return payload

    def build_signals(*_args: object, **_kwargs: object) -> FinalTestSignals:
        return _signals()

    def backtest(*_args: object, **_kwargs: object) -> FinalTestBacktestResult:
        return FinalTestBacktestResult(
            outputs={"nav": pl.DataFrame({"date": [date(2025, 12, 31)], "nav": [1.0]})},
            audits={"maximum_reconciliation_difference": 0.0},
            preflight={"status": "ready", "execution_started": True},
            publishable=publishable,
            gate_failures=() if publishable else ("shadow NAV gate failed",),
        )

    def metrics(*_args: object, **_kwargs: object) -> tuple[pl.DataFrame, pl.DataFrame]:
        metric = pl.DataFrame(
            {
                "period": ["test"],
                "scope": ["portfolio"],
                "entity": ["main"],
                "metric": ["annual_return"],
                "value": [-0.1],
            }
        )
        comparison = metric.select(
            "scope",
            "entity",
            "metric",
            pl.lit(0.1).alias("research"),
            pl.lit(0.05).alias("validation"),
            pl.col("value").alias("test"),
        )
        return metric, comparison

    def report(*_args: object, **_kwargs: object) -> str:
        return "# sealed final-test report\n"

    def copy_inputs(**kwargs: object) -> None:
        execution_inputs = kwargs["execution_inputs"]
        artifacts = Path(str(kwargs["artifacts"]))
        datasets = Path(str(kwargs["datasets"]))
        destination = artifacts / "execution_inputs"
        destination.mkdir()
        for relative, payload in execution_inputs.files.items():
            path = destination.joinpath(*relative.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        (datasets / "final_daily_panel").mkdir()

    for name, value in {
        "build_final_execution_inputs": build_execution_inputs,
        "freeze_final_execution_parquets": frozen_parquets,
        "build_final_test_signals": build_signals,
        "run_final_test_backtest": backtest,
        "build_final_metrics": metrics,
        "build_final_report": report,
        "_copy_release_inputs": copy_inputs,
        "resolve_final_test_data_panel": lambda final_root: SimpleNamespace(
            root=final_root / "daily_panel"
        ),
        "_upstream_identity": lambda *_args, **_kwargs: {
            "robustness_release": "stage8-successor",
            "self_contained_inputs": [],
        },
    }.items():
        monkeypatch.setattr(f"ashare_multifactor.final_test.pipeline.{name}", value)


def test_resume_claims_execution_once_and_publishes_same_attempt(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_steps(monkeypatch, publishable=True)

    result = pipeline_module.resume_final_test_release(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        approval_key=prepared_attempt.approval_key,
        attempt_id=prepared_attempt.attempt_id,
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
        run_id="final-release",
    )

    assert result.attempt_id == prepared_attempt.attempt_id
    assert result.release is not None
    with pytest.raises(ValueError, match="already succeeded|state transition"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )


def test_resume_recovers_crash_after_attempt_directories_before_identity(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_steps(monkeypatch, publishable=True)
    write_json_at = staging_module.write_json_at
    crashed = False

    def crash_before_identity(
        directory_fd: int, name: str, payload: object
    ) -> None:
        nonlocal crashed
        if name == "execution_identity.json" and not crashed:
            crashed = True
            raise KeyboardInterrupt("crash before execution identity")
        write_json_at(directory_fd, name, payload)

    monkeypatch.setattr(staging_module, "write_json_at", crash_before_identity)
    with pytest.raises(KeyboardInterrupt, match="before execution identity"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )

    monkeypatch.setattr(staging_module, "write_json_at", write_json_at)
    result = pipeline_module.resume_final_test_release(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        approval_key=prepared_attempt.approval_key,
        attempt_id=prepared_attempt.attempt_id,
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
        run_id="final-release",
    )

    assert result.release is not None


def test_resume_completes_binding_after_crash_before_manifest_hash_append(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_steps(monkeypatch, publishable=True)
    bind_manifest = pipeline_module.bind_execution_input_manifest_hash
    crashed = False

    def crash_before_bind(*_args: object, **_kwargs: object) -> str:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise KeyboardInterrupt("crash before manifest hash append")
        return bind_manifest(*_args, **_kwargs)

    monkeypatch.setattr(
        pipeline_module,
        "bind_execution_input_manifest_hash",
        crash_before_bind,
    )
    with pytest.raises(KeyboardInterrupt, match="before manifest hash append"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )

    monkeypatch.setattr(
        pipeline_module,
        "build_final_execution_inputs",
        lambda *_args, **_kwargs: pytest.fail("bound inputs must not be rebuilt"),
    )
    result = pipeline_module.resume_final_test_release(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        approval_key=prepared_attempt.approval_key,
        attempt_id=prepared_attempt.attempt_id,
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
        run_id="final-release",
    )

    assert result.release is not None


def test_resume_rejects_self_consistent_primary_files_before_first_hash_bind(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_steps(monkeypatch, publishable=True)
    bind_manifest = pipeline_module.bind_execution_input_manifest_hash
    crashed = False

    def crash_before_bind(*_args: object, **_kwargs: object) -> str:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise KeyboardInterrupt("crash before first input hash bind")
        return bind_manifest(*_args, **_kwargs)

    monkeypatch.setattr(
        pipeline_module,
        "bind_execution_input_manifest_hash",
        crash_before_bind,
    )
    with pytest.raises(KeyboardInterrupt, match="first input hash bind"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )

    input_root = (
        prepared_attempt.data_root
        / "processed/final_test/attempt_inputs"
        / prepared_attempt.attempt_id
    )
    actions = input_root / "corporate_actions.parquet"
    events = input_root / "security_events.parquet"
    pl.DataFrame({"placeholder": [999]}).write_parquet(actions)
    pl.DataFrame({"placeholder": [999]}).write_parquet(events)
    manifest_path = input_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = [
        file_record(path, root=input_root, role=path.stem).to_dict()
        for path in (actions, events)
    ]
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        pipeline_module,
        "bind_execution_input_manifest_hash",
        bind_manifest,
    )

    with pytest.raises(ValueError, match="deterministic execution output"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )


def test_first_execution_rejects_replacement_after_atomic_input_publish(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_steps(monkeypatch, publishable=True)
    build_inputs = pipeline_module.build_final_execution_inputs

    def publish_then_replace(*args: object, **kwargs: object) -> dict[str, object]:
        manifest = build_inputs(*args, **kwargs)
        authorization = args[0]
        assert isinstance(authorization, FinalTestAuthorization)
        input_root = (
            Path(str(kwargs["final_root"]))
            / "attempt_inputs"
            / authorization.attempt_id
        )
        actions = input_root / "corporate_actions.parquet"
        events = input_root / "security_events.parquet"
        pl.DataFrame({"placeholder": [999]}).write_parquet(actions)
        pl.DataFrame({"placeholder": [999]}).write_parquet(events)
        manifest["files"] = [
            file_record(path, root=input_root, role=path.stem).to_dict()
            for path in (actions, events)
        ]
        (input_root / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return manifest

    monkeypatch.setattr(
        pipeline_module,
        "build_final_execution_inputs",
        publish_then_replace,
    )

    with pytest.raises(ValueError, match="deterministic execution output"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )

def test_resume_records_failed_outcome_when_coverage_drifts_after_claim(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_steps(monkeypatch, publishable=True)
    original_verify = pipeline_module.verify_resume_coverages

    def drift_then_verify(*args: object, **kwargs: object) -> None:
        payload = json.loads(
            prepared_attempt.security_coverage.read_text(encoding="utf-8")
        )
        payload["post_claim_drift"] = True
        prepared_attempt.security_coverage.write_text(
            json.dumps(payload), encoding="utf-8"
        )
        original_verify(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "verify_resume_coverages", drift_then_verify)

    with pytest.raises(ValueError, match="coverage identity changed"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )

    registry = prepared_attempt.data_root / "processed/final_test/attempts"
    assert resolve_attempt_state(registry, prepared_attempt.attempt_id)["state"] == "failed"
    assert not (
        prepared_attempt.data_root / "processed/final_test/CURRENT.json"
    ).exists()


def test_resume_snapshots_coverages_before_post_review_self_consistent_swap(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_steps(monkeypatch, publishable=True)
    original_verify = pipeline_module.verify_resume_coverages

    def verify_then_swap(*args: object, **kwargs: object) -> None:
        original_verify(*args, **kwargs)
        payload = json.loads(
            prepared_attempt.security_coverage.read_text(encoding="utf-8")
        )
        payload["self_consistent_replacement"] = "second-official-set"
        prepared_attempt.security_coverage.write_text(
            json.dumps(payload), encoding="utf-8"
        )

    monkeypatch.setattr(pipeline_module, "verify_resume_coverages", verify_then_swap)

    with pytest.raises(ValueError, match="coverage.*snapshot|coverage identity"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )

    final_root = prepared_attempt.data_root / "processed/final_test"
    assert not (final_root / "CURRENT.json").exists()
    assert (
        resolve_attempt_state(final_root / "attempts", prepared_attempt.attempt_id)[
            "state"
        ]
        == "failed"
    )


def test_execution_coverage_snapshot_reuses_only_identical_identity(
    prepared_attempt: PreparedAttempt,
) -> None:
    preflight = preflight_resume(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        approval_key=prepared_attempt.approval_key,
        attempt_id=prepared_attempt.attempt_id,
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
    )
    final_root = prepared_attempt.data_root / "processed/final_test"
    first = snapshot_execution_coverages(
        final_root,
        attempt_id=prepared_attempt.attempt_id,
        preparation=preflight.preparation,
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
        expected_security_sha256=preflight.security_event_coverage_sha256,
        expected_corporate_sha256=preflight.corporate_action_coverage_sha256,
    )
    second = snapshot_execution_coverages(
        final_root,
        attempt_id=prepared_attempt.attempt_id,
        preparation=preflight.preparation,
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
        expected_security_sha256=preflight.security_event_coverage_sha256,
        expected_corporate_sha256=preflight.corporate_action_coverage_sha256,
    )

    assert second == first
    with pytest.raises(ValueError, match="coverage snapshot identity differs"):
        snapshot_execution_coverages(
            final_root,
            attempt_id=prepared_attempt.attempt_id,
            preparation=preflight.preparation,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            expected_security_sha256="d" * 64,
            expected_corporate_sha256=preflight.corporate_action_coverage_sha256,
        )


def test_resume_archives_verified_partial_execution_before_recomputing(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight = preflight_resume(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        approval_key=prepared_attempt.approval_key,
        attempt_id=prepared_attempt.attempt_id,
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
    )
    snapshot = _execution_coverage_snapshot(prepared_attempt, preflight)
    identities = {
        "prepare_manifest_sha256": preflight.preparation.manifest_sha256,
        "security_event_coverage_sha256": preflight.security_event_coverage_sha256,
        "corporate_action_coverage_sha256": preflight.corporate_action_coverage_sha256,
    }
    registry = prepared_attempt.data_root / "processed/final_test/attempts"
    append_attempt_state(
        registry,
        attempt_id=prepared_attempt.attempt_id,
        state="executing",
        identities=identities,
    )
    partial = (
        prepared_attempt.data_root
        / "processed/final_test/attempt_runs"
        / prepared_attempt.attempt_id
    )
    partial.mkdir(parents=True)
    (partial / "execution_identity.json").write_text(
        json.dumps(
            {
                "execution_id": "interrupted-execution",
                "attempt_id": prepared_attempt.attempt_id,
                "sealed_protocol_sha256": (
                    preflight.authorization.sealed_protocol_sha256
                ),
                **identities,
                "coverage_snapshot_manifest_sha256": snapshot.manifest_sha256,
            }
        ),
        encoding="utf-8",
    )
    bind_execution_identity(
        registry,
        identity=json.loads(
            (partial / "execution_identity.json").read_text(encoding="utf-8")
        ),
    )
    (partial / "partial.bin").write_bytes(b"immutable interrupted bytes")
    _patch_steps(monkeypatch, publishable=True)

    result = pipeline_module.resume_final_test_release(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        approval_key=prepared_attempt.approval_key,
        attempt_id=prepared_attempt.attempt_id,
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
        run_id="final-release",
    )

    archives = list(
        (
            prepared_attempt.data_root
            / "processed/final_test/interrupted_runs"
            / prepared_attempt.attempt_id
        ).iterdir()
    )
    assert result.release is not None
    assert len(archives) == 1
    assert (
        archives[0] / "archive/attempt_run/partial.bin"
    ).read_bytes() == b"immutable interrupted bytes"


def test_resume_completes_recovery_audit_after_move_before_event_crash(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight = preflight_resume(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        approval_key=prepared_attempt.approval_key,
        attempt_id=prepared_attempt.attempt_id,
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
    )
    snapshot = _execution_coverage_snapshot(prepared_attempt, preflight)
    identities = {
        "prepare_manifest_sha256": preflight.preparation.manifest_sha256,
        "security_event_coverage_sha256": preflight.security_event_coverage_sha256,
        "corporate_action_coverage_sha256": preflight.corporate_action_coverage_sha256,
    }
    registry = prepared_attempt.data_root / "processed/final_test/attempts"
    append_attempt_state(
        registry,
        attempt_id=prepared_attempt.attempt_id,
        state="executing",
        identities=identities,
    )
    partial = (
        prepared_attempt.data_root
        / "processed/final_test/attempt_runs"
        / prepared_attempt.attempt_id
    )
    partial.mkdir(parents=True)
    (partial / "execution_identity.json").write_text(
        json.dumps(
            {
                "execution_id": "interrupted-execution",
                "attempt_id": prepared_attempt.attempt_id,
                "sealed_protocol_sha256": (
                    preflight.authorization.sealed_protocol_sha256
                ),
                **identities,
                "coverage_snapshot_manifest_sha256": snapshot.manifest_sha256,
            }
        ),
        encoding="utf-8",
    )
    bind_execution_identity(
        registry,
        identity=json.loads(
            (partial / "execution_identity.json").read_text(encoding="utf-8")
        ),
    )
    (partial / "partial.bin").write_bytes(b"crash-between-move-and-complete")
    _patch_steps(monkeypatch, publishable=True)
    complete = recovery_module.complete_execution_recovery
    monkeypatch.setattr(
        recovery_module,
        "complete_execution_recovery",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("crash before recovery complete")
        ),
    )

    with pytest.raises(RuntimeError, match="recovery complete"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )

    assert not (registry / f"{prepared_attempt.attempt_id}.outcome.json").exists()
    assert resolve_attempt_state(registry, prepared_attempt.attempt_id)["state"] == "executing"

    monkeypatch.setattr(recovery_module, "complete_execution_recovery", complete)
    result = pipeline_module.resume_final_test_release(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        approval_key=prepared_attempt.approval_key,
        attempt_id=prepared_attempt.attempt_id,
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
        run_id="final-release",
    )

    assert result.release is not None
    assert len(list(registry.glob("*.recovery.*.intent.json"))) == 1
    assert len(list(registry.glob("*.recovery.*.complete.json"))) == 1


def test_resume_rejects_competing_recovery_target_without_moving_source(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight = preflight_resume(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        approval_key=prepared_attempt.approval_key,
        attempt_id=prepared_attempt.attempt_id,
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
    )
    snapshot = _execution_coverage_snapshot(prepared_attempt, preflight)
    identities = {
        "prepare_manifest_sha256": preflight.preparation.manifest_sha256,
        "security_event_coverage_sha256": preflight.security_event_coverage_sha256,
        "corporate_action_coverage_sha256": preflight.corporate_action_coverage_sha256,
    }
    final_root = prepared_attempt.data_root / "processed/final_test"
    registry = final_root / "attempts"
    append_attempt_state(
        registry,
        attempt_id=prepared_attempt.attempt_id,
        state="executing",
        identities=identities,
    )
    partial = final_root / "attempt_runs" / prepared_attempt.attempt_id
    partial.mkdir(parents=True)
    identity = {
        "execution_id": "interrupted-execution",
        "attempt_id": prepared_attempt.attempt_id,
        "sealed_protocol_sha256": preflight.authorization.sealed_protocol_sha256,
        **identities,
        "coverage_snapshot_manifest_sha256": snapshot.manifest_sha256,
    }
    (partial / "execution_identity.json").write_text(
        json.dumps(identity), encoding="utf-8"
    )
    bind_execution_identity(registry, identity=identity)
    original = b"must remain in original source"
    (partial / "partial.bin").write_bytes(original)
    intent = begin_execution_recovery(
        registry,
        attempt_id=prepared_attempt.attempt_id,
        execution_id="interrupted-execution",
        identities=identities,
        artifact_presence={
            "attempt_run": True,
            "execution_input_sources": False,
            "attempt_inputs": False,
        },
    )
    competing = final_root / str(intent["recovery_root"])
    competing.mkdir(parents=True)
    (competing / "foreign.bin").write_bytes(b"competing archive")
    _patch_steps(monkeypatch, publishable=True)

    with pytest.raises(FileExistsError, match="recovery claim target already exists"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )

    assert (partial / "partial.bin").read_bytes() == original
    assert (competing / "foreign.bin").read_bytes() == b"competing archive"
    assert not (final_root / "CURRENT.json").exists()


def test_resume_rejects_recovery_intent_path_escape_without_moving_source(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight = preflight_resume(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        approval_key=prepared_attempt.approval_key,
        attempt_id=prepared_attempt.attempt_id,
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
    )
    snapshot = _execution_coverage_snapshot(prepared_attempt, preflight)
    identities = {
        "prepare_manifest_sha256": preflight.preparation.manifest_sha256,
        "security_event_coverage_sha256": preflight.security_event_coverage_sha256,
        "corporate_action_coverage_sha256": preflight.corporate_action_coverage_sha256,
    }
    final_root = prepared_attempt.data_root / "processed/final_test"
    registry = final_root / "attempts"
    append_attempt_state(
        registry,
        attempt_id=prepared_attempt.attempt_id,
        state="executing",
        identities=identities,
    )
    partial = final_root / "attempt_runs" / prepared_attempt.attempt_id
    partial.mkdir(parents=True)
    (partial / "execution_identity.json").write_text(
        json.dumps(
            {
                "execution_id": "interrupted-execution",
                "attempt_id": prepared_attempt.attempt_id,
                "sealed_protocol_sha256": (
                    preflight.authorization.sealed_protocol_sha256
                ),
                **identities,
                "coverage_snapshot_manifest_sha256": snapshot.manifest_sha256,
            }
        ),
        encoding="utf-8",
    )
    bind_execution_identity(
        registry,
        identity=json.loads(
            (partial / "execution_identity.json").read_text(encoding="utf-8")
        ),
    )
    original = b"must not escape recovery root"
    (partial / "partial.bin").write_bytes(original)
    intent = begin_execution_recovery(
        registry,
        attempt_id=prepared_attempt.attempt_id,
        execution_id="interrupted-execution",
        identities=identities,
        artifact_presence={
            "attempt_run": True,
            "execution_input_sources": False,
            "attempt_inputs": False,
        },
    )
    intent_path = next(registry.glob("*.recovery.*.intent.json"))
    intent["recovery_root"] = "../escaped-recovery"
    intent["archived_path"] = "../escaped-recovery/archive"
    intent_path.write_text(json.dumps(intent), encoding="utf-8")
    _patch_steps(monkeypatch, publishable=True)

    with pytest.raises(ValueError, match="invalid execution recovery intent"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )

    assert (partial / "partial.bin").read_bytes() == original
    assert not (final_root.parent / "escaped-recovery").exists()


def test_resume_recovers_published_current_after_publication_interruption(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_steps(monkeypatch, publishable=True)
    publish = pipeline_module.publish_release

    def publish_then_interrupt(*args: object, **kwargs: object):
        publish(*args, **kwargs)
        raise RuntimeError("interrupted after CURRENT")

    monkeypatch.setattr(pipeline_module, "publish_release", publish_then_interrupt)
    with pytest.raises(RuntimeError, match="after CURRENT"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )

    final_root = prepared_attempt.data_root / "processed/final_test"
    registry = final_root / "attempts"
    assert (final_root / "CURRENT.json").is_file()
    assert not (registry / f"{prepared_attempt.attempt_id}.outcome.json").exists()
    assert resolve_attempt_state(registry, prepared_attempt.attempt_id)["state"] == "executing"

    monkeypatch.setattr(pipeline_module, "publish_release", publish)
    result = pipeline_module.resume_final_test_release(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        approval_key=prepared_attempt.approval_key,
        attempt_id=prepared_attempt.attempt_id,
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
        run_id="final-release",
    )

    assert result.attempt_id == prepared_attempt.attempt_id
    assert result.release is not None
    assert resolve_attempt_state(registry, prepared_attempt.attempt_id)["state"] == "published"


@pytest.mark.parametrize(
    "field",
    [
        "prepare_manifest_sha256",
        "security_event_coverage_sha256",
        "corporate_action_coverage_sha256",
        "execution_id",
        "coverage_snapshot_manifest_sha256",
    ],
)
def test_current_recovery_rejects_release_with_wrong_complete_identity(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    _patch_steps(monkeypatch, publishable=True)
    publish = pipeline_module.publish_release

    def publish_then_interrupt(*args: object, **kwargs: object):
        publish(*args, **kwargs)
        raise RuntimeError("interrupted after CURRENT")

    monkeypatch.setattr(pipeline_module, "publish_release", publish_then_interrupt)
    with pytest.raises(RuntimeError, match="after CURRENT"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )

    final_root = prepared_attempt.data_root / "processed/final_test"
    manifest_path = final_root / "releases/final-release/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = "0" * 64 if field != "execution_id" else "wrong-execution"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    pointer = json.loads((final_root / "CURRENT.json").read_text(encoding="utf-8"))
    pointer["manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (final_root / "CURRENT.json").write_text(json.dumps(pointer), encoding="utf-8")
    monkeypatch.setattr(pipeline_module, "publish_release", publish)

    with pytest.raises(ValueError, match="release identity"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )

    assert not (
        final_root / "attempts" / f"{prepared_attempt.attempt_id}.outcome.json"
    ).exists()


def test_current_recovery_rejects_release_content_not_bound_to_prepared_staging(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_steps(monkeypatch, publishable=True)
    publish = pipeline_module.publish_release

    def publish_then_interrupt(*args: object, **kwargs: object):
        publish(*args, **kwargs)
        raise RuntimeError("interrupted after CURRENT")

    monkeypatch.setattr(pipeline_module, "publish_release", publish_then_interrupt)
    with pytest.raises(RuntimeError, match="after CURRENT"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )

    final_root = prepared_attempt.data_root / "processed/final_test"
    release_root = final_root / "releases/final-release"
    report = release_root / "artifacts/report.md"
    report.write_text("changed release bytes\n", encoding="utf-8")
    manifest_path = release_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    replacement = file_record(report, root=release_root, role="artifacts").to_dict()
    manifest["files"] = [
        replacement if record["path"] == "artifacts/report.md" else record
        for record in manifest["files"]
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    pointer = json.loads((final_root / "CURRENT.json").read_text(encoding="utf-8"))
    pointer["manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (final_root / "CURRENT.json").write_text(json.dumps(pointer), encoding="utf-8")
    monkeypatch.setattr(pipeline_module, "publish_release", publish)

    with pytest.raises(ValueError, match="release identity"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )


def test_resume_recovers_orphan_release_after_rename_before_current_switch(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_steps(monkeypatch, publishable=True)
    publish = pipeline_module.publish_release

    def publish_then_interrupt(*args: object, **kwargs: object):
        return publish(*args, **kwargs, fail_before_switch=True)

    monkeypatch.setattr(pipeline_module, "publish_release", publish_then_interrupt)
    with pytest.raises(RuntimeError, match="before pointer switch"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )

    final_root = prepared_attempt.data_root / "processed/final_test"
    registry = final_root / "attempts"
    assert (final_root / "releases/final-release").is_dir()
    assert not (final_root / "CURRENT.json").exists()
    assert not (registry / f"{prepared_attempt.attempt_id}.outcome.json").exists()
    assert resolve_attempt_state(registry, prepared_attempt.attempt_id)["state"] == "executing"

    monkeypatch.setattr(pipeline_module, "publish_release", publish)
    result = pipeline_module.resume_final_test_release(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        approval_key=prepared_attempt.approval_key,
        attempt_id=prepared_attempt.attempt_id,
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
        run_id="final-release",
    )

    assert result.release is not None
    assert resolve_current(final_root).run_id == "final-release"
    assert resolve_attempt_state(registry, prepared_attempt.attempt_id)["state"] == "published"


def test_resume_rejects_orphan_release_with_mismatched_execution_identity(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_steps(monkeypatch, publishable=True)
    publish = pipeline_module.publish_release

    def publish_then_interrupt(*args: object, **kwargs: object):
        return publish(*args, **kwargs, fail_before_switch=True)

    monkeypatch.setattr(pipeline_module, "publish_release", publish_then_interrupt)
    with pytest.raises(RuntimeError, match="before pointer switch"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )

    final_root = prepared_attempt.data_root / "processed/final_test"
    identity_path = (
        final_root
        / "attempt_runs"
        / prepared_attempt.attempt_id
        / "execution_identity.json"
    )
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    identity["execution_id"] = "mismatched-execution"
    identity_path.write_text(json.dumps(identity), encoding="utf-8")
    monkeypatch.setattr(pipeline_module, "publish_release", publish)

    with pytest.raises(ValueError, match="orphan final-test release identity differs"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )

    assert not (final_root / "CURRENT.json").exists()


def test_resume_rejects_orphan_with_invalid_prepared_record_before_current_restore(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_steps(monkeypatch, publishable=True)
    publish = pipeline_module.publish_release

    def publish_then_interrupt(*args: object, **kwargs: object):
        return publish(*args, **kwargs, fail_before_switch=True)

    monkeypatch.setattr(pipeline_module, "publish_release", publish_then_interrupt)
    with pytest.raises(RuntimeError, match="before pointer switch"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )

    final_root = prepared_attempt.data_root / "processed/final_test"
    prepared_path = (
        final_root / "attempts" / f"{prepared_attempt.attempt_id}.prepared.json"
    )
    prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
    prepared["unexpected"] = "not an immutable prepared schema"
    prepared_path.write_text(json.dumps(prepared), encoding="utf-8")
    monkeypatch.setattr(pipeline_module, "publish_release", publish)

    with pytest.raises(ValueError, match="prepared final-test publication identity differs"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )

    assert not (final_root / "CURRENT.json").exists()


def test_resume_publication_failure_records_failed_outcome_without_current(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_steps(monkeypatch, publishable=True)
    monkeypatch.setattr(
        pipeline_module,
        "publish_release",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("synthetic publication failure")
        ),
    )

    with pytest.raises(RuntimeError, match="publication failure"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )

    final_root = prepared_attempt.data_root / "processed/final_test"
    registry = final_root / "attempts"
    assert not (final_root / "CURRENT.json").exists()
    assert resolve_attempt_state(registry, prepared_attempt.attempt_id)["state"] == "failed"


def test_prepared_staging_recovers_without_recomputing_research_results(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_steps(monkeypatch, publishable=True)
    publish = pipeline_module.publish_release
    monkeypatch.setattr(
        pipeline_module,
        "publish_release",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            KeyboardInterrupt("hard crash after prepared record")
        ),
    )

    with pytest.raises(KeyboardInterrupt, match="after prepared record"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )

    final_root = prepared_attempt.data_root / "processed/final_test"
    registry = final_root / "attempts"
    prepared_path = registry / f"{prepared_attempt.attempt_id}.prepared.json"
    prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
    required = {
        "attempt_manifest_sha256",
        "lineage_preview_sha256",
        "execution_id",
        "prepare_manifest_sha256",
        "security_event_coverage_sha256",
        "corporate_action_coverage_sha256",
        "coverage_snapshot_manifest_sha256",
        "staging_files_sha256",
        "staging_file_count",
    }
    assert required.issubset(prepared)
    assert not (registry / f"{prepared_attempt.attempt_id}.outcome.json").exists()
    monkeypatch.setattr(pipeline_module, "publish_release", publish)
    for name in (
        "build_final_execution_inputs",
        "build_final_test_signals",
        "run_final_test_backtest",
        "build_final_metrics",
        "build_final_report",
    ):
        monkeypatch.setattr(
            pipeline_module,
            name,
            lambda *_args, _name=name, **_kwargs: pytest.fail(
                f"prepared retry recomputed {_name}"
            ),
        )

    result = pipeline_module.resume_final_test_release(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        approval_key=prepared_attempt.approval_key,
        attempt_id=prepared_attempt.attempt_id,
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
        run_id="final-release",
    )

    assert result.release is not None
    assert resolve_attempt_state(registry, prepared_attempt.attempt_id)["state"] == "published"


def test_prepared_staging_identity_drift_fails_closed_without_recomputation(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_steps(monkeypatch, publishable=True)
    monkeypatch.setattr(
        pipeline_module,
        "publish_release",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            KeyboardInterrupt("hard crash after prepared record")
        ),
    )
    with pytest.raises(KeyboardInterrupt):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )
    final_root = prepared_attempt.data_root / "processed/final_test"
    staging = (
        final_root
        / "attempt_runs"
        / prepared_attempt.attempt_id
        / "artifacts/lineage_preview.json"
    )
    staging.write_text('{"changed":true}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="prepared.*identity"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )

    registry = final_root / "attempts"
    assert resolve_attempt_state(registry, prepared_attempt.attempt_id)["state"] == "failed"


def test_resume_preflight_failure_leaves_attempt_awaiting_without_outputs(
    prepared_attempt: PreparedAttempt,
) -> None:
    (prepared_attempt.security_coverage.parent / "szse.pdf").write_bytes(b"changed")
    final_root = prepared_attempt.data_root / "processed/final_test"

    with pytest.raises(ValueError, match="evidence.*changed"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )

    registry = final_root / "attempts"
    assert (
        resolve_attempt_state(registry, prepared_attempt.attempt_id)["state"]
        == "awaiting_official_evidence"
    )
    assert not (registry / f"{prepared_attempt.attempt_id}.outcome.json").exists()
    assert not (final_root / "attempt_runs").exists()


def test_resume_rejects_partial_execution_with_incomplete_identity(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight = preflight_resume(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        approval_key=prepared_attempt.approval_key,
        attempt_id=prepared_attempt.attempt_id,
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
    )
    identities = {
        "prepare_manifest_sha256": preflight.preparation.manifest_sha256,
        "security_event_coverage_sha256": preflight.security_event_coverage_sha256,
        "corporate_action_coverage_sha256": preflight.corporate_action_coverage_sha256,
    }
    registry = prepared_attempt.data_root / "processed/final_test/attempts"
    append_attempt_state(
        registry,
        attempt_id=prepared_attempt.attempt_id,
        state="executing",
        identities=identities,
    )
    partial = (
        prepared_attempt.data_root
        / "processed/final_test/attempt_runs"
        / prepared_attempt.attempt_id
    )
    partial.mkdir(parents=True)
    (partial / "execution_identity.json").write_text(
        json.dumps(
            {
                "execution_id": "interrupted-execution",
                "attempt_id": prepared_attempt.attempt_id,
                "prepare_manifest_sha256": preflight.preparation.manifest_sha256,
            }
        ),
        encoding="utf-8",
    )
    _patch_steps(monkeypatch, publishable=True)

    with pytest.raises(
        ValueError,
        match="identity|execution[ -]binding|registry record",
    ):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            run_id="final-release",
        )

    assert resolve_attempt_state(registry, prepared_attempt.attempt_id)["state"] == "failed"
    assert partial.is_dir()
    assert not (
        prepared_attempt.data_root / "processed/final_test/CURRENT.json"
    ).exists()


def test_concurrent_resume_allows_only_one_executor(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_steps(monkeypatch, publishable=True)

    def resume() -> str:
        try:
            pipeline_module.resume_final_test_release(
                code_root=prepared_attempt.code_root,
                data_root=prepared_attempt.data_root,
                approval_key=prepared_attempt.approval_key,
                attempt_id=prepared_attempt.attempt_id,
                security_event_coverage_path=prepared_attempt.security_coverage,
                corporate_action_coverage_root=prepared_attempt.corporate_coverage,
                run_id="final-release",
            )
        except ValueError:
            return "rejected"
        return "published"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result(timeout=10) for future in (executor.submit(resume), executor.submit(resume))]

    assert sorted(results) == ["published", "rejected"]


def test_resume_core_neither_authorizes_nor_builds_panel(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_steps(monkeypatch, publishable=True)
    monkeypatch.setattr(
        "ashare_multifactor.final_test.gate.authorize_final_test",
        lambda **_kwargs: pytest.fail("execution must not authorize"),
    )
    monkeypatch.setattr(
        pipeline_module,
        "build_or_reuse_final_test_daily_panel",
        lambda *_args, **_kwargs: pytest.fail("execution must not build a panel"),
    )

    result = pipeline_module.resume_final_test_release(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        approval_key=prepared_attempt.approval_key,
        attempt_id=prepared_attempt.attempt_id,
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
        run_id="final-release",
    )

    assert result.release is not None


def test_legacy_one_shot_pipeline_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="two-phase final-test workflow is required"):
        run_final_test_release(
            code_root=Path.cwd(),
            data_root=tmp_path,
            opening_token_path=tmp_path / "token.json",
            approval_key=b"synthetic-approval-key",
            attempt_id="attempt-001",
            run_id="final-release",
            security_event_coverage_path=tmp_path / "security-coverage.json",
            corporate_action_coverage_root=tmp_path / "action-coverage",
        )


def test_date_gate_failure_cannot_leave_publishable_attempt(
    prepared_attempt: PreparedAttempt, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_steps(monkeypatch, publishable=True)
    monkeypatch.setattr(
        "ashare_multifactor.final_test.pipeline._assert_release_date_bounds",
        lambda _root: (_ for _ in ()).throw(
            ValueError("artifact outside final-test period")
        ),
    )
    with pytest.raises(ValueError, match="artifact outside"):
        pipeline_module.resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            run_id="final-release",
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
        )
    attempt = (
        prepared_attempt.data_root
        / "processed/final_test/attempt_runs"
        / prepared_attempt.attempt_id
    )
    assert json.loads((attempt / "attempt_manifest.json").read_text())["status"] == "failed"
    outcome = (
        prepared_attempt.data_root
        / "processed/final_test/attempts"
        / f"{prepared_attempt.attempt_id}.outcome.json"
    )
    assert json.loads(outcome.read_text())["status"] == "failed"


def test_non_publishable_attempt_is_retained_without_switching_current(
    prepared_attempt: PreparedAttempt, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_steps(monkeypatch, publishable=False)
    result = pipeline_module.resume_final_test_release(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        approval_key=prepared_attempt.approval_key,
        attempt_id=prepared_attempt.attempt_id,
        run_id="blocked-release",
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
    )

    assert result.publishable is False
    assert result.release is None
    root = prepared_attempt.data_root / "processed/final_test"
    assert not (root / "CURRENT.json").exists()
    attempt = root / "attempt_runs" / prepared_attempt.attempt_id
    assert (attempt / "artifacts/preflight.json").is_file()
    assert (attempt / "attempt_manifest.json").is_file()
    outcome = json.loads(
        (root / "attempts" / f"{prepared_attempt.attempt_id}.outcome.json").read_text()
    )
    assert outcome["status"] == "failed"
    assert outcome["reason"] == "shadow NAV gate failed"


def test_cli_exposes_only_the_explicit_two_phase_entrypoints() -> None:
    source = Path("src/ashare_multifactor/cli/final_test.py").read_text(encoding="utf-8")

    assert "add_subparsers" in source
    assert 'add_parser("prepare"' in source
    assert 'add_parser("resume"' in source
    assert "--data-root" in source
    assert "opening-token" in source
    assert "approval-key-file" in source
    assert "security-event-coverage" in source
    assert "corporate-action-coverage" in source
    assert "run_final_test_release" not in source
    assert "parameter" not in source
    assert "search" not in source


def test_final_pipeline_signature_has_no_security_step_injection() -> None:
    parameters = inspect.signature(pipeline_module.resume_final_test_release).parameters
    assert "steps" not in parameters
    assert "opening_token_path" not in parameters
    assert "security_event_coverage_path" in parameters
    assert "corporate_action_coverage_root" in parameters


def test_final_execution_source_normalization_is_exactly_date_bounded() -> None:
    raw = pl.DataFrame(
        {
            "code": ["sz.000001"],
            "query_year": [2022],
            "query_year_type": ["operate"],
            "dividOperateDate": ["2022-06-01"],
            "dividPayDate": ["2022-06-08"],
            "dividStockMarketDate": [""],
            "dividCashPsBeforeTax": ["0.10"],
            "dividStocksPs": ["0"],
            "dividReserveToStockPs": ["0"],
        }
    )

    actions = _normalize_final_dividends(raw, symbols={"000001"})

    assert actions.get_column("ex_date").to_list() == [date(2022, 6, 1)]
    assert actions.get_column("effective_date").to_list() == [date(2022, 6, 8)]
    with pytest.raises(ValueError, match="sealed final-test query"):
        _normalize_final_dividends(
            raw.with_columns(pl.lit(2026).alias("query_year")),
            symbols={"000001"},
        )


def test_portfolio_inputs_use_boundary_nav_and_strict_period_slices() -> None:
    frames = {
        "nav": pl.DataFrame(
            {
                "date": [date(2004, 12, 31), date(2005, 1, 4), date(2017, 1, 3)],
                "nav": [100.0, 101.0, 999.0],
            }
        ),
        "orders": pl.DataFrame(
            {
                "order_id": ["old", "cross", "late"],
                "signal_date": [date(2004, 12, 31), date(2004, 12, 31), date(2017, 1, 3)],
                "quantity": [10, 20, 30],
                "remaining_quantity": [0, 20, 30],
            }
        ),
        "order_events": pl.DataFrame(
            {
                "order_id": ["old", "cross", "cross", "late"],
                "date": [date(2004, 12, 31), date(2005, 1, 4), date(2005, 1, 5), date(2017, 1, 3)],
                "event_seq": [1, 1, 2, 1],
                "remaining_quantity": [0, 20, 5, 30],
                "status": ["filled", "submitted", "partial", "submitted"],
            }
        ),
        "trades": pl.DataFrame(
            {"date": [date(2004, 12, 31), date(2005, 1, 4), date(2017, 1, 3)]}
        ),
        "target_diagnostics": pl.DataFrame(
            {"date": [date(2004, 12, 31), date(2005, 1, 4), date(2017, 1, 3)]}
        ),
    }

    sliced = _slice_backtest_period(
        frames, start=date(2005, 1, 1), end=date(2016, 12, 31)
    )

    assert sliced["nav"].get_column("date").to_list() == [
        date(2004, 12, 31),
        date(2005, 1, 4),
    ]
    assert sliced["orders"].get_column("order_id").to_list() == ["cross"]
    assert sliced["orders"].get_column("remaining_quantity").to_list() == [5]


def test_release_date_scan_rejects_pretest_rows(tmp_path: Path) -> None:
    pl.DataFrame(
        {"date": [date(2021, 12, 31)], "symbol": ["000001"]}
    ).write_parquet(tmp_path / "leaked.parquet")
    with pytest.raises(ValueError, match="leaked.parquet.*outside"):
        _assert_release_date_bounds(tmp_path)


def test_release_date_scan_rejects_early_artifact_rows(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    pl.DataFrame({"start_date": [date(2021, 12, 31)]}).write_parquet(
        artifacts / "stale_intervals.parquet"
    )
    with pytest.raises(ValueError, match="artifacts/stale_intervals.parquet.*outside"):
        _assert_release_date_bounds(tmp_path)


def test_stale_interval_slice_keeps_and_clips_cross_boundary_rows() -> None:
    frame = pl.DataFrame(
        {
            "symbol": ["a", "b", "c"],
            "first_stale_date": [date(2021, 12, 1), date(2025, 12, 1), date(2021, 1, 1)],
            "last_stale_date": [date(2022, 1, 5), date(2026, 1, 5), date(2021, 2, 1)],
        }
    )
    sliced = _slice_audit_frame("stale_intervals", frame)
    assert sliced.get_column("symbol").to_list() == ["a", "b"]
    assert sliced.get_column("first_stale_date").to_list() == [
        date(2022, 1, 1), date(2025, 12, 1)
    ]
    assert sliced.get_column("last_stale_date").to_list() == [
        date(2022, 1, 5), date(2025, 12, 31)
    ]


def test_research_and_validation_use_same_stage7_continuous_candidate() -> None:
    validation = SimpleNamespace(datasets=Path("validation/datasets"))

    research = _historical_backtest_root(validation, period="research")
    holdout = _historical_backtest_root(validation, period="validation")

    expected = Path(
        "validation/datasets/backtests/rolling_ic_family_size_stratified_buffered"
    )
    assert research == expected
    assert holdout == expected


def test_historical_releases_ignore_drifting_current_pointers(tmp_path: Path) -> None:
    empty_datasets = tmp_path / "empty-datasets"
    empty_artifacts = tmp_path / "empty-artifacts"
    empty_datasets.mkdir()
    empty_artifacts.mkdir()
    stage5_base = tmp_path / "processed/factor_combination"
    bound_stage5 = publish_release(
        stage5_base,
        run_id="stage5-bound",
        staged_datasets=empty_datasets,
        staged_artifacts=empty_artifacts,
        lineage={"stage": 5},
    )
    publish_release(
        stage5_base,
        run_id="stage5-drift",
        staged_datasets=empty_datasets,
        staged_artifacts=empty_artifacts,
        lineage={"stage": "drift"},
    )
    validation_lineage = {
        "upstream": {
            "stage_five_manifest": file_record(
                bound_stage5.manifest, root=tmp_path, role="stage_five_manifest"
            ).to_dict(),
            "stage_five_lineage": file_record(
                bound_stage5.lineage, root=tmp_path, role="stage_five_lineage"
            ).to_dict(),
        }
    }
    validation_base = tmp_path / "processed/validation_evaluation"
    bound_validation = publish_release(
        validation_base,
        run_id="validation-bound",
        staged_datasets=empty_datasets,
        staged_artifacts=empty_artifacts,
        lineage=validation_lineage,
    )
    publish_release(
        validation_base,
        run_id="validation-drift",
        staged_datasets=empty_datasets,
        staged_artifacts=empty_artifacts,
        lineage={"stage": "drift"},
    )

    stage5, validation = _resolve_historical_releases(
        tmp_path,
        {
            "upstream_validation": {
                "run_id": bound_validation.run_id,
                "manifest_sha256": bound_validation.manifest_sha256,
            }
        },
        {
            "inputs": {
                "validation_manifest": {
                    "sha256": bound_validation.manifest_sha256,
                    "size": bound_validation.manifest.stat().st_size,
                }
            }
        },
    )

    assert stage5.run_id == "stage5-bound"
    assert validation.run_id == "validation-bound"


def test_security_event_zero_rows_still_require_ready_official_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "official.json"
    evidence.write_text('{"source":"official"}\n', encoding="utf-8")
    evidence_index = tmp_path / "evidence_index.parquet"
    pl.DataFrame(
        {
            "evidence_id": ["ev-1"], "source": ["szse"], "market": ["sz"],
            "source_url": ["https://disc.static.szse.cn/download/disc/security.pdf"],
            "cache_file": [evidence.name],
            "sha256": [file_record(evidence, root=tmp_path, role="x").sha256],
        }
    ).write_parquet(evidence_index)
    query_coverage = tmp_path / "query_coverage.parquet"
    pl.DataFrame(
        {
            "symbol": ["000001"], "source": ["szse"], "market": ["sz"],
            "query_start": [date(2022, 1, 1)], "query_end": [date(2025, 12, 31)],
            "status": ["ok"], "event_count": [0], "evidence_id": ["ev-1"],
        }
    ).write_parquet(query_coverage)
    coverage = tmp_path / "coverage.json"
    official_query_coverage = _write_index(
        tmp_path,
        [
            OfficialQueryScope(
                symbol="000001",
                market="sz",
                category="security_events",
                query_category="",
                start=date(2022, 1, 1),
                end=date(2025, 12, 31),
            )
        ],
    )
    coverage.write_text(
        json.dumps(
            {
                "status": "ready",
                "period": ["2022-01-01", "2025-12-31"],
                "scope": "all_final_execution_symbols",
                "symbol_count": 1,
                "symbols_sha256": hashlib.sha256(b"000001\n").hexdigest(),
                "event_rows": 0,
                "evidence_index": file_record(
                    evidence_index,
                    root=tmp_path,
                    role="official_security_event_evidence_index",
                ).to_dict(),
                "coverage": [
                    file_record(
                        query_coverage,
                        root=tmp_path,
                        role="official_security_event_coverage",
                    ).to_dict()
                ],
                "official_query_coverage": file_record(
                    official_query_coverage,
                    root=tmp_path,
                    role="official_query_coverage",
                ).to_dict(),
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    verified = validate_security_event_coverage(Path("coverage.json"), symbols=["000001"])
    assert verified["event_rows"] == 0
    assert verified["coverage_root"] == tmp_path.resolve()
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        validate_security_event_coverage(alias / "coverage.json", symbols=["000001"])
    payload = json.loads(coverage.read_text(encoding="utf-8"))
    query_record = payload.pop("official_query_coverage")
    coverage.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="query.*coverage"):
        validate_security_event_coverage(coverage, symbols=["000001"])
    payload["official_query_coverage"] = query_record
    coverage.write_text(json.dumps(payload), encoding="utf-8")
    pl.concat([pl.read_parquet(query_coverage)] * 2).write_parquet(query_coverage)
    with pytest.raises(ValueError, match="query coverage"):
        validate_security_event_coverage(coverage, symbols=["000001"])
    coverage.write_text('{"status":"not_ready"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="security-event coverage"):
        validate_security_event_coverage(coverage)


def test_security_event_validator_hashes_the_single_manifest_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coverage = _write_security_event_coverage(
        tmp_path,
        symbol="000001",
        market="sz",
        source="szse",
        source_url="https://disc.static.szse.cn/download/disc/security.pdf",
    )
    disk_bytes = coverage.read_bytes()
    captured_payload = json.loads(disk_bytes)
    captured_payload["capture_marker"] = "B"
    captured_bytes = json.dumps(captured_payload, sort_keys=True).encode()
    original_read_bytes = Path.read_bytes
    manifest_reads = 0

    def capture_once(path: Path) -> bytes:
        nonlocal manifest_reads
        if path == coverage:
            manifest_reads += 1
            return captured_bytes
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", capture_once)

    verified = validate_security_event_coverage(coverage, symbols=["000001"])

    assert manifest_reads == 1
    assert verified["coverage_manifest_sha256"] == hashlib.sha256(
        captured_bytes
    ).hexdigest()
    assert original_read_bytes(coverage) == disk_bytes


def test_all_mutable_final_roots_reject_symlink_ancestors(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    final_root = data_root / "processed/final_test"
    final_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (final_root / "attempt_inputs").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        _assert_safe_roots(data_root, final_root)


def test_failed_claim_without_panel_is_archived_before_retry(tmp_path: Path) -> None:
    root = tmp_path / "final_test"
    root.mkdir()
    claim = root / "data-build-claim.json"
    claim.write_text('{"attempt_id":"a","status":"failed"}\n')
    assert _recover_or_archive_data_claim(root) == "archived_failed"
    assert not claim.exists()
    assert len(list((root / "failed-claims").glob("*.json"))) == 1


def test_security_events_must_match_coverage_counts_by_source_key() -> None:
    coverage = pl.DataFrame(
        {
            "symbol": ["000001", "600000"],
            "market": ["sz", "sh"],
            "source": ["szse", "sse"],
            "event_count": [1, 0],
        }
    )
    events = pl.DataFrame(
        {
            "source_symbol": ["600000"],
            "market": ["sh"],
            "source": ["sse"],
        }
    )
    with pytest.raises(ValueError, match="event counts"):
        _validate_event_counts(coverage, events)


@pytest.mark.parametrize(
    ("symbol", "market"),
    [("000001", "sz"), ("300001", "sz"), ("600000", "sh"),
     ("688001", "sh"), ("430001", "bj"), ("830001", "bj"),
     ("920001", "bj"), ("900901", "sh")],
)
def test_authoritative_symbol_market_boundaries(symbol: str, market: str) -> None:
    assert market_for_symbol(symbol) == market


def test_coverage_market_must_match_source_symbol() -> None:
    invalid = pl.DataFrame(
        {"source_symbol": ["000001"], "market": ["sh"], "source": ["sse"]}
    )
    with pytest.raises(ValueError, match="symbol market"):
        _validate_coverage_markets(invalid)


@pytest.mark.parametrize("symbol", ["920001", "830001", "430001"])
def test_bse_security_event_coverage_is_rejected(tmp_path: Path, symbol: str) -> None:
    coverage = _write_security_event_coverage(
        tmp_path,
        symbol=symbol,
        market="bj",
        source="bse",
        source_url="https://www.bse.cn/disclosure/2024/2024-08-16/notice.pdf",
    )
    with pytest.raises(ValueError, match="evidence source|market source"):
        validate_security_event_coverage(coverage, symbols=[symbol])


def test_bse_symbol_rejects_self_consistent_wrong_exchange(tmp_path: Path) -> None:
    coverage = _write_security_event_coverage(
        tmp_path,
        symbol="920001",
        market="sh",
        source="sse",
        source_url=(
            "https://www.sse.com.cn/disclosure/listedinfo/announcement/notice.pdf"
        ),
    )
    with pytest.raises(ValueError, match="symbol market"):
        validate_security_event_coverage(coverage, symbols=["920001"])


def test_bse_evidence_rejects_non_bse_official_url(tmp_path: Path) -> None:
    coverage = _write_security_event_coverage(
        tmp_path,
        symbol="920001",
        market="bj",
        source="bse",
        source_url=(
            "https://www.sse.com.cn/disclosure/listedinfo/announcement/notice.pdf"
        ),
    )
    with pytest.raises(ValueError, match="evidence source"):
        validate_security_event_coverage(coverage, symbols=["920001"])


def _write_security_event_coverage(
    root: Path,
    *,
    symbol: str,
    market: str,
    source: str,
    source_url: str,
) -> Path:
    evidence = root / "official.pdf"
    evidence.write_bytes(b"official")
    evidence_index = root / "evidence_index.parquet"
    pl.DataFrame(
        {
            "evidence_id": ["ev-1"],
            "source": [source],
            "market": [market],
            "source_url": [source_url],
            "cache_file": [evidence.name],
            "sha256": [file_record(evidence, root=root, role="x").sha256],
        }
    ).write_parquet(evidence_index)
    query_coverage = root / "query_coverage.parquet"
    pl.DataFrame(
        {
            "symbol": [symbol],
            "source": [source],
            "market": [market],
            "query_start": [date(2022, 1, 1)],
            "query_end": [date(2025, 12, 31)],
            "status": ["ok"],
            "event_count": [1],
            "evidence_id": ["ev-1"],
        }
    ).write_parquet(query_coverage)
    events = root / "security_events.parquet"
    pl.DataFrame(
        {
            "source_symbol": [symbol],
            "effective_date": [date(2024, 8, 16)],
            "event_type": ["write_off"],
            "target_symbol": [None],
            "ratio": [0.0],
            "cash_per_share": [0.0],
            "source": [source],
            "evidence_id": ["ev-1"],
        }
    ).write_parquet(events)
    coverage = root / "coverage.json"
    official_query_coverage = None
    if market == market_for_symbol(symbol) and market in {"sh", "sz"}:
        official_query_coverage = _write_index(
            root,
            [
                OfficialQueryScope(
                    symbol=symbol,
                    market=market,
                    category="security_events",
                    query_category="",
                    start=date(2022, 1, 1),
                    end=date(2025, 12, 31),
                )
            ],
        )
    payload: dict[str, object] = {
                "status": "ready",
                "period": ["2022-01-01", "2025-12-31"],
                "scope": "all_final_execution_symbols",
                "symbol_count": 1,
                "symbols_sha256": hashlib.sha256(f"{symbol}\n".encode()).hexdigest(),
                "event_rows": 1,
                "evidence_index": file_record(
                    evidence_index,
                    root=root,
                    role="official_security_event_evidence_index",
                ).to_dict(),
                "coverage": [
                    file_record(
                        query_coverage,
                        root=root,
                        role="official_security_event_coverage",
                    ).to_dict()
                ],
                "events_file": file_record(
                    events,
                    root=root,
                    role="official_security_events",
                ).to_dict(),
    }
    if official_query_coverage is not None:
        payload["official_query_coverage"] = file_record(
            official_query_coverage,
            root=root,
            role="official_query_coverage",
        ).to_dict()
    coverage.write_text(json.dumps(payload), encoding="utf-8")
    return coverage


def test_publishing_recovery_writes_prepared_then_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "final_test"
    panel = root / "daily_panel"
    panel.mkdir(parents=True)
    claim = {
        "attempt_id": "attempt-1",
        "approval_id": "approval-1",
        "git_commit": "a" * 40,
        "git_tree": "b" * 40,
        "sealed_protocol_sha256": "c" * 64,
        "robustness_release": "stage8-release",
        "robustness_manifest_sha256": "e" * 64,
        "robustness_lineage_sha256": "f" * 64,
        "status": "publishing",
        "data_manifest": {"sha256": "d" * 64},
    }
    (root / "data-build-claim.json").write_text(json.dumps(claim))
    monkeypatch.setattr(
        "ashare_multifactor.final_test.pipeline.resolve_final_test_data_panel",
        lambda _root: SimpleNamespace(requires_recovery=True),
    )

    assert _recover_or_archive_data_claim(root) == "recovered_published"
    recovery = root / "data-recovery"
    assert (recovery / "attempt-1.prepared.json").is_file()
    assert (recovery / "attempt-1.completed.json").is_file()
    published = json.loads((root / "data-build-claim.json").read_text())
    assert published["status"] == "published"

    (recovery / "attempt-1.completed.json").unlink()
    assert _recover_or_archive_data_claim(root) == "recovered_published"
    assert (recovery / "attempt-1.completed.json").is_file()


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("attempt_id", "attempt-2"),
        ("approval_id", "approval-2"),
        ("git_commit", "f" * 40),
        ("git_tree", "e" * 40),
        ("sealed_protocol_sha256", "1" * 64),
        ("robustness_release", "replacement-release"),
        ("robustness_manifest_sha256", "3" * 64),
        ("robustness_lineage_sha256", "4" * 64),
        ("robustness_manifest_sha256", None),
        ("robustness_lineage_sha256", None),
        ("data_manifest", {"sha256": "2" * 64}),
    ],
)
def test_prepared_recovery_rejects_mutated_publishing_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: object,
) -> None:
    root = tmp_path / "final_test"
    (root / "daily_panel").mkdir(parents=True)
    claim_path = root / "data-build-claim.json"
    claim = {
        "attempt_id": "attempt-1",
        "approval_id": "approval-1",
        "git_commit": "a" * 40,
        "git_tree": "b" * 40,
        "sealed_protocol_sha256": "c" * 64,
        "robustness_release": "stage8-release",
        "robustness_manifest_sha256": "e" * 64,
        "robustness_lineage_sha256": "f" * 64,
        "status": "publishing",
        "data_manifest": {"sha256": "d" * 64},
    }
    claim_path.write_text(json.dumps(claim))
    monkeypatch.setattr(
        "ashare_multifactor.final_test.pipeline.resolve_final_test_data_panel",
        lambda _root: SimpleNamespace(requires_recovery=True),
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.pipeline._write_json_atomic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("crash")),
    )
    with pytest.raises(RuntimeError, match="crash"):
        _recover_or_archive_data_claim(root)

    claim[field] = replacement
    claim_path.write_text(json.dumps(claim))
    expected_error = (
        "authorization identity is incomplete"
        if replacement is None
        else "prepared data recovery"
    )
    with pytest.raises(ValueError, match=expected_error):
        _recover_or_archive_data_claim(root)


@pytest.mark.parametrize("value", ["../escape", "/absolute", "a/b", "a..b", "."])
def test_publication_identifiers_reject_escape_forms(value: str) -> None:
    with pytest.raises(ValueError, match="identifier"):
        _validate_publication_id(value)


def test_prepared_publication_recovers_from_verified_current(tmp_path: Path) -> None:
    registry = tmp_path / "attempts"
    register_attempt(
        registry,
        attempt_id="attempt-001",
        git_commit="a" * 40,
        git_tree="b" * 40,
        sealed_protocol_sha256="c" * 64,
        robustness_release="stage8-release",
        approval_id="approval-001",
        token_sha256="d" * 64,
        robustness_manifest_sha256="e" * 64,
        robustness_lineage_sha256="f" * 64,
    )
    append_attempt_state(registry, attempt_id="attempt-001", state="preparing")
    append_attempt_state(
        registry,
        attempt_id="attempt-001",
        state="awaiting_official_evidence",
        identities={"prepare_manifest_sha256": "a" * 64},
    )
    append_attempt_state(
        registry,
        attempt_id="attempt-001",
        state="executing",
        identities={
            "prepare_manifest_sha256": "a" * 64,
            "security_event_coverage_sha256": "b" * 64,
            "corporate_action_coverage_sha256": "c" * 64,
        },
    )
    append_prepared_publication(
        registry,
        attempt_id="attempt-001",
        release_run_id="release-001",
        sealed_protocol_sha256="c" * 64,
        publication_identity=_prepared_publication_identity(),
    )
    current = {
        "run_id": "release-001",
        "manifest_sha256": "d" * 64,
    }

    outcome = recover_prepared_publication(
        registry,
        attempt_id="attempt-001",
        current=current,
    )

    assert outcome["status"] == "succeeded"
    assert outcome["release_manifest_sha256"] == "d" * 64


def test_prepared_publication_is_reusable_only_for_same_publication_identity(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "attempts"
    first = append_prepared_publication(
        registry,
        attempt_id="attempt-001",
        release_run_id="release-001",
        sealed_protocol_sha256="c" * 64,
        publication_identity=_prepared_publication_identity(),
    )

    assert (
        append_prepared_publication(
            registry,
            attempt_id="attempt-001",
            release_run_id="release-001",
            sealed_protocol_sha256="c" * 64,
            publication_identity=_prepared_publication_identity(),
        )
        == first
    )
    path = registry / "attempt-001.prepared.json"
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["unexpected"] = True
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="prepared publication identity"):
        append_prepared_publication(
            registry,
            attempt_id="attempt-001",
            release_run_id="release-001",
            sealed_protocol_sha256="c" * 64,
            publication_identity=_prepared_publication_identity(),
        )
    with pytest.raises(ValueError, match="prepared publication identity"):
        append_prepared_publication(
            registry,
            attempt_id="attempt-001",
            release_run_id="different-release",
            sealed_protocol_sha256="c" * 64,
            publication_identity=_prepared_publication_identity(),
        )


def test_release_copies_final_data_and_execution_evidence(tmp_path: Path) -> None:
    panel = tmp_path / "daily_panel"
    panel.mkdir()
    (panel / "data_manifest.json").write_text("{}\n")
    (panel / "input_files.json").write_text("{}\n")
    frozen_files = {
        "manifest.json": b"{}\n",
        "coverage.json": b"{}\n",
    }
    frozen = BoundExecutionInputs(
        manifest={},
        manifest_sha256=hashlib.sha256(frozen_files["manifest.json"]).hexdigest(),
        files=frozen_files,
    )
    datasets = tmp_path / "staged/datasets"
    artifacts = tmp_path / "staged/artifacts"
    datasets.mkdir(parents=True)
    artifacts.mkdir()

    _copy_release_inputs(
        panel_root=panel,
        execution_inputs=frozen,
        datasets=datasets,
        artifacts=artifacts,
    )

    assert (datasets / "final_daily_panel/data_manifest.json").is_file()
    assert (datasets / "final_daily_panel/input_files.json").is_file()
    assert (artifacts / "execution_inputs/coverage.json").is_file()


def test_release_copy_rejects_nested_execution_evidence_symlink(
    tmp_path: Path,
) -> None:
    panel = tmp_path / "daily_panel"
    panel.mkdir()
    (panel / "data_manifest.json").write_text("{}\n", encoding="utf-8")
    frozen = BoundExecutionInputs(
        manifest={},
        manifest_sha256=hashlib.sha256(b"{}\n").hexdigest(),
        files={"manifest.json": b"{}\n", "../escaped.bin": b"outside"},
    )
    datasets = tmp_path / "staged/datasets"
    artifacts = tmp_path / "staged/artifacts"
    datasets.mkdir(parents=True)
    artifacts.mkdir()

    with pytest.raises((OSError, ValueError)):
        _copy_release_inputs(
            panel_root=panel,
            execution_inputs=frozen,
            datasets=datasets,
            artifacts=artifacts,
        )

    assert not (artifacts / "execution_inputs").exists()


def test_pipeline_release_ignores_later_active_execution_source_changes(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_steps(monkeypatch, publishable=True)
    final_root = prepared_attempt.data_root / "processed/final_test"
    panel_root = final_root / "daily_panel"
    active_source = final_root / "execution_input_sources"
    original_action_bytes = b""

    def build_inputs(
        authorization: FinalTestAuthorization,
        *_args: object,
        **_kwargs: object,
    ) -> dict[str, object]:
        nonlocal original_action_bytes
        execution_root = final_root / "attempt_inputs" / authorization.attempt_id
        materialized = execution_root / "coverage_snapshot"
        shutil.copytree(
            final_root
            / "execution_coverage_snapshots"
            / authorization.attempt_id,
            materialized,
        )
        actions = execution_root / "corporate_actions.parquet"
        events = execution_root / "security_events.parquet"
        pl.DataFrame({"placeholder": [1]}).write_parquet(actions)
        original_action_bytes = actions.read_bytes()
        pl.DataFrame({"placeholder": [1]}).write_parquet(events)
        manifest = build_execution_input_manifest(
            execution_root / "manifest.json",
            authorization=authorization,
            files={
                "corporate_actions.parquet": actions,
                "security_events.parquet": events,
            },
            execution_identity=_kwargs["execution_identity"],
        )
        manifest["coverage_snapshot_files"] = [
            file_record(
                path,
                root=execution_root,
                role="official_coverage_snapshot",
            ).to_dict()
            for path in sorted(item for item in materialized.rglob("*") if item.is_file())
        ]
        (execution_root / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        active_source.mkdir()
        (active_source / "evidence.bin").write_bytes(b"initial active source")
        return manifest

    def copy_after_source_change(**kwargs: object) -> None:
        (active_source / "evidence.bin").write_bytes(b"changed active source")
        canonical_action = (
            final_root
            / "attempt_inputs"
            / prepared_attempt.attempt_id
            / "corporate_actions.parquet"
        )
        canonical_action.write_bytes(b"replaced after validation")
        assert "source_root" not in kwargs
        registry = final_root / "attempts"
        identity = resolve_execution_binding(
            registry,
            attempt_id=prepared_attempt.attempt_id,
        )
        manifest = (
            final_root
            / "attempt_inputs"
            / prepared_attempt.attempt_id
            / "manifest.json"
        )
        assert resolve_execution_input_manifest_hash(
            registry,
            identity=identity,
        ) == sha256_file(manifest)
        _copy_release_inputs(**kwargs)

    monkeypatch.setattr(pipeline_module, "build_final_execution_inputs", build_inputs)
    monkeypatch.setattr(
        pipeline_module,
        "resolve_final_test_data_panel",
        lambda _root: SimpleNamespace(root=panel_root),
    )
    monkeypatch.setattr(pipeline_module, "_copy_release_inputs", copy_after_source_change)

    result = pipeline_module.resume_final_test_release(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        approval_key=prepared_attempt.approval_key,
        attempt_id=prepared_attempt.attempt_id,
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
        run_id="final-release",
    )

    assert result.release is not None
    assert (
        result.release.artifacts
        / "execution_inputs/coverage_snapshot/snapshot_manifest.json"
    ).is_file()
    assert (
        result.release.artifacts
        / "execution_inputs/corporate_actions.parquet"
    ).read_bytes() == original_action_bytes
    assert not (result.release.artifacts / "execution_sources").exists()


def test_prepared_release_uses_frozen_package_after_staging_replacement(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_steps(monkeypatch, publishable=True)
    append_prepared = pipeline_module.append_prepared_publication
    originals: dict[str, bytes] = {}

    def append_then_replace(*args: object, **kwargs: object) -> dict[str, object]:
        result = append_prepared(*args, **kwargs)
        attempt_root = (
            prepared_attempt.data_root
            / "processed/final_test/attempt_runs"
            / prepared_attempt.attempt_id
        )
        targets = (
            attempt_root / "datasets/final_test_metrics.parquet",
            attempt_root / "datasets/target_weights.parquet",
            attempt_root / "artifacts/report.md",
        )
        for path in targets:
            relative = path.relative_to(attempt_root).as_posix()
            originals[relative] = path.read_bytes()
            path.write_bytes(f"replacement:{relative}".encode())
        return result

    monkeypatch.setattr(
        pipeline_module,
        "append_prepared_publication",
        append_then_replace,
    )

    result = pipeline_module.resume_final_test_release(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        approval_key=prepared_attempt.approval_key,
        attempt_id=prepared_attempt.attempt_id,
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
        run_id="final-release",
    )

    assert result.release is not None
    for relative, expected in originals.items():
        assert (result.release.root / relative).read_bytes() == expected


def test_failed_attempt_data_reuse_requires_same_seal_git_and_raw_inventory() -> None:
    authorization = _authorization("attempt-002")
    claim = {
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "git_commit": authorization.git_commit,
        "git_tree": authorization.git_tree,
        "robustness_release": authorization.robustness_release,
        "robustness_manifest_sha256": authorization.robustness_manifest_sha256,
        "robustness_lineage_sha256": authorization.robustness_lineage_sha256,
    }
    inventory = {"files": [{"path": "2022.csv", "sha256": "d" * 64}]}

    _assert_reusable_data_claim(claim, authorization, inventory, inventory)
    with pytest.raises(ValueError, match="raw inventory"):
        _assert_reusable_data_claim(
            claim,
            authorization,
            inventory,
            {"files": [{"path": "2022.csv", "sha256": "e" * 64}]},
        )
