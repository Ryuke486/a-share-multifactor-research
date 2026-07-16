from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import version
import hashlib
import hmac
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
    _slice_backtest_period,
    build_final_metrics,
    build_final_report,
)
from ashare_multifactor.final_test.signals import (
    FinalTestSignals,
    build_final_test_signals,
    _load_frozen_config,
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
    authorization = authorize_final_test(
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
        build_or_reuse_final_test_daily_panel(
            config,
            authorization,
            FINAL_TEST_START,
            FINAL_TEST_END,
            code_root=code_root,
        )
        execution_manifest = build_final_execution_inputs(
            authorization,
            code_root=code_root,
            data_root=data_root,
            final_root=final_root,
            security_event_coverage_path=security_event_coverage_path,
            corporate_action_coverage_root=corporate_action_coverage_root,
        )
        signals = build_final_test_signals(
            authorization,
            code_root=code_root,
            final_root=final_root,
        )
        backtest = run_final_test_backtest(
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

        test_metrics, comparison = build_final_metrics(
            authorization,
            signals,
            backtest,
            code_root=code_root,
            data_root=data_root,
        )
        test_metrics.write_parquet(datasets / "final_test_metrics.parquet")
        comparison.write_parquet(datasets / "period_comparison.parquet")
        completed_at = datetime.now(timezone.utc).isoformat()
        resolution = resolve_final_test_data_panel(final_root)
        _copy_release_inputs(
            panel_root=resolution.root,
            execution_root=(final_root / "attempt_inputs" / authorization.attempt_id),
            source_root=final_root / "execution_input_sources",
            datasets=datasets,
            artifacts=artifacts,
        )
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
        )
        report = build_final_report(
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
        _assert_release_date_bounds(attempt_root)
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
        frame.write_parquet(datasets / f"{name}.parquet")
    _write_json(artifacts / "preflight.json", backtest.preflight)
    _write_json(
        artifacts / "runtime_audits.json",
        {key: value for key, value in backtest.audits.items() if not isinstance(value, pl.DataFrame)},
    )
    for name, value in backtest.audits.items():
        if isinstance(value, pl.DataFrame):
            _slice_audit_frame(name, value).write_parquet(
                artifacts / f"{name}.parquet"
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
            "robustness_release": authorization.robustness_release,
            "robustness_manifest_sha256": authorization.robustness_manifest_sha256,
            "robustness_lineage_sha256": authorization.robustness_lineage_sha256,
            "files": records,
        },
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
