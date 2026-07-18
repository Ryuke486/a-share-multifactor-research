from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from importlib.metadata import version
import hashlib
import hmac
from io import BytesIO
import json
import os
from pathlib import Path
import platform
from uuid import uuid4

import polars as pl

from ashare_multifactor.audit.publication import (
    PublishedRelease,
    opened_verified_current,
    resolve_current,
)
from ashare_multifactor.audit.records import file_record
from ashare_multifactor.audit.secure_tree import (
    read_frozen_tree_at,
)
from ashare_multifactor.final_test.backtest import (
    FinalTestBacktestResult,
    run_final_test_backtest,
)
from ashare_multifactor.final_test.coverage_snapshot import snapshot_execution_coverages
from ashare_multifactor.final_test.data_extension import (
    _input_inventory,
    build_final_test_daily_panel,
)
from ashare_multifactor.final_test.data_publication import resolve_final_test_data_panel
from ashare_multifactor.final_test.execution_sources import (
    build_final_execution_inputs,
    freeze_final_execution_parquets,
)
from ashare_multifactor.final_test.execution_binding import (
    BoundExecutionInputs,
    resolve_bound_execution_inputs,
    validate_execution_input_candidate,
)
from ashare_multifactor.final_test.gate import (
    FINAL_TEST_END,
    FINAL_TEST_START,
    FinalTestAuthorization,
)
from ashare_multifactor.final_test.interrupted_recovery import (
    has_recoverable_execution_archive,
    recover_interrupted_execution,
)
from ashare_multifactor.final_test.publication_transaction import (
    FinalPublicationTransaction,
    PublicationNamespaceChanged,
)
from ashare_multifactor.final_test.recovery_secure_fs import (
    opened_directory,
    read_bytes_at,
)
from ashare_multifactor.final_test.release_input_staging import (
    copy_release_inputs as _copy_release_inputs,
)
from ashare_multifactor.final_test.resume import (
    ResumePreflight,
    preflight_resume,
    verify_resume_coverages,
)
from ashare_multifactor.final_test.registry import (
    append_attempt_outcome,
    assert_no_authoritative_success,
    bind_execution_identity,
    bind_execution_input_manifest_hash,
    bind_execution_output_intent,
    claim_attempt_execution,
    resolve_optional_prepared_publication,
    resolve_attempt_state_readonly,
    resolve_optional_execution_binding,
    resolve_execution_output_intent,
    validate_publication_id,
)
from ashare_multifactor.final_test.release_outputs import (
    _slice_backtest_period,
    build_final_metrics,
    build_final_report,
)
from ashare_multifactor.final_test.signals import (
    FinalTestSignals,
    build_final_test_signals,
    _load_frozen_config,
)
from ashare_multifactor.final_test.secure_attempt_staging import (
    FrozenPreparedPackage as _FrozenPreparedPackage,
    assert_attempt_directory as _assert_attempt_directory,
    ensure_attempt_root as _secure_ensure_attempt_root,
    entry_exists_at as _entry_exists_at,
    freeze_prepared_package as _freeze_prepared_package,
    json_bytes as _json_bytes,
    open_existing_attempt_directories as _open_existing_attempt_directories,
    replace_attempt_manifest as _replace_attempt_manifest,
    release_files_identity as _release_files_identity,
    snapshot_attempt_panel as _snapshot_attempt_panel,
    verify_attempt_manifest_records as _verify_attempt_manifest_records,
    write_attempt_manifest as _secure_write_attempt_manifest,
    write_bytes_at as _write_bytes_at,
    write_frame_at as _write_frame_at,
    write_json_at as _write_json_at,
)


_RECOVERY_IDENTITY_FIELDS = (
    "attempt_id",
    "approval_id",
    "git_commit",
    "git_tree",
    "sealed_protocol_sha256",
    "robustness_release",
    "robustness_manifest_sha256",
    "robustness_lineage_sha256",
)


@dataclass(frozen=True)
class FinalTestPipelineResult:
    attempt_id: str
    publishable: bool
    attempt_root: Path
    release: PublishedRelease | None
    gate_failures: tuple[str, ...]


def append_prepared_publication(
    transaction: FinalPublicationTransaction, **kwargs: object
) -> dict[str, object]:
    """Test seam around the descriptor-bound prepared-record write."""
    return transaction.append_prepared(**kwargs)


def publish_release(
    transaction: FinalPublicationTransaction, **kwargs: object
) -> PublishedRelease:
    """Test seam around the descriptor-bound release transaction."""
    return transaction.publish(**kwargs)


def run_final_test_release(
    *,
    code_root: Path,
    data_root: Path,
    opening_token_path: Path,
    approval_key: bytes,
    attempt_id: str | None = None,
    run_id: str | None = None,
    security_event_coverage_path: Path,
    corporate_action_coverage_root: Path,
) -> FinalTestPipelineResult:
    """Fail closed: final-test execution now requires a prepared attempt."""
    del (
        code_root,
        data_root,
        opening_token_path,
        approval_key,
        attempt_id,
        run_id,
        security_event_coverage_path,
        corporate_action_coverage_root,
    )
    raise ValueError("two-phase final-test workflow is required")


def resume_final_test_release(
    *,
    code_root: Path,
    data_root: Path,
    approval_key: bytes,
    attempt_id: str,
    security_event_coverage_path: Path,
    corporate_action_coverage_root: Path,
    run_id: str,
) -> FinalTestPipelineResult:
    """Claim and execute one immutable prepared final-test attempt."""
    code_root = code_root.resolve()
    data_root = data_root.resolve()
    final_root = data_root / "processed/final_test"
    registry_root = final_root / "attempts"
    _assert_safe_roots(data_root, final_root)
    assert_no_authoritative_success(registry_root)
    _validate_publication_id(attempt_id)
    _validate_publication_id(run_id)
    state = resolve_attempt_state_readonly(registry_root, attempt_id)["state"]
    if state not in {"awaiting_official_evidence", "executing"}:
        raise ValueError("invalid final-test state transition")
    preflight = preflight_resume(
        code_root=code_root,
        data_root=data_root,
        attempt_id=attempt_id,
        approval_key=approval_key,
        security_event_coverage_path=security_event_coverage_path,
        corporate_action_coverage_root=corporate_action_coverage_root,
        expected_state=str(state),
    )
    prepared = resolve_optional_prepared_publication(
        registry_root,
        attempt_id=attempt_id,
        sealed_protocol_sha256=preflight.authorization.sealed_protocol_sha256,
    )
    prepared_run_id = (
        str(prepared["release_run_id"]) if prepared is not None else None
    )
    if prepared_run_id is not None and run_id != prepared_run_id:
        raise ValueError("prepared final-test publication run identity differs")
    identities = {
        "prepare_manifest_sha256": preflight.preparation.manifest_sha256,
        "security_event_coverage_sha256": preflight.security_event_coverage_sha256,
        "corporate_action_coverage_sha256": preflight.corporate_action_coverage_sha256,
    }
    with claim_attempt_execution(
        registry_root,
        attempt_id=attempt_id,
        identities=identities,
    ):
        try:
            return _execute_authorized_final_test(
                code_root=code_root,
                data_root=data_root,
                preflight=preflight,
                security_event_coverage_path=security_event_coverage_path,
                corporate_action_coverage_root=corporate_action_coverage_root,
                run_id=run_id,
                prepared_run_id=prepared_run_id,
            )
        except Exception as error:
            outcome = registry_root / f"{attempt_id}.outcome.json"
            prepared = registry_root / f"{attempt_id}.prepared.json"
            publication_may_need_recovery = (
                prepared.is_file() and (final_root / "CURRENT.json").is_file()
            )
            if prepared.is_file() and not publication_may_need_recovery:
                try:
                    publication_may_need_recovery = (
                        _verify_orphan_release(
                            final_root,
                            preflight=preflight,
                            run_id=run_id,
                        )
                        is not None
                    )
                except ValueError:
                    publication_may_need_recovery = False
            if not publication_may_need_recovery:
                publication_may_need_recovery = has_recoverable_execution_archive(
                    final_root,
                    attempt_id=attempt_id,
                )
            if (
                not outcome.exists()
                and not publication_may_need_recovery
                and not isinstance(error, PublicationNamespaceChanged)
            ):
                append_attempt_outcome(
                    registry_root,
                    attempt_id=attempt_id,
                    status="failed",
                    authoritative=False,
                    reason=f"{type(error).__name__}: {error}",
                )
            raise


