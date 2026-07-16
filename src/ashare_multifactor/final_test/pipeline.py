from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import version
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil

import polars as pl

from ashare_multifactor.audit.publication import PublishedRelease, publish_release, resolve_current
from ashare_multifactor.audit.records import file_record
from ashare_multifactor.final_test.backtest import (
    FinalTestBacktestResult,
    run_final_test_backtest,
)
from ashare_multifactor.final_test.data_extension import (
    _input_inventory,
    build_final_test_daily_panel,
)
from ashare_multifactor.final_test.data_publication import resolve_final_test_data_panel
from ashare_multifactor.final_test.execution_sources import build_final_execution_inputs
from ashare_multifactor.final_test.gate import (
    FINAL_TEST_END,
    FINAL_TEST_START,
    FinalTestAuthorization,
    authorize_final_test,
)
from ashare_multifactor.final_test.registry import (
    append_prepared_publication,
    append_attempt_outcome,
    assert_no_authoritative_success,
    recover_prepared_publication,
    validate_publication_id,
)
from ashare_multifactor.final_test.release_outputs import (
    build_final_metrics,
    build_final_report,
)
from ashare_multifactor.final_test.signals import (
    FinalTestSignals,
    build_final_test_signals,
    _load_frozen_config,
)


@dataclass(frozen=True)
class FinalTestPipelineResult:
    attempt_id: str
    publishable: bool
    attempt_root: Path
    release: PublishedRelease | None
    gate_failures: tuple[str, ...]


@dataclass(frozen=True)
class FinalTestPipelineSteps:
    """Internal seam for synthetic tests; the CLI always uses the sealed defaults."""

    authorize: Callable[..., FinalTestAuthorization]
    build_data: Callable[..., object]
    build_execution_inputs: Callable[..., Mapping[str, object]]
    build_signals: Callable[..., FinalTestSignals]
    run_backtest: Callable[..., FinalTestBacktestResult]
    build_metrics: Callable[..., tuple[pl.DataFrame, pl.DataFrame]]
    render_report: Callable[..., str]


