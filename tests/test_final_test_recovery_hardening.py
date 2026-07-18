from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from test_final_test_pipeline import _patch_steps
from test_final_test_resume import PreparedAttempt

from ashare_multifactor.final_test import execution_sources as sources_module
from ashare_multifactor.final_test import interrupted_recovery as recovery_module
from ashare_multifactor.final_test import pipeline as pipeline_module
from ashare_multifactor.audit.records import sha256_file
from ashare_multifactor.final_test.coverage_snapshot import (
    snapshot_execution_coverages,
)
from ashare_multifactor.final_test.execution_sources import build_final_execution_inputs
from ashare_multifactor.final_test.registry import (
    append_attempt_state,
    begin_execution_recovery,
    bind_execution_identity,
    bind_execution_input_manifest_hash,
    complete_execution_recovery,
    resolve_attempt_state,
)
from ashare_multifactor.final_test.resume import preflight_resume


pytest_plugins = ("test_final_test_resume",)


def test_recovery_rejects_self_reported_identity_not_bound_in_registry(
    prepared_attempt: PreparedAttempt,
) -> None:
    preflight = _preflight(prepared_attempt)
    snapshot = _snapshot(prepared_attempt, preflight)
    _mark_executing(prepared_attempt, preflight)
    final_root = prepared_attempt.data_root / "processed/final_test"
    registry = final_root / "attempts"
    bound_identity = {
        "execution_id": "bound-execution",
        "attempt_id": prepared_attempt.attempt_id,
        "sealed_protocol_sha256": preflight.authorization.sealed_protocol_sha256,
        **_identities(preflight),
        "coverage_snapshot_manifest_sha256": snapshot.manifest_sha256,
    }
    bind_execution_identity(registry, identity=bound_identity)
    attempt_root = final_root / "attempt_runs" / prepared_attempt.attempt_id
    attempt_root.mkdir(parents=True)
    _write_execution_identity(
        attempt_root,
        prepared_attempt=prepared_attempt,
        preflight=preflight,
        execution_id="self-reported-execution",
        snapshot_sha256=snapshot.manifest_sha256,
        bind_registry=False,
    )

    with pytest.raises(ValueError, match="binding|registry"):
        recovery_module.recover_interrupted_execution(
            final_root,
            preflight=preflight,
            coverage_snapshot_manifest_sha256=snapshot.manifest_sha256,
        )

    assert (attempt_root / "execution_identity.json").is_file()