def _execute_authorized_final_test(
    *,
    code_root: Path,
    data_root: Path,
    preflight: ResumePreflight,
    security_event_coverage_path: Path,
    corporate_action_coverage_root: Path,
    run_id: str,
    prepared_run_id: str | None,
) -> FinalTestPipelineResult:
    """Execute only from a verified preparation; never authorize or build data."""
    authorization = preflight.authorization
    _assert_authorization(authorization)
    final_root = data_root / "processed/final_test"
    registry_root = final_root / "attempts"
    if (final_root / "CURRENT.json").is_file():
        return _recover_or_reject_current(
            final_root,
            registry_root,
            preflight=preflight,
            run_id=run_id,
        )
    orphan = _recover_orphan_release(
        final_root,
        registry_root=registry_root,
        preflight=preflight,
        run_id=run_id,
    )
    if orphan is not None:
        return orphan
    if prepared_run_id is not None:
        return _recover_prepared_staging(
            final_root,
            registry_root=registry_root,
            preflight=preflight,
            run_id=run_id,
        )
    verify_resume_coverages(
        preflight,
        security_event_coverage_path=security_event_coverage_path,
        corporate_action_coverage_root=corporate_action_coverage_root,
    )
    coverage_snapshot = snapshot_execution_coverages(
        final_root,
        attempt_id=authorization.attempt_id,
        preparation=preflight.preparation,
        security_event_coverage_path=security_event_coverage_path,
        corporate_action_coverage_root=corporate_action_coverage_root,
        expected_security_sha256=preflight.security_event_coverage_sha256,
        expected_corporate_sha256=preflight.corporate_action_coverage_sha256,
    )
    execution_identity = resolve_optional_execution_binding(
        registry_root,
        attempt_id=authorization.attempt_id,
    )
    if execution_identity is None:
        execution_identity = bind_execution_identity(
            registry_root,
            identity={
                "execution_id": uuid4().hex,
                "attempt_id": authorization.attempt_id,
                "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
                "prepare_manifest_sha256": preflight.preparation.manifest_sha256,
                "security_event_coverage_sha256": (
                    preflight.security_event_coverage_sha256
                ),
                "corporate_action_coverage_sha256": (
                    preflight.corporate_action_coverage_sha256
                ),
                "coverage_snapshot_manifest_sha256": coverage_snapshot.manifest_sha256,
            },
        )
    expected_execution_identity = _expected_execution_identity(
        preflight,
        execution_id=execution_identity.get("execution_id"),
        coverage_snapshot_manifest_sha256=coverage_snapshot.manifest_sha256,
    )
    if execution_identity != expected_execution_identity:
        raise ValueError("final-test execution registry binding differs")
    expected_parquet_bytes = freeze_final_execution_parquets(
        symbols=list(coverage_snapshot.symbols),
        security_event_coverage_path=coverage_snapshot.security_event_coverage_path,
        corporate_action_coverage_root=coverage_snapshot.corporate_action_coverage_root,
        execution_identity=execution_identity,
    )
    bind_execution_output_intent(
        registry_root,
        identity=execution_identity,
        outputs={
            name: {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
            for name, payload in sorted(expected_parquet_bytes.items())
        },
    )
    expected_primary_outputs = resolve_execution_output_intent(
        registry_root,
        identity=execution_identity,
    )
    attempt_root = final_root / "attempt_runs" / authorization.attempt_id
    bound_inputs = _bind_or_resolve_existing_execution_inputs(
        final_root,
        authorization=authorization,
        execution_identity=execution_identity,
        expected_primary_outputs=expected_primary_outputs,
    )
    if not _is_resumable_attempt_shell(attempt_root, execution_identity):
        recover_interrupted_execution(
            final_root,
            preflight=preflight,
            coverage_snapshot_manifest_sha256=coverage_snapshot.manifest_sha256,
        )
    attempt_root, attempt_directories = _secure_ensure_attempt_root(
        final_root,
        attempt_id=authorization.attempt_id,
        execution_identity=execution_identity,
    )
    datasets = attempt_root / "datasets"
    artifacts = attempt_root / "artifacts"
    _assert_attempt_directory(attempt_root, attempt_directories)
    started_at = datetime.now(timezone.utc).isoformat()
    panel_snapshot = None
    bound_panel_files = None
    try:
        config = _load_frozen_config(code_root, data_root)
        if bound_inputs is None:
            build_final_execution_inputs(
                authorization,
                code_root=code_root,
                data_root=data_root,
                final_root=final_root,
                security_event_coverage_path=(
                    coverage_snapshot.security_event_coverage_path
                ),
                corporate_action_coverage_root=(
                    coverage_snapshot.corporate_action_coverage_root
                ),
                symbols=list(coverage_snapshot.symbols),
                execution_identity=execution_identity,
                expected_parquet_bytes=expected_parquet_bytes,
            )
            candidate = validate_execution_input_candidate(
                final_root,
                authorization,
                execution_identity=execution_identity,
                expected_primary_outputs=expected_primary_outputs,
            )
            bind_execution_input_manifest_hash(
                registry_root,
                identity=execution_identity,
                manifest_sha256=candidate.manifest_sha256,
            )
            bound_inputs = resolve_bound_execution_inputs(
                final_root, authorization
            )
        execution_manifest = dict(bound_inputs.manifest)
        panel_resolution = resolve_final_test_data_panel(final_root)
        panel_snapshot = _snapshot_attempt_panel(
            panel_resolution.root,
            expected_manifest_sha256=panel_resolution.data_manifest_sha256,
            datasets_fd=attempt_directories.datasets_fd,
            staged_panel_root=datasets / "final_daily_panel",
            period=config.test,
        )
        _assert_attempt_directory(attempt_root, attempt_directories)
        signals = build_final_test_signals(
            authorization,
            code_root=code_root,
            final_root=final_root,
            panel_snapshot=panel_snapshot,
        )
        backtest = run_final_test_backtest(
            authorization,
            signals,
            code_root=code_root,
            final_root=final_root,
            panel_snapshot=panel_snapshot,
        )
        _write_attempt_core(
            attempt_directories.datasets_fd,
            attempt_directories.artifacts_fd,
            signals,
            backtest,
        )
        _assert_attempt_directory(attempt_root, attempt_directories)
        if not backtest.publishable:
            reason = "; ".join(backtest.gate_failures) or "publishable=false"
            _copy_release_inputs(
                panel_root=panel_snapshot.root,
                panel_snapshot=panel_snapshot,
                execution_inputs=bound_inputs,
                panel_manifest_sha256=panel_resolution.data_manifest_sha256,
                panel_already_staged=True,
                datasets=datasets,
                artifacts=artifacts,
                datasets_fd=attempt_directories.datasets_fd,
                artifacts_fd=attempt_directories.artifacts_fd,
            )
            bound_panel_files = panel_snapshot.read_frozen_files()
            _write_attempt_manifest(
                attempt_directories.attempt_fd,
                authorization=authorization,
                status="failed",
                reason=reason,
                bound_panel_files=bound_panel_files,
            )
            append_attempt_outcome(
                registry_root,
                attempt_id=authorization.attempt_id,
                status="failed",
                authoritative=False,
                reason=reason,
            )
            return FinalTestPipelineResult(
                authorization.attempt_id,
                False,
                attempt_root,
                None,
                backtest.gate_failures,
            )

        test_metrics, comparison = build_final_metrics(
            authorization,
            signals,
            backtest,
            code_root=code_root,
            data_root=data_root,
        )
        _write_frame_at(
            attempt_directories.datasets_fd,
            "final_test_metrics.parquet",
            test_metrics,
        )
        _write_frame_at(
            attempt_directories.datasets_fd,
            "period_comparison.parquet",
            comparison,
        )
        _assert_attempt_directory(attempt_root, attempt_directories)
        completed_at = datetime.now(timezone.utc).isoformat()
        _copy_release_inputs(
            panel_root=panel_snapshot.root,
            panel_snapshot=panel_snapshot,
            execution_inputs=bound_inputs,
            panel_manifest_sha256=panel_resolution.data_manifest_sha256,
            panel_already_staged=True,
            datasets=datasets,
            artifacts=artifacts,
            datasets_fd=attempt_directories.datasets_fd,
            artifacts_fd=attempt_directories.artifacts_fd,
        )
        _assert_attempt_directory(attempt_root, attempt_directories)
        upstream = _upstream_identity(
            data_root,
            authorization,
            execution_manifest,
            staged_root=attempt_root,
        )
        lineage = _lineage(
            authorization,
            upstream=upstream,
            supported_markets=config.supported_markets,
            started_at=started_at,
            completed_at=completed_at,
            execution_identity=execution_identity,
        )
        report = build_final_report(
            authorization,
            comparison,
            code_root=code_root,
            registry_root=registry_root,
        )
        _write_bytes_at(
            attempt_directories.artifacts_fd,
            "report.md",
            report.encode("utf-8"),
        )
        _write_json_at(
            attempt_directories.artifacts_fd,
            "execution_inputs.json",
            dict(execution_manifest),
        )
        _write_json_at(
            attempt_directories.artifacts_fd,
            "lineage_preview.json",
            lineage,
        )
        _write_json_at(
            attempt_directories.artifacts_fd,
            "checkpoint_c.json",
            {
                "status": "required",
                "development_stopped": True,
                "delivery_started": False,
            },
        )
        _assert_attempt_directory(attempt_root, attempt_directories)
        _assert_attempt_directory(attempt_root, attempt_directories)
        bound_panel_files = panel_snapshot.read_frozen_files()
        _assert_attempt_directory(attempt_root, attempt_directories)
        _write_attempt_manifest(
            attempt_directories.attempt_fd,
            authorization=authorization,
            status="validating",
            reason="frozen final-test publication gates are pending",
            bound_panel_files=bound_panel_files,
        )
        _assert_attempt_directory(attempt_root, attempt_directories)
        prepared_package = _freeze_prepared_package(
            attempt_root,
            lineage=lineage,
            bound_input_files=bound_inputs.files,
            bound_panel_files=bound_panel_files,
            attempt_fd=attempt_directories.attempt_fd,
            datasets_fd=attempt_directories.datasets_fd,
            artifacts_fd=attempt_directories.artifacts_fd,
        )
        _assert_attempt_directory(attempt_root, attempt_directories)
        if (
            dict(prepared_package.execution_identity) != dict(execution_identity)
            or dict(prepared_package.lineage) != dict(lineage)
        ):
            raise ValueError("prepared final-test staging identity differs")
        _verify_attempt_manifest_records(prepared_package)
        _assert_frozen_release_date_bounds(prepared_package.release_files)
        _assert_attempt_directory(attempt_root, attempt_directories)
        publishable_manifest = {
            **prepared_package.attempt_manifest,
            **_attempt_manifest_metadata(
                authorization,
                status="publishable",
                reason="all frozen final-test publication gates passed",
            ),
        }
        publishable_manifest_bytes = _json_bytes(publishable_manifest)
        _replace_attempt_manifest(
            attempt_directories.attempt_fd,
            expected_bytes=prepared_package.attempt_manifest_bytes,
            replacement_bytes=publishable_manifest_bytes,
        )
        prepared_package = replace(
            prepared_package,
            attempt_manifest=publishable_manifest,
            attempt_manifest_bytes=publishable_manifest_bytes,
        )
        _assert_attempt_directory(attempt_root, attempt_directories)
        publication_identity = _prepared_staging_identity(
            prepared_package,
            preflight=preflight,
            execution_identity=execution_identity,
        )
        with FinalPublicationTransaction.open(
            final_root, attempt_id=authorization.attempt_id
        ) as transaction:
            transaction.crosscheck_attempt(attempt_directories)
            append_prepared_publication(
                transaction,
                release_run_id=run_id,
                sealed_protocol_sha256=authorization.sealed_protocol_sha256,
                publication_identity=publication_identity,
            )
            release = publish_release(
                transaction,
                run_id=run_id,
                release_files=prepared_package.release_files,
                manifest_metadata=_release_manifest_metadata(
                    preflight,
                    execution_identity=execution_identity,
                ),
                current_must_be_absent=True,
            )
            transaction.append_success(
                release, reason="all frozen final-test publication gates passed"
            )
        return FinalTestPipelineResult(
            authorization.attempt_id,
            True,
            attempt_root,
            release,
            (),
        )
    except BaseException as error:
        if _entry_exists_at(
            attempt_directories.attempt_fd, "attempt_manifest.json"
        ):
            _replace_validating_manifest_with_failure(
                attempt_directories.attempt_fd,
                authorization=authorization,
                error=error,
            )
        else:
            _write_attempt_manifest(
                attempt_directories.attempt_fd,
                authorization=authorization,
                status="failed",
                reason=f"{type(error).__name__}: {error}",
                bound_panel_files=bound_panel_files,
            )
        raise
    finally:
        if panel_snapshot is not None:
            panel_snapshot.close()
        attempt_directories.close()


def build_or_reuse_final_test_daily_panel(
    config: object,
    authorization: FinalTestAuthorization,
    start: object,
    end: object,
    *,
    code_root: Path,
) -> object:
    final_root = getattr(config, "paths").processed / "final_test"
    claim_path = final_root / "data-build-claim.json"
    claim_state = _recover_or_archive_data_claim(final_root)
    if claim_state in {"absent", "archived_failed"}:
        return build_final_test_daily_panel(
            config, authorization, start, end, code_root=code_root
        )
    if not claim_path.is_file():
        return build_final_test_daily_panel(
            config, authorization, start, end, code_root=code_root
        )
    resolution = resolve_final_test_data_panel(final_root)
    claim = json.loads(claim_path.read_text(encoding="utf-8"))
    recorded_inventory = json.loads(
        (resolution.root / "input_files.json").read_text(encoding="utf-8")
    )
    current_inventory = _input_inventory(config, start, end)
    _assert_reusable_data_claim(
        claim, authorization, recorded_inventory, current_inventory
    )
    reuse_root = final_root / "data-reuse"
    if reuse_root.is_symlink():
        raise ValueError("final-test data reuse path uses a symlink")
    reuse_root.mkdir(parents=True, exist_ok=True)
    reuse_path = reuse_root / f"{authorization.attempt_id}.json"
    if reuse_path.exists() or reuse_path.is_symlink():
        raise FileExistsError("final-test data reuse is already recorded")
    _write_json(
        reuse_path,
        {
            "attempt_id": authorization.attempt_id,
            "approval_id": authorization.approval_id,
            "git_commit": authorization.git_commit,
            "git_tree": authorization.git_tree,
            "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
            "robustness_manifest_sha256": authorization.robustness_manifest_sha256,
            "robustness_lineage_sha256": authorization.robustness_lineage_sha256,
            "robustness_release": authorization.robustness_release,
            "data_manifest_sha256": resolution.data_manifest_sha256,
            "status": "reused_verified_immutable_panel",
        },
    )
    return resolution


def _recover_or_archive_data_claim(final_root: Path) -> str:
    claim_path = final_root / "data-build-claim.json"
    if not claim_path.is_file():
        return "absent"
    claim_bytes = claim_path.read_bytes()
    claim = json.loads(claim_bytes)
    initial_claim_sha256 = hashlib.sha256(claim_bytes).hexdigest()
    status = claim.get("status") if isinstance(claim, dict) else None
    panel = final_root / "daily_panel"
    if status == "failed" and not panel.exists():
        archive_root = final_root / "failed-claims"
        if archive_root.is_symlink():
            raise ValueError("failed claim archive uses a symlink")
        archive_root.mkdir(exist_ok=True)
        archived_attempt = validate_publication_id(str(claim.get("attempt_id", "")))
        digest = initial_claim_sha256[:16]
        destination = archive_root / f"{archived_attempt}-{digest}.json"
        if destination.exists():
            raise FileExistsError("failed claim archive already exists")
        os.replace(claim_path, destination)
        return "archived_failed"
    attempt_id = validate_publication_id(str(claim.get("attempt_id", "")))
    recovery_root = final_root / "data-recovery"
    prepared_path = recovery_root / f"{attempt_id}.prepared.json"
    completed_path = recovery_root / f"{attempt_id}.completed.json"
    if status == "publishing" and panel.is_dir():
        resolution = resolve_final_test_data_panel(final_root)
        if not resolution.requires_recovery:
            raise ValueError("publishing claim recovery state is inconsistent")
        _assert_safe_recovery_root(recovery_root)
        existing_prepared = set(recovery_root.glob("*.prepared.json"))
        if existing_prepared and existing_prepared != {prepared_path}:
            raise ValueError("prepared data recovery attempt does not match claim")
        manifest_sha256 = _claim_manifest_sha256(claim)
        recovery_identity = _claim_recovery_identity(claim)
        if not prepared_path.exists():
            _write_json_exclusive(
                prepared_path,
                {
                    "status": "prepared",
                    "claim_sha256": initial_claim_sha256,
                    "data_manifest_sha256": manifest_sha256,
                    **recovery_identity,
                },
            )
        prepared = _load_prepared_recovery(
            prepared_path,
            claim=claim,
            current_claim_sha256=hashlib.sha256(claim_path.read_bytes()).hexdigest(),
        )
        claim["status"] = "published"
        _write_json_atomic(claim_path, claim)
        _complete_data_recovery(
            completed_path,
            prepared=prepared,
            published_claim_sha256=hashlib.sha256(claim_path.read_bytes()).hexdigest(),
        )
        return "recovered_published"
    if status == "published":
        if prepared_path.is_file():
            if not panel.is_dir():
                raise ValueError("prepared data recovery is missing the published panel")
            resolve_final_test_data_panel(final_root)
            _assert_safe_recovery_root(recovery_root)
            prepared = _load_prepared_recovery(
                prepared_path,
                claim=claim,
            )
            _complete_data_recovery(
                completed_path,
                prepared=prepared,
                published_claim_sha256=hashlib.sha256(claim_path.read_bytes()).hexdigest(),
            )
            return "recovered_published"
        return "published"
    raise ValueError("final-test data claim requires manual recovery")


def _claim_manifest_sha256(claim: Mapping[str, object]) -> str:
    manifest = claim.get("data_manifest")
    digest = manifest.get("sha256") if isinstance(manifest, dict) else None
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError("final-test data claim manifest identity is invalid")
    return digest


def _claim_recovery_identity(claim: Mapping[str, object]) -> dict[str, str]:
    identity = {field: claim.get(field) for field in _RECOVERY_IDENTITY_FIELDS}
    if any(not isinstance(value, str) or not value for value in identity.values()):
        raise ValueError("final-test recovery authorization identity is incomplete")
    if (
        len(identity["git_commit"]) != 40
        or len(identity["git_tree"]) != 40
        or len(identity["sealed_protocol_sha256"]) != 64
    ):
        raise ValueError("final-test recovery authorization identity is invalid")
    return {field: str(identity[field]) for field in _RECOVERY_IDENTITY_FIELDS}


def _assert_safe_recovery_root(root: Path) -> None:
    if root.is_symlink():
        raise ValueError("data recovery path uses a symlink")
    root.mkdir(exist_ok=True)


def _load_prepared_recovery(
    path: Path,
    *,
    claim: Mapping[str, object],
    current_claim_sha256: str | None = None,
) -> Mapping[str, object]:
    if path.is_symlink():
        raise ValueError("prepared data recovery audit uses a symlink")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError("invalid prepared data recovery audit") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("claim_sha256"), str):
        raise ValueError("prepared data recovery audit does not match claim")
    if current_claim_sha256 is not None and not hmac.compare_digest(
        str(payload["claim_sha256"]), current_claim_sha256
    ):
        raise ValueError("prepared data recovery claim hash does not match publishing claim")
    if (
        payload.get("status") != "prepared"
        or payload.get("data_manifest_sha256") != _claim_manifest_sha256(claim)
        or len(str(payload["claim_sha256"])) != 64
        or any(
            payload.get(field) != value
            for field, value in _claim_recovery_identity(claim).items()
        )
    ):
        raise ValueError("prepared data recovery audit does not match claim")
    return payload