def run_final_test_release(
    *,
    code_root: Path,
    data_root: Path,
    opening_token_path: Path,
    approval_key: bytes,
    attempt_id: str | None = None,
    run_id: str | None = None,
    security_event_coverage_path: Path | None = None,
    steps: FinalTestPipelineSteps | None = None,
) -> FinalTestPipelineResult:
    """Run and publish the single authorized 2022--2025 final-test attempt."""
    code_root = code_root.resolve()
    data_root = data_root.resolve()
    final_root = data_root / "processed/final_test"
    registry_root = final_root / "attempts"
    _assert_safe_roots(data_root, final_root)
    if (final_root / "CURRENT.json").is_file():
        return _recover_or_reject_current(final_root, registry_root)
    assert_no_authoritative_success(registry_root)
    if attempt_id is not None:
        _validate_publication_id(attempt_id)
    if run_id is not None:
        _validate_publication_id(run_id)
    if steps is None and security_event_coverage_path is None:
        raise ValueError("official security-event coverage path is required")
    selected = steps or _default_steps()
    authorization = selected.authorize(
        code_root=code_root,
        robustness_root=data_root / "processed/robustness",
        validation_root=data_root / "processed/validation_evaluation",
        opening_token_path=opening_token_path,
        approval_key=approval_key,
        registry_root=registry_root,
        requested_start=FINAL_TEST_START,
        requested_end=FINAL_TEST_END,
        attempt_id=attempt_id,
    )
    _assert_authorization(authorization)
    actual_run_id = run_id or authorization.attempt_id
    _validate_publication_id(actual_run_id)
    attempt_root = final_root / "attempt_runs" / authorization.attempt_id
    if attempt_root.exists() or attempt_root.is_symlink():
        raise FileExistsError(f"final-test attempt artifacts already exist: {attempt_root}")
    datasets = attempt_root / "datasets"
    artifacts = attempt_root / "artifacts"
    datasets.mkdir(parents=True)
    artifacts.mkdir()
    started_at = datetime.now(timezone.utc).isoformat()
    published_release: PublishedRelease | None = None
    try:
        config = _load_frozen_config(code_root, data_root)
        selected.build_data(
            config,
            authorization,
            FINAL_TEST_START,
            FINAL_TEST_END,
            code_root=code_root,
        )
        execution_manifest = selected.build_execution_inputs(
            authorization,
            code_root=code_root,
            data_root=data_root,
            final_root=final_root,
            security_event_coverage_path=security_event_coverage_path,
        )
        signals = selected.build_signals(
            authorization,
            code_root=code_root,
            final_root=final_root,
        )
        backtest = selected.run_backtest(
            authorization,
            signals,
            code_root=code_root,
            final_root=final_root,
        )
        _write_attempt_core(datasets, artifacts, signals, backtest)
        if not backtest.publishable:
            reason = "; ".join(backtest.gate_failures) or "publishable=false"
            _write_attempt_manifest(
                attempt_root,
                authorization=authorization,
                status="failed",
                reason=reason,
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

        test_metrics, comparison = selected.build_metrics(
            authorization,
            signals,
            backtest,
            code_root=code_root,
            data_root=data_root,
        )
        test_metrics.write_parquet(datasets / "final_test_metrics.parquet")
        comparison.write_parquet(datasets / "period_comparison.parquet")
        completed_at = datetime.now(timezone.utc).isoformat()
        if steps is None:
            resolution = resolve_final_test_data_panel(final_root)
            _copy_release_inputs(
                panel_root=resolution.root,
                execution_root=(
                    final_root / "attempt_inputs" / authorization.attempt_id
                ),
                source_root=final_root / "execution_input_sources",
                datasets=datasets,
                artifacts=artifacts,
            )
        upstream = _upstream_identity(
            data_root,
            authorization,
            execution_manifest,
            allow_missing=steps is not None,
            staged_root=attempt_root if steps is None else None,
        )
        lineage = _lineage(
            authorization,
            upstream=upstream,
            started_at=started_at,
            completed_at=completed_at,
        )
        report = selected.render_report(
            authorization,
            comparison,
            code_root=code_root,
            registry_root=registry_root,
        )
        (artifacts / "report.md").write_text(report, encoding="utf-8")
        _write_json(artifacts / "execution_inputs.json", dict(execution_manifest))
        _write_json(artifacts / "lineage_preview.json", lineage)
        _write_json(
            artifacts / "checkpoint_c.json",
            {
                "status": "required",
                "development_stopped": True,
                "delivery_started": False,
            },
        )
        _write_attempt_manifest(
            attempt_root,
            authorization=authorization,
            status="publishable",
            reason="all frozen final-test publication gates passed",
        )
        append_prepared_publication(
            registry_root,
            attempt_id=authorization.attempt_id,
            release_run_id=actual_run_id,
            sealed_protocol_sha256=authorization.sealed_protocol_sha256,
        )
        release = publish_release(
            final_root,
            run_id=actual_run_id,
            staged_datasets=datasets,
            staged_artifacts=artifacts,
            lineage=lineage,
            manifest_metadata={
                "stage": "final_test",
                "period": [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()],
                "attempt_id": authorization.attempt_id,
                "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
                "publishable": True,
            },
        )
        published_release = release
        append_attempt_outcome(
            registry_root,
            attempt_id=authorization.attempt_id,
            status="succeeded",
            authoritative=True,
            reason="all frozen final-test publication gates passed",
            release_run_id=release.run_id,
            release_manifest_sha256=release.manifest_sha256,
        )
        return FinalTestPipelineResult(
            authorization.attempt_id,
            True,
            attempt_root,
            release,
            (),
        )
    except BaseException as error:
        if attempt_root.exists() and not (attempt_root / "attempt_manifest.json").exists():
            _write_attempt_manifest(
                attempt_root,
                authorization=authorization,
                status="failed",
                reason=f"{type(error).__name__}: {error}",
            )
        outcome = registry_root / f"{authorization.attempt_id}.outcome.json"
        if published_release is None and not outcome.exists():
            append_attempt_outcome(
                registry_root,
                attempt_id=authorization.attempt_id,
                status="failed",
                authoritative=False,
                reason=f"{type(error).__name__}: {error}",
            )
        raise


def _default_steps() -> FinalTestPipelineSteps:
    return FinalTestPipelineSteps(
        authorize=authorize_final_test,
        build_data=build_or_reuse_final_test_daily_panel,
        build_execution_inputs=build_final_execution_inputs,
        build_signals=build_final_test_signals,
        run_backtest=run_final_test_backtest,
        build_metrics=build_final_metrics,
        render_report=build_final_report,
    )


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
    claim = json.loads(claim_path.read_text(encoding="utf-8"))
    status = claim.get("status") if isinstance(claim, dict) else None
    panel = final_root / "daily_panel"
    if status == "failed" and not panel.exists():
        archive_root = final_root / "failed-claims"
        if archive_root.is_symlink():
            raise ValueError("failed claim archive uses a symlink")
        archive_root.mkdir(exist_ok=True)
        archived_attempt = validate_publication_id(str(claim.get("attempt_id", "")))
        digest = hashlib.sha256(claim_path.read_bytes()).hexdigest()[:16]
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
        manifest_sha256 = _claim_manifest_sha256(claim)
        if not prepared_path.exists():
            _write_json_exclusive(
                prepared_path,
                {
                    "attempt_id": attempt_id,
                    "status": "prepared",
                    "claim_sha256": hashlib.sha256(claim_path.read_bytes()).hexdigest(),
                    "data_manifest_sha256": manifest_sha256,
                },
            )
        prepared = _load_prepared_recovery(
            prepared_path,
            attempt_id=attempt_id,
            data_manifest_sha256=manifest_sha256,
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
                attempt_id=attempt_id,
                data_manifest_sha256=_claim_manifest_sha256(claim),
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


def _assert_safe_recovery_root(root: Path) -> None:
    if root.is_symlink():
        raise ValueError("data recovery path uses a symlink")
    root.mkdir(exist_ok=True)


def _load_prepared_recovery(
    path: Path, *, attempt_id: str, data_manifest_sha256: str
) -> Mapping[str, object]:
    if path.is_symlink():
        raise ValueError("prepared data recovery audit uses a symlink")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError("invalid prepared data recovery audit") from error
    if (
        not isinstance(payload, dict)
        or payload.get("attempt_id") != attempt_id
        or payload.get("status") != "prepared"
        or payload.get("data_manifest_sha256") != data_manifest_sha256
        or not isinstance(payload.get("claim_sha256"), str)
        or len(str(payload["claim_sha256"])) != 64
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
    final_root: Path, registry_root: Path
) -> FinalTestPipelineResult:
    release = resolve_current(final_root)
    current = {"run_id": release.run_id, "manifest_sha256": release.manifest_sha256}
    for path in sorted(registry_root.glob("*.prepared.json")):
        prepared = json.loads(path.read_text(encoding="utf-8"))
        if prepared.get("release_run_id") != release.run_id:
            continue
        attempt_id = str(prepared.get("attempt_id", ""))
        lineage = json.loads(release.lineage.read_text(encoding="utf-8"))
        identity = lineage.get("authorization")
        if not isinstance(identity, dict) or (
            identity.get("attempt_id") != attempt_id
            or identity.get("sealed_protocol_sha256")
            != prepared.get("sealed_protocol_sha256")
        ):
            raise ValueError("prepared final-test publication differs from release lineage")
        outcome = registry_root / f"{attempt_id}.outcome.json"
        if outcome.exists():
            raise ValueError("authoritative final-test run already succeeded")
        recover_prepared_publication(
            registry_root, attempt_id=attempt_id, current=current
        )
        return FinalTestPipelineResult(
            attempt_id,
            True,
            final_root / "attempt_runs" / attempt_id,
            release,
            (),
        )
    raise ValueError("authoritative final-test run already succeeded")


def _copy_release_inputs(
    *,
    panel_root: Path,
    execution_root: Path,
    source_root: Path | None = None,
    datasets: Path,
    artifacts: Path,
) -> None:
    checked = (panel_root, execution_root, *((source_root,) if source_root else ()))
    if any(path.is_symlink() for path in checked):
        raise ValueError("final-test release input uses a symlink")
    shutil.copytree(panel_root, datasets / "final_daily_panel")
    shutil.copytree(execution_root, artifacts / "execution_inputs")
    if source_root is not None:
        shutil.copytree(source_root, artifacts / "execution_sources")


def _write_attempt_core(
    datasets: Path,
    artifacts: Path,
    signals: FinalTestSignals,
    backtest: FinalTestBacktestResult,
) -> None:
    for name, frame in {
        "factor_panel": signals.factor_panel,
        "composite_scores": signals.composite_scores,
        "composite_weights": signals.composite_weights,
        "target_weights": signals.target_weights,
        **backtest.outputs,
    }.items():
        frame.write_parquet(datasets / f"{name}.parquet")
    _write_json(artifacts / "preflight.json", backtest.preflight)
    _write_json(
        artifacts / "runtime_audits.json",
        {key: value for key, value in backtest.audits.items() if not isinstance(value, pl.DataFrame)},
    )
    for name, value in backtest.audits.items():
        if isinstance(value, pl.DataFrame):
            value.write_parquet(artifacts / f"{name}.parquet")


def _write_attempt_manifest(
    root: Path,
    *,
    authorization: FinalTestAuthorization,
    status: str,
    reason: str,
) -> None:
    records = [
        file_record(path, root=root, role=path.parts[-2]).to_dict()
        for path in sorted(item for item in root.rglob("*") if item.is_file())
        if path.name != "attempt_manifest.json"
    ]
    _write_json(
        root / "attempt_manifest.json",
        {
            "attempt_id": authorization.attempt_id,
            "status": status,
            "reason": reason,
            "period": [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()],
            "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
            "git_commit": authorization.git_commit,
            "git_tree": authorization.git_tree,
            "files": records,
        },
    )


def _upstream_identity(
    data_root: Path,
    authorization: FinalTestAuthorization,
    execution_manifest: Mapping[str, object],
    *,
    allow_missing: bool,
    staged_root: Path | None,
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
    elif allow_missing:
        final_data = []
    else:
        raise ValueError("final-test data identity is incomplete")
    staged_inputs = (
        [
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
        if staged_root is not None
        else []
    )
    try:
        robustness = resolve_current(data_root / "processed/robustness")
        validation = resolve_current(data_root / "processed/validation_evaluation")
    except FileNotFoundError:
        if not allow_missing:
            raise ValueError("final-test upstream release identity is incomplete") from None
        return {
            "robustness_release": authorization.robustness_release,
            "final_test_data": final_data,
            "self_contained_inputs": staged_inputs,
            "execution_inputs": dict(execution_manifest),
        }
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
    started_at: str,
    completed_at: str,
) -> dict[str, object]:
    return {
        "stage": "final_test",
        "period": [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()],
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
        "execution": {"started_at": started_at, "completed_at": completed_at},
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