def test_recovery_rejects_replaced_attempt_input_manifest_bound_in_registry(
    prepared_attempt: PreparedAttempt,
) -> None:
    preflight, snapshot, final_root, attempt_root, _intent = _claimed_interruption(
        prepared_attempt,
        with_all_sources=True,
    )
    manifest = (
        final_root
        / "attempt_inputs"
        / prepared_attempt.attempt_id
        / "manifest.json"
    )
    manifest.write_text(
        json.dumps(
            {
                "execution_id": "interrupted-execution",
                "self_consistent_replacement": True,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="inputs.*registry binding"):
        recovery_module.recover_interrupted_execution(
            final_root,
            preflight=preflight,
            coverage_snapshot_manifest_sha256=snapshot.manifest_sha256,
        )

    assert (attempt_root / "execution_identity.json").is_file()


def test_real_execution_inputs_are_archived_and_retained_after_crash(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight = _preflight(prepared_attempt)
    final_root = prepared_attempt.data_root / "processed/final_test"
    snapshot = _snapshot(prepared_attempt, preflight)
    resolution = SimpleNamespace(
        root=final_root / "daily_panel",
        claim_status="published",
        requires_recovery=False,
        data_manifest_sha256="d" * 64,
    )
    monkeypatch.setattr(sources_module, "resolve_final_test_data_panel", lambda _root: resolution)
    _mark_executing(prepared_attempt, preflight)
    attempt_root = final_root / "attempt_runs" / prepared_attempt.attempt_id
    attempt_root.mkdir(parents=True)
    first_execution_id = "first-execution"
    _write_execution_identity(
        attempt_root,
        prepared_attempt=prepared_attempt,
        preflight=preflight,
        execution_id=first_execution_id,
        snapshot_sha256=snapshot.manifest_sha256,
    )
    execution_identity = json.loads(
        (attempt_root / "execution_identity.json").read_text(encoding="utf-8")
    )
    build_final_execution_inputs(
        preflight.authorization,
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        final_root=final_root,
        security_event_coverage_path=snapshot.security_event_coverage_path,
        corporate_action_coverage_root=snapshot.corporate_action_coverage_root,
        symbols=list(snapshot.symbols),
        execution_identity=execution_identity,
    )
    bind_execution_input_manifest_hash(
        final_root / "attempts",
        identity=execution_identity,
        manifest_sha256=sha256_file(
            final_root
            / "attempt_inputs"
            / prepared_attempt.attempt_id
            / "manifest.json"
        ),
    )
    source_root = final_root / "execution_input_sources"
    assert (
        source_root
        / "packages/security_events/sz/000001/query-package/query_manifest.json"
    ).is_file()
    assert (
        source_root
        / "corporate_action_coverage"
        / "packages/corporate_actions/sz/000001/query-package/query_manifest.json"
    ).is_file()
    (attempt_root / "signals-and-backtest.completed").write_text("crashed after work")

    archive = recovery_module.recover_interrupted_execution(
        final_root,
        preflight=preflight,
        coverage_snapshot_manifest_sha256=snapshot.manifest_sha256,
    )

    assert archive is not None
    assert (archive / "attempt_run/signals-and-backtest.completed").is_file()
    assert (archive / "execution_input_sources/source_manifest.json").is_file()
    assert (archive / "attempt_inputs/manifest.json").is_file()
    retained = final_root / "attempt_inputs" / prepared_attempt.attempt_id
    assert (retained / "manifest.json").is_file()
    assert sha256_file(retained / "manifest.json") == sha256_file(
        archive / "attempt_inputs/manifest.json"
    )
    with pytest.raises(FileExistsError, match="already bound"):
        build_final_execution_inputs(
            preflight.authorization,
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            final_root=final_root,
            security_event_coverage_path=snapshot.security_event_coverage_path,
            corporate_action_coverage_root=snapshot.corporate_action_coverage_root,
            symbols=list(snapshot.symbols),
            execution_identity=execution_identity,
        )


def test_execution_input_staging_resumes_only_an_exact_prefix(tmp_path: Path) -> None:
    path = tmp_path / "corporate_actions.parquet"
    path.write_bytes(b"exact-")

    sources_module._write_resumable_bytes(path, b"exact-payload")

    assert path.read_bytes() == b"exact-payload"
    path.write_bytes(b"different")
    with pytest.raises(ValueError, match="staging bytes differ"):
        sources_module._write_resumable_bytes(path, b"exact-payload")


@pytest.mark.parametrize(
    "tamper",
    ["query_package", "source_action_symlink", "manifest_symlink"],
)
def test_reusable_execution_sources_revalidate_copied_official_query_evidence(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tamper: str,
) -> None:
    preflight = _preflight(prepared_attempt)
    final_root = prepared_attempt.data_root / "processed/final_test"
    snapshot = _snapshot(prepared_attempt, preflight)
    resolution = SimpleNamespace(
        root=final_root / "daily_panel",
        claim_status="published",
        requires_recovery=False,
        data_manifest_sha256="d" * 64,
    )
    monkeypatch.setattr(
        sources_module,
        "resolve_final_test_data_panel",
        lambda _root: resolution,
    )
    kwargs = {
        "code_root": prepared_attempt.code_root,
        "data_root": prepared_attempt.data_root,
        "final_root": final_root,
        "security_event_coverage_path": snapshot.security_event_coverage_path,
        "corporate_action_coverage_root": snapshot.corporate_action_coverage_root,
        "symbols": list(snapshot.symbols),
        "execution_identity": {
            "execution_id": "first-execution",
            "attempt_id": prepared_attempt.attempt_id,
            "sealed_protocol_sha256": (
                preflight.authorization.sealed_protocol_sha256
            ),
            **_identities(preflight),
            "coverage_snapshot_manifest_sha256": snapshot.manifest_sha256,
        },
    }
    build_final_execution_inputs(preflight.authorization, **kwargs)
    source_root = final_root / "execution_input_sources"
    if tamper == "query_package":
        (
            source_root
            / "packages/security_events/sz/000001/query-package/query_manifest.json"
        ).unlink()
    elif tamper == "source_action_symlink":
        outside_action = tmp_path / "outside-corporate-actions.parquet"
        outside_action.write_bytes(b"outside execution input")
        source_action = source_root / "corporate_actions.parquet"
        source_action.unlink()
        source_action.symlink_to(outside_action)
    manifest_path = source_root / "source_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = []
    if tamper == "manifest_symlink":
        outside_manifest = tmp_path / "outside-source-manifest.json"
        outside_manifest.write_text(json.dumps(manifest), encoding="utf-8")
        manifest_path.unlink()
        manifest_path.symlink_to(outside_manifest)
    else:
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    shutil.rmtree(final_root / "attempt_inputs" / prepared_attempt.attempt_id)

    with pytest.raises(ValueError):
        build_final_execution_inputs(preflight.authorization, **kwargs)
    assert not (
        final_root / "attempt_inputs" / prepared_attempt.attempt_id
    ).exists()


def test_recovery_rejects_empty_competing_claim_target_without_moving_sources(
    prepared_attempt: PreparedAttempt,
) -> None:
    preflight = _preflight(prepared_attempt)
    final_root = prepared_attempt.data_root / "processed/final_test"
    snapshot = _snapshot(prepared_attempt, preflight)
    _mark_executing(prepared_attempt, preflight)
    attempt_root = final_root / "attempt_runs" / prepared_attempt.attempt_id
    attempt_root.mkdir(parents=True)
    _write_execution_identity(
        attempt_root,
        prepared_attempt=prepared_attempt,
        preflight=preflight,
        execution_id="interrupted-execution",
        snapshot_sha256=snapshot.manifest_sha256,
    )
    intent = begin_execution_recovery(
        final_root / "attempts",
        attempt_id=prepared_attempt.attempt_id,
        execution_id="interrupted-execution",
        identities=_identities(preflight),
        artifact_presence={
            "attempt_run": True,
            "execution_input_sources": False,
            "attempt_inputs": False,
        },
    )
    competing = final_root / str(intent["recovery_root"])
    competing.mkdir(parents=True)

    with pytest.raises(FileExistsError, match="recovery claim|already exists"):
        recovery_module.recover_interrupted_execution(
            final_root,
            preflight=preflight,
            coverage_snapshot_manifest_sha256=snapshot.manifest_sha256,
        )

    assert (attempt_root / "execution_identity.json").is_file()
    assert list(competing.iterdir()) == []


def test_recovery_rejects_published_claim_archive_symlink_before_any_move(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    preflight, snapshot, final_root, attempt_root, intent = _claimed_interruption(
        prepared_attempt,
        with_all_sources=True,
    )
    recovery_root = final_root / str(intent["recovery_root"])
    _publish_claim_fixture(recovery_root, intent)
    external = tmp_path / "external-archive"
    external.mkdir()
    (external / "sentinel.bin").write_bytes(b"external must remain unchanged")
    (recovery_root / "archive").symlink_to(external, target_is_directory=True)
    source_roots = {
        "attempt_run": attempt_root,
        "execution_input_sources": final_root / "execution_input_sources",
        "attempt_inputs": final_root / "attempt_inputs" / prepared_attempt.attempt_id,
    }
    source_before = {label: _tree_bytes(root) for label, root in source_roots.items()}
    external_before = _tree_bytes(external)

    with pytest.raises(ValueError, match="archive.*symlink|safe directory"):
        recovery_module.recover_interrupted_execution(
            final_root,
            preflight=preflight,
            coverage_snapshot_manifest_sha256=snapshot.manifest_sha256,
        )

    assert {
        label: _tree_bytes(root) for label, root in source_roots.items()
    } == source_before
    assert _tree_bytes(external) == external_before


def test_recovery_rejects_interrupted_runs_replaced_after_pipeline_safety_check(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    preflight, snapshot, final_root, attempt_root, _intent = _claimed_interruption(
        prepared_attempt,
        with_all_sources=True,
    )
    pipeline_module._assert_safe_roots(prepared_attempt.data_root, final_root)
    interrupted_runs = final_root / "interrupted_runs"
    interrupted_runs.mkdir()
    displaced = tmp_path / "original-interrupted-runs"
    interrupted_runs.rename(displaced)
    external = tmp_path / "external-interrupted-runs"
    external.mkdir()
    interrupted_runs.symlink_to(external, target_is_directory=True)
    source_roots = {
        "attempt_run": attempt_root,
        "execution_input_sources": final_root / "execution_input_sources",
        "attempt_inputs": final_root / "attempt_inputs" / prepared_attempt.attempt_id,
    }
    source_before = {label: _tree_bytes(root) for label, root in source_roots.items()}

    with pytest.raises(ValueError, match="interrupted_runs.*symlink|safe directory"):
        recovery_module.recover_interrupted_execution(
            final_root,
            preflight=preflight,
            coverage_snapshot_manifest_sha256=snapshot.manifest_sha256,
        )

    assert _tree_bytes(external) == {}
    assert _tree_bytes(displaced) == {}
    assert {
        label: _tree_bytes(root) for label, root in source_roots.items()
    } == source_before


def test_recovery_never_follows_interrupted_runs_replaced_after_anchor_check(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight, snapshot, final_root, attempt_root, _intent = _claimed_interruption(
        prepared_attempt,
        with_all_sources=True,
    )
    interrupted_runs = final_root / "interrupted_runs"
    interrupted_runs.mkdir()
    displaced = tmp_path / "checked-interrupted-runs"
    external = tmp_path / "window-external-interrupted-runs"
    external.mkdir()
    original_pending = recovery_module.pending_execution_recovery
    swapped = False

    def replace_after_initial_anchor_check(*args: object, **kwargs: object):
        nonlocal swapped
        result = original_pending(*args, **kwargs)
        interrupted_runs.rename(displaced)
        interrupted_runs.symlink_to(external, target_is_directory=True)
        swapped = True
        return result

    monkeypatch.setattr(
        recovery_module,
        "pending_execution_recovery",
        replace_after_initial_anchor_check,
    )
    source_roots = {
        "attempt_run": attempt_root,
        "execution_input_sources": final_root / "execution_input_sources",
        "attempt_inputs": final_root / "attempt_inputs" / prepared_attempt.attempt_id,
    }
    source_before = {label: _tree_bytes(root) for label, root in source_roots.items()}

    with pytest.raises(ValueError, match="interrupted_runs.*symlink|safe directory"):
        recovery_module.recover_interrupted_execution(
            final_root,
            preflight=preflight,
            coverage_snapshot_manifest_sha256=snapshot.manifest_sha256,
        )

    assert swapped
    assert _tree_bytes(external) == {}
    assert _tree_bytes(displaced) == {}
    assert {
        label: _tree_bytes(root) for label, root in source_roots.items()
    } == source_before


def test_existing_complete_reentry_rejects_external_manifest_after_anchor_check(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight, snapshot, final_root, _attempt_root, intent = _claimed_interruption(
        prepared_attempt,
        with_all_sources=True,
    )
    archive = recovery_module.recover_interrupted_execution(
        final_root,
        preflight=preflight,
        coverage_snapshot_manifest_sha256=snapshot.manifest_sha256,
    )
    assert archive is not None
    recovery_root = archive.parent
    manifest_bytes = (recovery_root / "archive_manifest.json").read_bytes()
    audit_root = final_root / "attempts"
    audit_before = _tree_bytes(audit_root)
    interrupted_runs = final_root / "interrupted_runs"
    displaced = tmp_path / "completed-interrupted-runs"
    external = tmp_path / "external-completed-interrupted-runs"
    external_recovery = external / prepared_attempt.attempt_id / str(intent["recovery_id"])
    external_recovery.mkdir(parents=True)
    (external_recovery / "archive_manifest.json").write_bytes(manifest_bytes)
    external_before = _tree_bytes(external)
    displaced_before = _tree_bytes(interrupted_runs)
    original_check = recovery_module._reject_unsafe_optional_directory_at
    swapped = False

    def replace_after_initial_anchor_check(
        parent_fd: int,
        name: str,
        *,
        label: str,
    ) -> bool:
        nonlocal swapped
        result = original_check(parent_fd, name, label=label)
        if not swapped and name == "interrupted_runs":
            interrupted_runs.rename(displaced)
            interrupted_runs.symlink_to(external, target_is_directory=True)
            swapped = True
        return result

    monkeypatch.setattr(
        recovery_module,
        "_reject_unsafe_optional_directory_at",
        replace_after_initial_anchor_check,
    )

    with pytest.raises(ValueError, match="interrupted_runs|recovery.*safe|identity"):
        recovery_module.recover_interrupted_execution(
            final_root,
            preflight=preflight,
            coverage_snapshot_manifest_sha256=snapshot.manifest_sha256,
        )

    assert swapped
    assert _tree_bytes(external) == external_before
    assert _tree_bytes(displaced) == displaced_before
    assert _tree_bytes(audit_root) == audit_before


def test_recovery_rename_window_never_follows_replaced_archive_symlink(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight, snapshot, final_root, attempt_root, intent = _claimed_interruption(
        prepared_attempt,
        with_all_sources=True,
    )
    recovery_root = final_root / str(intent["recovery_root"])
    _publish_claim_fixture(recovery_root, intent)
    archive = recovery_root / "archive"
    archive.mkdir()
    displaced = tmp_path / "archive-moved-outside-final-test"
    external = tmp_path / "window-external"
    external.mkdir()
    original_rename = recovery_module._atomic_rename_no_replace_at
    swapped = False

    def replace_archive_then_rename(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
    ) -> None:
        nonlocal swapped
        if not swapped and destination_name == "attempt_run":
            archive.rename(displaced)
            archive.symlink_to(external, target_is_directory=True)
            swapped = True
        original_rename(
            source_fd,
            source_name,
            destination_fd,
            destination_name,
        )

    monkeypatch.setattr(
        recovery_module,
        "_atomic_rename_no_replace_at",
        replace_archive_then_rename,
    )
    source_roots = {
        "attempt_run": attempt_root,
        "execution_input_sources": final_root / "execution_input_sources",
        "attempt_inputs": final_root / "attempt_inputs" / prepared_attempt.attempt_id,
    }
    source_before = {label: _tree_bytes(root) for label, root in source_roots.items()}

    with pytest.raises(ValueError, match="archive.*symlink|safe directory"):
        recovery_module.recover_interrupted_execution(
            final_root,
            preflight=preflight,
            coverage_snapshot_manifest_sha256=snapshot.manifest_sha256,
        )

    assert swapped
    assert _tree_bytes(external) == {}
    assert _tree_bytes(displaced) == {}
    assert {
        label: _tree_bytes(root) for label, root in source_roots.items()
    } == source_before


@pytest.mark.parametrize(
    "mutation",
    ["extra", "truncated", "wrong_identity", "wrong_hash", "archive_bytes"],
)
def test_existing_recovery_complete_requires_exact_schema_and_hashes(
    prepared_attempt: PreparedAttempt,
    mutation: str,
) -> None:
    preflight = _preflight(prepared_attempt)
    registry = prepared_attempt.data_root / "processed/final_test/attempts"
    _mark_executing(prepared_attempt, preflight)
    intent = begin_execution_recovery(
        registry,
        attempt_id=prepared_attempt.attempt_id,
        execution_id="interrupted-execution",
        identities=_identities(preflight),
        artifact_presence={
            "attempt_run": True,
            "execution_input_sources": False,
            "attempt_inputs": False,
        },
    )
    archive_manifest = (
        prepared_attempt.data_root
        / "processed/final_test"
        / str(intent["recovery_root"])
        / "archive_manifest.json"
    )
    archive_manifest.parent.mkdir(parents=True)
    archive_manifest.write_text('{"status":"archived"}\n', encoding="utf-8")
    archive_sha256 = sha256_file(archive_manifest)
    complete = complete_execution_recovery(
        registry,
        intent=intent,
        verified_archive_manifest_sha256=archive_sha256,
        verified_archive_manifest_bytes=archive_manifest.read_bytes(),
    )
    path = registry / (
        f"{prepared_attempt.attempt_id}.recovery.{intent['recovery_id']}.complete.json"
    )
    changed = deepcopy(complete)
    if mutation == "extra":
        changed["unexpected"] = True
    elif mutation == "truncated":
        changed.pop("execution_id")
    elif mutation == "wrong_identity":
        changed["execution_id"] = "other-execution"
    elif mutation == "wrong_hash":
        changed["archive_manifest_sha256"] = "b" * 64
    else:
        archive_manifest.write_text('{"status":"tampered"}\n', encoding="utf-8")
    path.write_text(json.dumps(changed), encoding="utf-8")
    verified_sha256 = (
        sha256_file(archive_manifest) if mutation == "archive_bytes" else archive_sha256
    )

    with pytest.raises(ValueError, match="completed execution recovery"):
        complete_execution_recovery(
            registry,
            intent=intent,
            verified_archive_manifest_sha256=verified_sha256,
            verified_archive_manifest_bytes=archive_manifest.read_bytes(),
        )


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("missing", "coverage_snapshot_manifest_sha256"),
        ("extra", "unexpected"),
        ("wrong", "sealed_protocol_sha256"),
        ("wrong", "coverage_snapshot_manifest_sha256"),
    ],
)
def test_partial_execution_identity_requires_exact_bound_schema(
    prepared_attempt: PreparedAttempt,
    mutation: str,
    value: str,
) -> None:
    preflight = _preflight(prepared_attempt)
    final_root = prepared_attempt.data_root / "processed/final_test"
    snapshot = _snapshot(prepared_attempt, preflight)
    _mark_executing(prepared_attempt, preflight)
    attempt_root = final_root / "attempt_runs" / prepared_attempt.attempt_id
    attempt_root.mkdir(parents=True)
    _write_execution_identity(
        attempt_root,
        prepared_attempt=prepared_attempt,
        preflight=preflight,
        execution_id="interrupted-execution",
        snapshot_sha256=snapshot.manifest_sha256,
    )
    path = attempt_root / "execution_identity.json"
    identity = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "missing":
        identity.pop(value)
    elif mutation == "extra":
        identity[value] = True
    else:
        identity[value] = "f" * 64
    path.write_text(json.dumps(identity), encoding="utf-8")

    with pytest.raises(ValueError, match="execution identity"):
        recovery_module.recover_interrupted_execution(
            final_root,
            preflight=preflight,
            coverage_snapshot_manifest_sha256=snapshot.manifest_sha256,
        )

    assert attempt_root.is_dir()


@pytest.mark.parametrize("mutation", ["extra", "absolute_path", "unrecorded_manifest"])
def test_coverage_snapshot_manifest_requires_exact_safe_recorded_schema(
    prepared_attempt: PreparedAttempt,
    mutation: str,
) -> None:
    preflight = _preflight(prepared_attempt)
    final_root = prepared_attempt.data_root / "processed/final_test"
    snapshot = _snapshot(prepared_attempt, preflight)
    manifest = json.loads(snapshot.manifest_path.read_text(encoding="utf-8"))
    if mutation == "extra":
        manifest["unexpected"] = True
    elif mutation == "absolute_path":
        manifest["security_manifest"] = str(prepared_attempt.security_coverage)
    else:
        security_path = str(manifest["security_manifest"])
        manifest["files"] = [
            record for record in manifest["files"] if record["path"] != security_path
        ]
    snapshot.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="coverage snapshot"):
        snapshot_execution_coverages(
            final_root,
            attempt_id=prepared_attempt.attempt_id,
            preparation=preflight.preparation,
            security_event_coverage_path=prepared_attempt.security_coverage,
            corporate_action_coverage_root=prepared_attempt.corporate_coverage,
            expected_security_sha256=preflight.security_event_coverage_sha256,
            expected_corporate_sha256=preflight.corporate_action_coverage_sha256,
        )


def test_wrong_run_id_cannot_mutate_prepared_orphan_before_original_run_recovers(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_steps(monkeypatch, publishable=True)
    publish = pipeline_module.publish_release

    def interrupt(*args: object, **kwargs: object):
        return publish(*args, **kwargs, fail_before_switch=True)

    monkeypatch.setattr(pipeline_module, "publish_release", interrupt)
    with pytest.raises(RuntimeError, match="before pointer switch"):
        _resume(prepared_attempt, run_id="original-run")
    final_root = prepared_attempt.data_root / "processed/final_test"
    before = _tree_bytes(final_root)

    monkeypatch.setattr(pipeline_module, "publish_release", publish)
    with pytest.raises(ValueError, match="prepared.*run"):
        _resume(prepared_attempt, run_id="wrong-run")

    assert _tree_bytes(final_root) == before
    recovered = _resume(prepared_attempt, run_id="original-run")
    assert recovered.release is not None
    assert recovered.release.run_id == "original-run"
    assert resolve_attempt_state(final_root / "attempts", prepared_attempt.attempt_id)[
        "state"
    ] == "published"


def _preflight(prepared_attempt: PreparedAttempt):
    return preflight_resume(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        approval_key=prepared_attempt.approval_key,
        attempt_id=prepared_attempt.attempt_id,
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
    )


def _claimed_interruption(
    prepared_attempt: PreparedAttempt, *, with_all_sources: bool = False
):
    preflight = _preflight(prepared_attempt)
    snapshot = _snapshot(prepared_attempt, preflight)
    _mark_executing(prepared_attempt, preflight)
    final_root = prepared_attempt.data_root / "processed/final_test"
    attempt_root = final_root / "attempt_runs" / prepared_attempt.attempt_id
    attempt_root.mkdir(parents=True)
    _write_execution_identity(
        attempt_root,
        prepared_attempt=prepared_attempt,
        preflight=preflight,
        execution_id="interrupted-execution",
        snapshot_sha256=snapshot.manifest_sha256,
    )
    if with_all_sources:
        source_root = final_root / "execution_input_sources"
        source_root.mkdir()
        (source_root / "source_manifest.json").write_text(
            json.dumps(
                {
                    "execution_id": "interrupted-execution",
                    "attempt_id": prepared_attempt.attempt_id,
                }
            ),
            encoding="utf-8",
        )
        (source_root / "source.bin").write_bytes(b"source bytes must roll back")
        input_root = final_root / "attempt_inputs" / prepared_attempt.attempt_id
        input_root.mkdir(parents=True)
        (input_root / "manifest.json").write_text(
            json.dumps({"execution_id": "interrupted-execution"}),
            encoding="utf-8",
        )
        (input_root / "input.bin").write_bytes(b"attempt input must roll back")
        bind_execution_input_manifest_hash(
            final_root / "attempts",
            identity=json.loads(
                (attempt_root / "execution_identity.json").read_text(
                    encoding="utf-8"
                )
            ),
            manifest_sha256=sha256_file(input_root / "manifest.json"),
        )
    intent = begin_execution_recovery(
        final_root / "attempts",
        attempt_id=prepared_attempt.attempt_id,
        execution_id="interrupted-execution",
        identities=_identities(preflight),
        artifact_presence={
            "attempt_run": True,
            "execution_input_sources": with_all_sources,
            "attempt_inputs": with_all_sources,
        },
    )
    return preflight, snapshot, final_root, attempt_root, intent


def _snapshot(prepared_attempt: PreparedAttempt, preflight: object):
    final_root = prepared_attempt.data_root / "processed/final_test"
    return snapshot_execution_coverages(
        final_root,
        attempt_id=prepared_attempt.attempt_id,
        preparation=preflight.preparation,
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
        expected_security_sha256=preflight.security_event_coverage_sha256,
        expected_corporate_sha256=preflight.corporate_action_coverage_sha256,
    )


def _identities(preflight: object) -> dict[str, str]:
    return {
        "prepare_manifest_sha256": preflight.preparation.manifest_sha256,
        "security_event_coverage_sha256": preflight.security_event_coverage_sha256,
        "corporate_action_coverage_sha256": preflight.corporate_action_coverage_sha256,
    }


def _mark_executing(prepared_attempt: PreparedAttempt, preflight: object) -> None:
    append_attempt_state(
        prepared_attempt.data_root / "processed/final_test/attempts",
        attempt_id=prepared_attempt.attempt_id,
        state="executing",
        identities=_identities(preflight),
    )


def _write_execution_identity(
    root: Path,
    *,
    prepared_attempt: PreparedAttempt,
    preflight: object,
    execution_id: str,
    snapshot_sha256: str,
    bind_registry: bool = True,
) -> None:
    identity = {
        "execution_id": execution_id,
        "attempt_id": prepared_attempt.attempt_id,
        "sealed_protocol_sha256": preflight.authorization.sealed_protocol_sha256,
        **_identities(preflight),
        "coverage_snapshot_manifest_sha256": snapshot_sha256,
    }
    (root / "execution_identity.json").write_text(
        json.dumps(identity),
        encoding="utf-8",
    )
    if bind_registry:
        bind_execution_identity(
            prepared_attempt.data_root / "processed/final_test/attempts",
            identity=identity,
        )


def _resume(prepared_attempt: PreparedAttempt, *, run_id: str):
    return pipeline_module.resume_final_test_release(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        approval_key=prepared_attempt.approval_key,
        attempt_id=prepared_attempt.attempt_id,
        security_event_coverage_path=prepared_attempt.security_coverage,
        corporate_action_coverage_root=prepared_attempt.corporate_coverage,
        run_id=run_id,
    )


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _publish_claim_fixture(
    recovery_root: Path,
    intent: dict[str, object],
) -> None:
    recovery_root.mkdir(parents=True)
    (recovery_root / ".intent-claim.json").write_text(
        json.dumps(intent, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