def _complete_data_recovery(
    path: Path,
    *,
    prepared: Mapping[str, object],
    published_claim_sha256: str,
) -> None:
    payload = {
        "attempt_id": prepared["attempt_id"],
        "status": "completed",
        "prepared_claim_sha256": prepared["claim_sha256"],
        "data_manifest_sha256": prepared["data_manifest_sha256"],
        "published_claim_sha256": published_claim_sha256,
    }
    if path.is_symlink():
        raise ValueError("completed data recovery audit uses a symlink")
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != payload:
            raise ValueError("completed data recovery audit changed")
        return
    _write_json_exclusive(path, payload)


def _write_json_exclusive(path: Path, payload: Mapping[str, object]) -> None:
    content = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        + "\n"
    ).encode()
    temporary = path.with_name(f".{path.name}.{os.urandom(8).hex()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except BaseException:
        raise
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.urandom(8).hex()}.tmp")
    try:
        _write_json_exclusive(temporary, payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _assert_reusable_data_claim(
    claim: Mapping[str, object],
    authorization: FinalTestAuthorization,
    recorded_inventory: Mapping[str, object],
    current_inventory: Mapping[str, object],
) -> None:
    expected = {
        "git_commit": authorization.git_commit,
        "git_tree": authorization.git_tree,
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "robustness_release": authorization.robustness_release,
        "robustness_manifest_sha256": authorization.robustness_manifest_sha256,
        "robustness_lineage_sha256": authorization.robustness_lineage_sha256,
    }
    if any(claim.get(key) != value for key, value in expected.items()):
        raise ValueError("final-test panel cannot be reused across seal or Git identity")
    if recorded_inventory != current_inventory:
        raise ValueError("final-test raw inventory changed; panel reuse is forbidden")


def _validate_publication_id(value: str) -> str:
    return validate_publication_id(value)


def _assert_safe_roots(data_root: Path, final_root: Path) -> None:
    processed = data_root / "processed"
    for path in (
        processed,
        final_root,
        final_root / "attempt_runs",
        final_root / "interrupted_runs",
        final_root / "execution_coverage_snapshots",
        final_root / "attempts",
        final_root / "attempt_inputs",
        final_root / "execution_input_sources",
        final_root / "data-reuse",
        final_root / "failed-claims",
        final_root / "data-recovery",
        final_root / "releases",
        final_root / "CURRENT.json",
    ):
        if path.is_symlink():
            raise ValueError("final-test publication path uses a symlink")
    if final_root.resolve() != processed.resolve() / "final_test":
        raise ValueError("final-test publication path escapes processed root")


def _recover_or_reject_current(
    final_root: Path,
    registry_root: Path,
    *,
    preflight: ResumePreflight,
    run_id: str,
) -> FinalTestPipelineResult:
    del registry_root
    attempt_id = preflight.authorization.attempt_id
    with FinalPublicationTransaction.open(
        final_root, attempt_id=attempt_id
    ) as transaction:
        with opened_verified_current(final_root) as held:
            transaction.crosscheck_current(held)
            release = held.release
            if release.run_id != run_id:
                raise ValueError("prepared final-test publication run identity differs")
            _verify_complete_release_identity(
                final_root,
                registry_root=final_root / "attempts",
                preflight=preflight,
                run_id=run_id,
                release=release,
                held_release_files=held.files,
                held_manifest=held.manifest,
                transaction=transaction,
            )
            held.assert_unchanged()
            transaction.recover_prepared(release)
            held.assert_unchanged()
            return FinalTestPipelineResult(
                attempt_id,
                True,
                final_root / "attempt_runs" / attempt_id,
                release,
                (),
            )


def _recover_orphan_release(
    final_root: Path,
    *,
    registry_root: Path,
    preflight: ResumePreflight,
    run_id: str,
) -> FinalTestPipelineResult | None:
    del registry_root
    if not (final_root / "releases" / run_id).exists():
        return None
    attempt_id = preflight.authorization.attempt_id
    with FinalPublicationTransaction.open(
        final_root, attempt_id=attempt_id
    ) as transaction:
        if not transaction.release_exists(run_id):
            return None
        release, release_files, manifest = transaction.resolve_release_package(run_id)
        try:
            _verify_complete_release_identity(
                final_root,
                registry_root=final_root / "attempts",
                preflight=preflight,
                run_id=run_id,
                release=release,
                held_release_files=release_files,
                held_manifest=manifest,
                transaction=transaction,
            )
        except ValueError as error:
            raise ValueError("orphan final-test release identity differs") from error
        transaction.restore_current(release)
        transaction.recover_prepared(release)
        return FinalTestPipelineResult(
            attempt_id,
            True,
            final_root / "attempt_runs" / attempt_id,
            release,
            (),
        )


def _verify_orphan_release(
    final_root: Path,
    *,
    preflight: ResumePreflight,
    run_id: str,
) -> PublishedRelease | None:
    if not (final_root / "releases" / run_id).exists():
        return None
    attempt_id = preflight.authorization.attempt_id
    with FinalPublicationTransaction.open(
        final_root, attempt_id=attempt_id
    ) as transaction:
        if not transaction.release_exists(run_id):
            return None
        release, release_files, manifest = transaction.resolve_release_package(run_id)
        try:
            _verify_complete_release_identity(
                final_root,
                registry_root=final_root / "attempts",
                preflight=preflight,
                run_id=run_id,
                release=release,
                held_release_files=release_files,
                held_manifest=manifest,
                transaction=transaction,
            )
        except ValueError as error:
            raise ValueError("orphan final-test release identity differs") from error
        return release


def _recover_prepared_staging(
    final_root: Path,
    *,
    registry_root: Path,
    preflight: ResumePreflight,
    run_id: str,
) -> FinalTestPipelineResult:
    del registry_root
    attempt_id = preflight.authorization.attempt_id
    attempt_root = final_root / "attempt_runs" / attempt_id
    with FinalPublicationTransaction.open(
        final_root, attempt_id=attempt_id
    ) as transaction:
        execution_identity, _, _, prepared_package = _verify_prepared_staging(
            attempt_root,
            registry_root=final_root / "attempts",
            preflight=preflight,
            run_id=run_id,
            transaction=transaction,
        )
        release = publish_release(
            transaction,
            run_id=run_id,
            release_files=prepared_package.release_files,
            manifest_metadata=_release_manifest_metadata(
                preflight,
                execution_identity=execution_identity,
            ),
            current_must_be_absent=True,
        )
        transaction.append_success(
            release, reason="recovered verified prepared final-test publication"
        )
        return FinalTestPipelineResult(attempt_id, True, attempt_root, release, ())


def _verify_complete_release_identity(
    final_root: Path,
    *,
    registry_root: Path,
    preflight: ResumePreflight,
    run_id: str,
    release: PublishedRelease,
    held_release_files: Mapping[str, bytes] | None = None,
    held_manifest: Mapping[str, object] | None = None,
    transaction: FinalPublicationTransaction,
) -> None:
    attempt_id = preflight.authorization.attempt_id
    attempt_root = final_root / "attempt_runs" / attempt_id
    execution_identity, lineage_preview, publication_identity, prepared_package = (
        _verify_prepared_staging(
            attempt_root,
            registry_root=registry_root,
            preflight=preflight,
            run_id=run_id,
            transaction=transaction,
        )
    )
    try:
        if held_release_files is None or held_manifest is None:
            manifest = json.loads(release.manifest.read_text(encoding="utf-8"))
            lineage_bytes = release.lineage.read_bytes()
            release_files = _read_published_release_files(release.root)
        else:
            manifest = dict(held_manifest)
            release_files = dict(held_release_files)
            lineage_bytes = release_files["lineage.json"]
        lineage = json.loads(lineage_bytes)
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as error:
        raise ValueError("final-test release identity is incomplete") from error
    expected_metadata = _release_manifest_metadata(
        preflight,
        execution_identity=execution_identity,
    )
    release_staging_identity = _release_files_identity(release_files)
    expected_staging_identity = {
        key: publication_identity[key]
        for key in ("staging_files_sha256", "staging_file_count")
    }
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"files", "run_id", *expected_metadata}
        or manifest.get("run_id") != run_id
        or any(manifest.get(key) != value for key, value in expected_metadata.items())
        or release_staging_identity != expected_staging_identity
        or release_files != dict(prepared_package.release_files)
        or lineage != lineage_preview
        or hashlib.sha256(lineage_bytes).hexdigest()
        != publication_identity["lineage_preview_sha256"]
    ):
        raise ValueError("final-test release identity differs")


def _verify_prepared_staging(
    attempt_root: Path,
    *,
    registry_root: Path,
    preflight: ResumePreflight,
    run_id: str,
    transaction: FinalPublicationTransaction,
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    _FrozenPreparedPackage,
]:
    attempt_id = preflight.authorization.attempt_id
    final_root = attempt_root.parent.parent
    opened_root, directories = _open_existing_attempt_directories(
        final_root, attempt_id=attempt_id
    )
    try:
        datasets = read_frozen_tree_at(
            directories.datasets_fd, label="prepared datasets staging"
        )
        panel_prefix = "final_daily_panel/"
        bound_panel_files = {
            path.removeprefix(panel_prefix): payload
            for path, payload in datasets.items()
            if path.startswith(panel_prefix)
        }
        package = _freeze_prepared_package(
            opened_root,
            lineage=None,
            bound_input_files=resolve_bound_execution_inputs(
                final_root, preflight.authorization
            ).files,
            bound_panel_files=bound_panel_files,
            attempt_fd=directories.attempt_fd,
            datasets_fd=directories.datasets_fd,
            artifacts_fd=directories.artifacts_fd,
        )
        _assert_attempt_directory(opened_root, directories)
        transaction.crosscheck_attempt(directories)
    finally:
        directories.close()
    execution_identity = dict(package.execution_identity)
    lineage = dict(package.lineage)
    attempt_manifest = dict(package.attempt_manifest)
    publication_identity = _prepared_staging_identity(
        package,
        preflight=preflight,
        execution_identity=execution_identity,
    )
    try:
        transaction.resolve_prepared(
            release_run_id=run_id,
            sealed_protocol_sha256=preflight.authorization.sealed_protocol_sha256,
            publication_identity=publication_identity,
        )
    except ValueError as error:
        raise ValueError("prepared final-test publication identity differs") from error
    expected_execution = _expected_execution_identity(
        preflight,
        execution_id=publication_identity["execution_id"],
        coverage_snapshot_manifest_sha256=publication_identity[
            "coverage_snapshot_manifest_sha256"
        ],
    )
    authorization = lineage.get("authorization")
    execution = lineage.get("execution")
    if (
        execution_identity != expected_execution
        or not isinstance(attempt_manifest, dict)
        or attempt_manifest.get("status") != "publishable"
        or attempt_manifest.get("attempt_id") != attempt_id
        or attempt_manifest.get("sealed_protocol_sha256")
        != preflight.authorization.sealed_protocol_sha256
        or not isinstance(authorization, dict)
        or authorization.get("attempt_id") != attempt_id
        or authorization.get("sealed_protocol_sha256")
        != preflight.authorization.sealed_protocol_sha256
        or not isinstance(execution, dict)
        or execution.get("identity") != execution_identity
    ):
        raise ValueError("prepared final-test staging identity differs")
    _verify_attempt_manifest_records(package)
    return execution_identity, lineage, publication_identity, package


def _prepared_staging_identity(
    package: _FrozenPreparedPackage,
    *,
    preflight: ResumePreflight,
    execution_identity: Mapping[str, object],
) -> dict[str, object]:
    expected = _expected_execution_identity(
        preflight,
        execution_id=execution_identity.get("execution_id"),
        coverage_snapshot_manifest_sha256=execution_identity.get(
            "coverage_snapshot_manifest_sha256"
        ),
    )
    if dict(execution_identity) != expected:
        raise ValueError("prepared final-test staging identity differs")
    staging = _release_files_identity(package.release_files)
    return {
        "attempt_manifest_sha256": hashlib.sha256(
            package.attempt_manifest_bytes
        ).hexdigest(),
        "lineage_preview_sha256": hashlib.sha256(package.lineage_bytes).hexdigest(),
        "execution_id": expected["execution_id"],
        "prepare_manifest_sha256": expected["prepare_manifest_sha256"],
        "security_event_coverage_sha256": expected[
            "security_event_coverage_sha256"
        ],
        "corporate_action_coverage_sha256": expected[
            "corporate_action_coverage_sha256"
        ],
        "coverage_snapshot_manifest_sha256": expected[
            "coverage_snapshot_manifest_sha256"
        ],
        **staging,
    }


def _read_published_release_files(root: Path) -> dict[str, bytes]:
    with opened_directory(root, label="published final-test release") as release_fd:
        files = read_frozen_tree_at(release_fd, label="published final-test release")
    files.pop("manifest.json", None)
    return files


def _expected_execution_identity(
    preflight: ResumePreflight,
    *,
    execution_id: object,
    coverage_snapshot_manifest_sha256: object,
) -> dict[str, object]:
    if not isinstance(execution_id, str) or not execution_id:
        raise ValueError("prepared final-test staging identity differs")
    validate_publication_id(execution_id)
    if (
        not isinstance(coverage_snapshot_manifest_sha256, str)
        or len(coverage_snapshot_manifest_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in coverage_snapshot_manifest_sha256
        )
    ):
        raise ValueError("prepared final-test staging identity differs")
    return {
        "execution_id": execution_id,
        "attempt_id": preflight.authorization.attempt_id,
        "sealed_protocol_sha256": preflight.authorization.sealed_protocol_sha256,
        "prepare_manifest_sha256": preflight.preparation.manifest_sha256,
        "security_event_coverage_sha256": preflight.security_event_coverage_sha256,
        "corporate_action_coverage_sha256": preflight.corporate_action_coverage_sha256,
        "coverage_snapshot_manifest_sha256": coverage_snapshot_manifest_sha256,
    }


def _release_manifest_metadata(
    preflight: ResumePreflight,
    *,
    execution_identity: Mapping[str, object],
) -> dict[str, object]:
    expected = _expected_execution_identity(
        preflight,
        execution_id=execution_identity.get("execution_id"),
        coverage_snapshot_manifest_sha256=execution_identity.get(
            "coverage_snapshot_manifest_sha256"
        ),
    )
    if dict(execution_identity) != expected:
        raise ValueError("final-test execution identity differs")
    return {
        "stage": "final_test",
        "period": [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()],
        **expected,
        "publishable": True,
    }


def _bind_or_resolve_existing_execution_inputs(
    final_root: Path,
    *,
    authorization: FinalTestAuthorization,
    execution_identity: Mapping[str, object],
    expected_primary_outputs: Mapping[str, Mapping[str, object]],
) -> BoundExecutionInputs | None:
    input_root = final_root / "attempt_inputs" / authorization.attempt_id
    if input_root.is_symlink():
        raise ValueError("execution-input root uses a symlink")
    if not input_root.exists():
        return None
    candidate = validate_execution_input_candidate(
        final_root,
        authorization,
        execution_identity=execution_identity,
        expected_primary_outputs=expected_primary_outputs,
    )
    bind_execution_input_manifest_hash(
        final_root / "attempts",
        identity=execution_identity,
        manifest_sha256=candidate.manifest_sha256,
    )
    return resolve_bound_execution_inputs(final_root, authorization)


def _is_resumable_attempt_shell(
    attempt_root: Path,
    execution_identity: Mapping[str, object],
) -> bool:
    if attempt_root.is_symlink() or not attempt_root.is_dir():
        return False
    items = {path.name: path for path in attempt_root.iterdir()}
    if set(items) != {"execution_identity.json", "datasets", "artifacts"}:
        return False
    if (
        not items["datasets"].is_dir()
        or items["datasets"].is_symlink()
        or any(items["datasets"].iterdir())
        or not items["artifacts"].is_dir()
        or items["artifacts"].is_symlink()
        or any(items["artifacts"].iterdir())
        or items["execution_identity.json"].is_symlink()
    ):
        return False
    try:
        stored = json.loads(
            items["execution_identity.json"].read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return False
    return stored == dict(execution_identity)


def _assert_safe_release_tree(
    root: Path,
    *,
    record_files: bool,
) -> list[dict[str, object]]:
    items = list(root.rglob("*")) if root.is_dir() else []
    if root.is_symlink() or not root.is_dir() or any(path.is_symlink() for path in items):
        raise ValueError("final-test release input uses a symlink or is missing")
    if not record_files:
        return []
    return [
        file_record(path, root=root, role="final_test_release_input").to_dict()
        for path in sorted(item for item in items if item.is_file())
    ]


def _write_attempt_core(
    datasets_fd: int,
    artifacts_fd: int,
    signals: FinalTestSignals,
    backtest: FinalTestBacktestResult,
) -> None:
    release_outputs = _slice_backtest_period(
        backtest.outputs,
        start=FINAL_TEST_START,
        end=FINAL_TEST_END,
        include_nav_boundary=False,
    )
    for name, frame in {
        "factor_panel": signals.factor_panel,
        "composite_scores": signals.composite_scores,
        "composite_weights": signals.composite_weights,
        "target_weights": signals.target_weights,
        **release_outputs,
    }.items():
        _write_frame_at(datasets_fd, f"{name}.parquet", frame)
    _write_json_at(artifacts_fd, "preflight.json", backtest.preflight)
    _write_json_at(
        artifacts_fd,
        "runtime_audits.json",
        {key: value for key, value in backtest.audits.items() if not isinstance(value, pl.DataFrame)},
    )
    for name, value in backtest.audits.items():
        if isinstance(value, pl.DataFrame):
            _write_frame_at(
                artifacts_fd,
                f"{name}.parquet",
                _slice_audit_frame(name, value),
            )


def _slice_audit_frame(name: str, frame: pl.DataFrame) -> pl.DataFrame:
    if name == "stale_intervals":
        required = {"first_stale_date", "last_stale_date"}
        if not required.issubset(frame.columns):
            raise ValueError("stale_intervals audit schema is invalid")
        return frame.filter(
            (pl.col("last_stale_date") >= FINAL_TEST_START)
            & (pl.col("first_stale_date") <= FINAL_TEST_END)
        ).with_columns(
            pl.max_horizontal(
                pl.col("first_stale_date"), pl.lit(FINAL_TEST_START)
            ).alias("first_stale_date"),
            pl.min_horizontal(
                pl.col("last_stale_date"), pl.lit(FINAL_TEST_END)
            ).alias("last_stale_date"),
        )
    if "date" in frame.columns:
        return frame.filter(pl.col("date").cast(pl.Date).is_between(
            FINAL_TEST_START, FINAL_TEST_END
        ))
    date_columns = [
        column
        for column, dtype in frame.schema.items()
        if dtype == pl.Date or isinstance(dtype, pl.Datetime)
    ]
    if date_columns:
        raise ValueError(f"unknown dated audit schema: {name}")
    return frame


def _assert_release_date_bounds(root: Path) -> None:
    """Reject any dated release row outside the one-shot final-test period."""
    parquet_paths = sorted(root.rglob("*.parquet"))
    for path in parquet_paths:
        schema = pl.read_parquet_schema(path)
        date_columns = [
            name
            for name, dtype in schema.items()
            if dtype == pl.Date or isinstance(dtype, pl.Datetime)
        ]
        if not date_columns:
            _assert_undated_release_relation(path, root=root)
            continue
        frame = pl.read_parquet(path, columns=date_columns)
        for column in date_columns:
            invalid = frame.filter(
                pl.col(column).is_not_null()
                & ~pl.col(column).cast(pl.Date).is_between(FINAL_TEST_START, FINAL_TEST_END)
            )
            if not invalid.is_empty():
                relative = path.relative_to(root)
                raise ValueError(
                    f"{relative}: date column {column} contains rows outside final-test period"
                )


def _assert_frozen_release_date_bounds(files: Mapping[str, bytes]) -> None:
    """Apply the release semantic gate to the exact bytes selected for publication."""
    for relative, payload in sorted(files.items()):
        if not relative.endswith(".parquet"):
            continue
        schema = pl.read_parquet_schema(BytesIO(payload))
        date_columns = [
            name
            for name, dtype in schema.items()
            if dtype == pl.Date or isinstance(dtype, pl.Datetime)
        ]
        if not date_columns:
            _assert_frozen_undated_release_relation(relative, payload, files)
            continue
        frame = pl.read_parquet(BytesIO(payload), columns=date_columns)
        for column in date_columns:
            if frame.filter(
                pl.col(column).is_not_null()
                & ~pl.col(column).cast(pl.Date).is_between(
                    FINAL_TEST_START, FINAL_TEST_END
                )
            ).height:
                raise ValueError(
                    f"{relative}: date column {column} contains rows outside final-test period"
                )


def _assert_frozen_undated_release_relation(
    relative: str, payload: bytes, files: Mapping[str, bytes]
) -> None:
    name = Path(relative).name
    schema = pl.read_parquet_schema(BytesIO(payload))
    if name == "final_test_metrics.parquet":
        if "period" not in schema:
            raise ValueError(f"{relative}: undated metric table lacks period identity")
        return
    if name == "period_comparison.parquet":
        if "test" not in schema:
            raise ValueError(f"{relative}: undated comparison lacks final-test metric relation")
        return
    if name in {"orders.parquet", "scenario_orders.parquet"}:
        event_name = name.replace("orders", "order_events")
        event_relative = str(Path(relative).with_name(event_name))
        event_payload = files.get(event_relative)
        if event_payload is None:
            raise ValueError(f"{relative}: undated terminal table lacks event history")
        frame = pl.read_parquet(BytesIO(payload))
        keys = ["order_id"]
        if "scenario" in frame.columns:
            keys.insert(0, "scenario")
        events = pl.read_parquet(BytesIO(event_payload), columns=keys)
        if not frame.select(keys).join(events.unique(), on=keys, how="anti").is_empty():
            raise ValueError(f"{relative}: terminal rows lack dated event relation")


def _assert_undated_release_relation(path: Path, *, root: Path) -> None:
    """Require an explicit bounded-table relation for datasets without date columns."""
    relative = path.relative_to(root)
    if relative.name == "final_test_metrics.parquet":
        if "period" not in pl.read_parquet_schema(path):
            raise ValueError(f"{relative}: undated metric table lacks period identity")
        return
    if relative.name == "period_comparison.parquet":
        if "test" not in pl.read_parquet_schema(path):
            raise ValueError(f"{relative}: undated comparison lacks final-test metric relation")
        return
    if relative.name in {"orders.parquet", "scenario_orders.parquet"}:
        frame = pl.read_parquet(path)
        event_name = relative.name.replace("orders", "order_events")
        event_path = path.with_name(event_name)
        if not event_path.is_file():
            raise ValueError(f"{relative}: undated terminal table lacks event history")
        keys = ["order_id"]
        if "scenario" in frame.columns:
            keys.insert(0, "scenario")
        events = pl.read_parquet(event_path, columns=keys)
        missing = frame.select(keys).join(events.unique(), on=keys, how="anti")
        if not missing.is_empty():
            raise ValueError(f"{relative}: terminal rows lack dated event relation")
        return
    # Undated evidence tables are content-addressed by their enclosing manifests.
    return


def _write_attempt_manifest(
    attempt_fd: int,
    *,
    authorization: FinalTestAuthorization,
    status: str,
    reason: str,
    bound_panel_files: Mapping[str, bytes] | None = None,
) -> None:
    _secure_write_attempt_manifest(
        attempt_fd,
        metadata=_attempt_manifest_metadata(
            authorization, status=status, reason=reason
        ),
        bound_panel_files=bound_panel_files,
    )


def _attempt_manifest_metadata(
    authorization: FinalTestAuthorization,
    *,
    status: str,
    reason: str,
) -> dict[str, object]:
    return {
        "attempt_id": authorization.attempt_id,
        "status": status,
        "reason": reason,
        "period": [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()],
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "git_commit": authorization.git_commit,
        "git_tree": authorization.git_tree,
        "robustness_release": authorization.robustness_release,
        "robustness_manifest_sha256": authorization.robustness_manifest_sha256,
        "robustness_lineage_sha256": authorization.robustness_lineage_sha256,
    }


def _replace_validating_manifest_with_failure(
    attempt_fd: int,
    *,
    authorization: FinalTestAuthorization,
    error: BaseException,
) -> None:
    current_bytes = read_bytes_at(
        attempt_fd, "attempt_manifest.json", label="attempt manifest failure transition"
    )
    try:
        current = json.loads(current_bytes)
    except json.JSONDecodeError:
        return
    if not isinstance(current, dict) or current.get("status") != "validating":
        return
    failed = {
        **current,
        **_attempt_manifest_metadata(
            authorization,
            status="failed",
            reason=f"{type(error).__name__}: {error}",
        ),
    }
    _replace_attempt_manifest(
        attempt_fd,
        expected_bytes=current_bytes,
        replacement_bytes=_json_bytes(failed),
    )


def _upstream_identity(
    data_root: Path,
    authorization: FinalTestAuthorization,
    execution_manifest: Mapping[str, object],
    *,
    staged_root: Path,
) -> dict[str, object]:
    final_root = data_root / "processed/final_test"
    if (final_root / "data-build-claim.json").is_file():
        resolution = resolve_final_test_data_panel(final_root)
        final_data = [
            file_record(path, root=data_root, role="final_test_data_input").to_dict()
            for path in (
                resolution.root / "data_manifest.json",
                resolution.root / "input_files.json",
            )
        ]
    else:
        raise ValueError("final-test data identity is incomplete")
    staged_inputs = [
            file_record(path, root=staged_root, role="self_contained_final_input").to_dict()
            for path in sorted(
                item
                for relative in (
                    "datasets/final_daily_panel",
                    "artifacts/execution_inputs",
                    "artifacts/execution_sources",
                )
                for item in (staged_root / relative).rglob("*")
                if item.is_file()
            )
        ]
    try:
        robustness = resolve_current(data_root / "processed/robustness")
        validation = resolve_current(data_root / "processed/validation_evaluation")
    except FileNotFoundError:
        raise ValueError("final-test upstream release identity is incomplete") from None
    if robustness.run_id != authorization.robustness_release:
        raise ValueError("authorized robustness release changed")
    sealed = json.loads(
        (robustness.artifacts / "sealed_test_protocol.json").read_text(encoding="utf-8")
    )
    expected_validation = sealed.get("upstream_validation")
    if not isinstance(expected_validation, dict) or (
        validation.run_id != expected_validation.get("run_id")
        or validation.manifest_sha256 != expected_validation.get("manifest_sha256")
    ):
        raise ValueError("authorized validation release changed")
    return {
        "robustness_release": robustness.run_id,
        "robustness_manifest_sha256": robustness.manifest_sha256,
        "validation_release": validation.run_id,
        "validation_manifest_sha256": validation.manifest_sha256,
        "final_test_data": final_data,
        "self_contained_inputs": staged_inputs,
        "execution_inputs": dict(execution_manifest),
    }


def _lineage(
    authorization: FinalTestAuthorization,
    *,
    upstream: Mapping[str, object],
    supported_markets: tuple[str, ...],
    started_at: str,
    completed_at: str,
    execution_identity: Mapping[str, object],
) -> dict[str, object]:
    return {
        "stage": "final_test",
        "period": [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()],
        "supported_markets": list(supported_markets),
        "authorization": {
            "attempt_id": authorization.attempt_id,
            "approval_id": authorization.approval_id,
            "registered_at": authorization.registered_at,
            "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
            "git_commit": authorization.git_commit,
            "git_tree": authorization.git_tree,
        },
        "upstream": dict(upstream),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "dependencies": {
                package: version(package)
                for package in (
                    "baostock",
                    "numpy",
                    "polars",
                    "scipy",
                    "PyYAML",
                    "matplotlib",
                )
            },
        },
        "execution": {
            "started_at": started_at,
            "completed_at": completed_at,
            "identity": dict(execution_identity),
        },
        "policy": {
            "parameter_search": False,
            "test_tuning": False,
            "checkpoint_c_required": True,
        },
    }


def _assert_authorization(authorization: FinalTestAuthorization) -> None:
    if not isinstance(authorization, FinalTestAuthorization):
        raise TypeError("authorization must be a FinalTestAuthorization")
    if authorization.test_period != (FINAL_TEST_START, FINAL_TEST_END):
        raise ValueError("authorization differs from the exact final-test period")


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
