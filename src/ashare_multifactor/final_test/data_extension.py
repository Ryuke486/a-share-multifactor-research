from __future__ import annotations

from dataclasses import replace
from datetime import date
import json
import os
from pathlib import Path
import subprocess
import hashlib
from uuid import uuid4

from ashare_multifactor.audit.publication import resolve_current
from ashare_multifactor.audit.records import sha256_file
from ashare_multifactor.audit.secure_tree import read_frozen_tree_at
from ashare_multifactor.config import Period, ResearchConfig
from ashare_multifactor.data.build import BuildManifest, build_parquet_dataset
from ashare_multifactor.data.discovery import DailyFilePair, discover_daily_pairs
from ashare_multifactor.final_test.data_inventory import (
    build_input_inventory,
    file_identity as _file_identity,
    load_bound_input_pairs,
    verify_file_identity as _verify_file_identity,
    verify_final_test_data_panel,
    write_json as _write_json,
)
from ashare_multifactor.final_test.data_publication import (
    FinalTestDataResolution,
    claim_build as _claim_build,
    resolve_final_test_data_panel,
    update_claim as _update_claim,
)
from ashare_multifactor.final_test.gate import (
    FINAL_TEST_END,
    FINAL_TEST_START,
    FinalTestAuthorization,
)
from ashare_multifactor.final_test.panel_binding import (
    FrozenPanelSnapshot,
    bind_panel_snapshot,
)
from ashare_multifactor.final_test.recovery_secure_fs import (
    assert_directory_entry,
    atomic_rename_no_replace_at,
    directory_identity,
    open_directory_at,
    opened_directory,
    opened_directory_at,
    read_bytes_at,
    write_bytes_exclusive_at,
)
from ashare_multifactor.final_test.secure_stage2_output import (
    SecureStage2Output,
    opened_stage2_directories,
)


__all__ = [
    "FinalTestDataResolution",
    "build_final_test_daily_panel",
    "resolve_final_test_data_panel",
    "verify_final_test_data_panel",
]


def build_final_test_daily_panel(
    config: ResearchConfig,
    authorization: FinalTestAuthorization,
    start: date,
    end: date,
    *,
    code_root: Path,
) -> BuildManifest:
    """Build the authorized one-shot panel through the unchanged Stage-2 pipeline."""
    if not isinstance(authorization, FinalTestAuthorization):
        raise TypeError("authorization must be a FinalTestAuthorization")
    sealed_period = (FINAL_TEST_START, FINAL_TEST_END)
    if authorization.test_period != sealed_period:
        raise ValueError("authorization period differs from the sealed final-test period")
    if (start, end) != sealed_period:
        raise ValueError("build dates must equal the exact final-test period")
    if (config.test.start, config.test.end) != sealed_period:
        raise ValueError("configured test period differs from the sealed final-test period")

    final_root, target = _validate_target_paths(config)
    _verify_authorization(config, authorization, code_root)
    claim_path = final_root / "data-build-claim.json"
    if claim_path.exists() or claim_path.is_symlink():
        raise ValueError("final-test data build is already claimed")
    inventory_root = final_root / "data-build-inputs"
    if inventory_root.is_symlink():
        raise ValueError("final-test bound input inventory path uses a symlink")
    inventory_root.mkdir(parents=True, exist_ok=True)
    inventory_path = inventory_root / f"{authorization.attempt_id}.json"
    if inventory_path.exists() or inventory_path.is_symlink():
        inventory, pairs = _load_orphan_input_inventory(
            inventory_path,
            config=config,
            authorization=authorization,
            start=start,
            end=end,
        )
    else:
        try:
            inventory = _bound_input_inventory(
                _input_inventory(config, start, end),
                authorization,
            )
        except Exception as error:
            claim_path = _claim_build(final_root, authorization)
            _update_claim(claim_path, authorization, status="failed", error=error)
            raise
        _write_json(inventory_path, inventory)
        pairs = load_bound_input_pairs(
            config,
            inventory,
            start=start,
            end=end,
        )
    staging_relative_path = f"data-staging/{authorization.attempt_id}"
    claim_path = _claim_build(
        final_root,
        authorization,
        input_inventory=_file_identity(
            inventory_path,
            relative_path=f"data-build-inputs/{authorization.attempt_id}.json",
        ),
        staging_relative_path=staging_relative_path,
    )
    return _build_claimed_panel(
        config,
        authorization,
        start,
        end,
        claim_path=claim_path,
        target=target,
        inventory=inventory,
        pairs=pairs,
    )


def _bound_input_inventory(
    inventory: dict[str, object],
    authorization: FinalTestAuthorization,
) -> dict[str, object]:
    if set(inventory) != {"schema_version", "period", "pairs"}:
        raise ValueError("invalid final-test input inventory")
    return {
        **inventory,
        "authorization_identity": _authorization_identity(authorization),
    }


def _load_orphan_input_inventory(
    path: Path,
    *,
    config: ResearchConfig,
    authorization: FinalTestAuthorization,
    start: date,
    end: date,
) -> tuple[dict[str, object], list[DailyFilePair]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("orphan final-test input inventory path is invalid")
    try:
        inventory = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("orphan final-test input inventory is invalid") from error
    if (
        not isinstance(inventory, dict)
        or set(inventory)
        != {"schema_version", "period", "pairs", "authorization_identity"}
        or inventory.get("authorization_identity")
        != _authorization_identity(authorization)
    ):
        raise ValueError("orphan final-test input inventory identity differs")
    pairs = load_bound_input_pairs(config, inventory, start=start, end=end)
    return inventory, pairs


def _authorization_identity(
    authorization: FinalTestAuthorization,
) -> dict[str, object]:
    return {
        "attempt_id": authorization.attempt_id,
        "approval_id": authorization.approval_id,
        "registered_at": authorization.registered_at,
        "git_commit": authorization.git_commit,
        "git_tree": authorization.git_tree,
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "robustness_release": authorization.robustness_release,
        "robustness_manifest_sha256": authorization.robustness_manifest_sha256,
        "robustness_lineage_sha256": authorization.robustness_lineage_sha256,
    }


def _build_claimed_panel(
    config: ResearchConfig,
    authorization: FinalTestAuthorization,
    start: date,
    end: date,
    *,
    claim_path: Path,
    target: Path,
    inventory: dict[str, object],
    pairs: list[DailyFilePair],
) -> BuildManifest:
    processed = config.paths.processed
    temporary_processed = processed / "final_test/data-staging" / authorization.attempt_id
    if temporary_processed.is_symlink():
        raise ValueError("final-test data staging path uses a symlink")
    if temporary_processed.exists() and not _is_empty_stage2_shell(
        temporary_processed
    ):
        raise FileExistsError("final-test data staging requires recovery")
    temporary_target = temporary_processed / "validation_evaluation/daily_panel"
    build_config = replace(
        config,
        paths=replace(config.paths, processed=temporary_processed),
        validation=config.test,
    )
    published = False
    bound_panel: FrozenPanelSnapshot | None = None
    try:
        with opened_stage2_directories(
            target.parent, authorization.attempt_id
        ) as stage2_directories:
            with SecureStage2Output(
                stage2_directories.validation_fd, temporary_target.name
            ) as secure_output:
                manifest = build_parquet_dataset(
                    build_config,
                    start,
                    end,
                    output_root=temporary_target,
                    discovered_pairs=list(pairs),
                    supported_markets=config.supported_markets,
                    secure_output=secure_output,
                )
            stage2_directories.assert_bound()
            load_bound_input_pairs(config, inventory, start=start, end=end)
            bound_panel, data_manifest_identity = _bind_built_panel(
                temporary_target,
                inventory=inventory,
                manifest=manifest,
                period=Period(start, end),
                target_parent_fd=stage2_directories.validation_fd,
            )
            stage2_directories.assert_bound()
            _validate_target_paths(config)
            if target.exists() or target.is_symlink():
                raise FileExistsError(f"final-test daily panel already exists: {target}")
            _update_claim(
                claim_path,
                authorization,
                status="publishing",
                data_manifest=data_manifest_identity,
            )
            stage2_directories.assert_bound()
            bound_panel_files = bound_panel.read_frozen_files()
            _publish_built_panel(
                temporary_target,
                target,
                expected_source_identity=bound_panel.root_identity,
                source_parent_fd=stage2_directories.validation_fd,
                destination_parent_fd=stage2_directories.final_fd,
                expected_source_files=bound_panel_files,
            )
            bound_panel.read_frozen_files()
            stage2_directories.assert_published(bound_panel.root_identity)
            published = True
            try:
                _update_claim(
                    claim_path,
                    authorization,
                    status="published",
                    data_manifest=data_manifest_identity,
                )
                stage2_directories.assert_bound()
                stage2_directories.assert_published(bound_panel.root_identity)
                stage2_directories.remove_empty_ancestors()
            except Exception as error:
                raise RuntimeError(
                    "final-test data is a published artifact that requires recovery/audit"
                ) from error
            return manifest
    except Exception as error:
        if not published:
            _update_claim(
                claim_path,
                authorization,
                status="failed",
                error=error,
            )
        raise
    except BaseException:
        raise
    finally:
        if bound_panel is not None:
            bound_panel.close()


def _is_empty_stage2_shell(root: Path) -> bool:
    return (
        root.is_dir()
        and not root.is_symlink()
        and {path.name for path in root.iterdir()} == {"validation_evaluation"}
        and not any((root / "validation_evaluation").iterdir())
    )


def _bind_built_panel(
    target: Path,
    *,
    target_parent_fd: int,
    inventory: dict[str, object],
    manifest: BuildManifest,
    period: Period,
) -> tuple[FrozenPanelSnapshot, dict[str, object]]:
    with opened_directory_at(
        target_parent_fd, target.name, label="final-test built panel"
    ) as target_fd:
        stage2_bytes = read_bytes_at(
            target_fd, "manifest.json", label="Stage-2 manifest"
        )
        if json.loads(stage2_bytes) != manifest.to_dict():
            raise ValueError("Stage-2 output identity changed after build")
        inventory_bytes = _json_bytes(inventory)
        write_bytes_exclusive_at(target_fd, "input_files.json", inventory_bytes)
        data_manifest = _data_manifest_from_bytes(
            stage2_bytes, inventory_bytes, inventory
        )
        data_manifest_bytes = _json_bytes(data_manifest)
        write_bytes_exclusive_at(
            target_fd, "data_manifest.json", data_manifest_bytes
        )
    snapshot = bind_panel_snapshot(
        target,
        datasets_fd=target_parent_fd,
        expected_manifest_sha256=hashlib.sha256(data_manifest_bytes).hexdigest(),
        period=period,
    )
    return snapshot, {
        "relative_path": "daily_panel/data_manifest.json",
        "sha256": hashlib.sha256(data_manifest_bytes).hexdigest(),
        "size_bytes": len(data_manifest_bytes),
    }


def _data_manifest_from_bytes(
    stage2_bytes: bytes,
    inventory_bytes: bytes,
    inventory: dict[str, object],
) -> dict[str, object]:
    stage2 = json.loads(stage2_bytes)
    quality = stage2.get("quality_issues")
    pairs = inventory.get("pairs")
    if not isinstance(quality, dict) or not isinstance(pairs, list):
        raise ValueError("cannot bind invalid Stage-2 or input manifest")
    file_count = sum(
        len(pair.get("files", []))
        for pair in pairs
        if isinstance(pair, dict) and isinstance(pair.get("files"), list)
    )
    return {
        "schema_version": "1",
        "stage2_manifest": _bytes_identity(stage2_bytes, "manifest.json"),
        "input_files": {
            **_bytes_identity(inventory_bytes, "input_files.json"),
            "pair_count": len(pairs),
            "file_count": file_count,
        },
        "quality_issues": quality,
    }


def _bytes_identity(payload: bytes, relative_path: str) -> dict[str, object]:
    return {
        "relative_path": relative_path,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }


def _json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _publish_built_panel(
    source: Path,
    destination: Path,
    *,
    expected_source_identity: tuple[int, int] | None = None,
    source_parent_fd: int | None = None,
    destination_parent_fd: int | None = None,
    expected_source_files: dict[str, bytes] | None = None,
) -> None:
    if source_parent_fd is not None or destination_parent_fd is not None:
        if source_parent_fd is None or destination_parent_fd is None:
            raise ValueError("complete held Stage-2 publication descriptors required")
        with opened_directory_at(
            source_parent_fd, source.name, label="final-test data staging"
        ) as source_fd:
            source_identity = directory_identity(source_fd)
            if (
                expected_source_identity is not None
                and source_identity != expected_source_identity
            ):
                raise ValueError("final-test data staging identity changed")
            if (
                expected_source_files is not None
                and read_frozen_tree_at(
                    source_fd, label="bound Stage-2 publication source"
                )
                != expected_source_files
            ):
                raise ValueError("final-test data staging bytes changed")
            atomic_rename_no_replace_at(
                source_parent_fd,
                source.name,
                destination_parent_fd,
                destination.name,
            )
            assert_directory_entry(
                destination_parent_fd,
                destination.name,
                expected=source_identity,
                label="final-test daily panel",
            )
            if (
                expected_source_files is not None
                and read_frozen_tree_at(
                    source_fd, label="published Stage-2 panel"
                )
                != expected_source_files
            ):
                rejected_name = f".rejected-daily-panel-{uuid4().hex}"
                atomic_rename_no_replace_at(
                    destination_parent_fd,
                    destination.name,
                    source_parent_fd,
                    rejected_name,
                )
                assert_directory_entry(
                    source_parent_fd,
                    rejected_name,
                    expected=source_identity,
                    label="rejected Stage-2 panel",
                )
                raise ValueError("final-test data staging bytes changed during publish")
        return
    final_root = destination.parent
    relative_parent = source.parent.relative_to(final_root)
    with opened_directory(
        final_root.parent, label="final-test processed parent"
    ) as final_parent_fd:
        with opened_directory_at(
            final_parent_fd, final_root.name, label="final-test root"
        ) as final_fd:
            final_identity = directory_identity(final_fd)
            descriptors: list[tuple[int, tuple[int, int], int, str]] = []
            current_fd = final_fd
            try:
                for part in relative_parent.parts:
                    parent_fd = current_fd
                    current_fd = open_directory_at(
                        parent_fd, part, label="final-test data staging parent"
                    )
                    descriptors.append(
                        (current_fd, directory_identity(current_fd), parent_fd, part)
                    )
                with opened_directory_at(
                    current_fd, source.name, label="final-test data staging"
                ) as source_fd:
                    source_identity = directory_identity(source_fd)
                    if (
                        expected_source_identity is not None
                        and source_identity != expected_source_identity
                    ):
                        raise ValueError("final-test data staging identity changed")
                    if (
                        expected_source_files is not None
                        and read_frozen_tree_at(
                            source_fd, label="bound Stage-2 publication source"
                        )
                        != expected_source_files
                    ):
                        raise ValueError("final-test data staging bytes changed")
                    atomic_rename_no_replace_at(
                        current_fd,
                        source.name,
                        final_fd,
                        destination.name,
                    )
                    assert_directory_entry(
                        final_fd,
                        destination.name,
                        expected=source_identity,
                        label="final-test daily panel",
                    )
                    if (
                        expected_source_files is not None
                        and read_frozen_tree_at(
                            source_fd, label="published Stage-2 panel"
                        )
                        != expected_source_files
                    ):
                        rejected_name = f".rejected-daily-panel-{uuid4().hex}"
                        atomic_rename_no_replace_at(
                            final_fd,
                            destination.name,
                            current_fd,
                            rejected_name,
                        )
                        assert_directory_entry(
                            current_fd,
                            rejected_name,
                            expected=source_identity,
                            label="rejected Stage-2 panel",
                        )
                        raise ValueError(
                            "final-test data staging bytes changed during publish"
                        )
                    assert_directory_entry(
                        final_parent_fd,
                        final_root.name,
                        expected=final_identity,
                        label="final-test root",
                    )
                _remove_empty_staging_ancestors(descriptors)
            finally:
                for descriptor, _, _, _ in reversed(descriptors):
                    os.close(descriptor)


def _remove_empty_staging_ancestors(
    descriptors: list[tuple[int, tuple[int, int], int, str]],
) -> None:
    # Keep the shared data-staging directory; remove only the attempt-specific
    # descendants that became empty when daily_panel was atomically published.
    for _, identity, parent_fd, name in reversed(descriptors[1:]):
        assert_directory_entry(
            parent_fd,
            name,
            expected=identity,
            label="final-test empty data staging directory",
        )
        os.rmdir(name, dir_fd=parent_fd)


def recover_final_test_daily_panel(
    config: ResearchConfig,
    authorization: FinalTestAuthorization,
    start: date,
    end: date,
    *,
    code_root: Path,
) -> FinalTestDataResolution:
    """Recover the same claim and exact bound inputs without new discovery."""
    if (start, end) != (FINAL_TEST_START, FINAL_TEST_END):
        raise ValueError("build dates must equal the exact final-test period")
    _verify_authorization(config, authorization, code_root)
    final_root = config.paths.processed / "final_test"
    target = final_root / "daily_panel"
    claim_path = final_root / "data-build-claim.json"
    claim = _load_claim_for_authorization(claim_path, authorization)
    status = claim.get("status")
    if status == "failed":
        raise ValueError("final-test data claim is failed and cannot be recovered")
    if status == "published":
        return resolve_final_test_data_panel(final_root)
    if status == "publishing":
        _recover_publishing_claim(
            final_root,
            target=target,
            claim_path=claim_path,
            claim=claim,
            authorization=authorization,
            period=Period(start, end),
        )
        return resolve_final_test_data_panel(final_root)
    if status != "claimed":
        raise ValueError("final-test data claim has an invalid recovery state")
    inventory = _load_claim_inventory(final_root, claim)
    pairs = load_bound_input_pairs(config, inventory, start=start, end=end)
    staging = _claim_staging_path(final_root, claim)
    if staging.exists() and staging != final_root / "data-staging" / authorization.attempt_id:
        raise ValueError("final-test data staging identity differs")
    _build_claimed_panel(
        config,
        authorization,
        start,
        end,
        claim_path=claim_path,
        target=target,
        inventory=inventory,
        pairs=pairs,
    )
    return resolve_final_test_data_panel(final_root)


def _load_claim_for_authorization(
    path: Path, authorization: FinalTestAuthorization
) -> dict[str, object]:
    try:
        claim = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError("invalid final-test data publication claim") from error
    expected = {
        "attempt_id": authorization.attempt_id,
        "approval_id": authorization.approval_id,
        "git_commit": authorization.git_commit,
        "git_tree": authorization.git_tree,
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "robustness_release": authorization.robustness_release,
        "robustness_manifest_sha256": authorization.robustness_manifest_sha256,
        "robustness_lineage_sha256": authorization.robustness_lineage_sha256,
    }
    if not isinstance(claim, dict) or any(
        claim.get(key) != value for key, value in expected.items()
    ):
        raise ValueError("final-test data claim differs from authorization")
    return claim


def _load_claim_inventory(
    final_root: Path, claim: dict[str, object]
) -> dict[str, object]:
    record = claim.get("input_inventory")
    if not isinstance(record, dict):
        raise ValueError("final-test data claim lacks bound input inventory")
    path = _verify_file_identity(final_root, record, "bound input inventory")
    try:
        inventory = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("invalid claim-bound final-test input inventory") from error
    if not isinstance(inventory, dict):
        raise ValueError("invalid claim-bound final-test input inventory")
    return inventory


def _claim_staging_path(final_root: Path, claim: dict[str, object]) -> Path:
    relative = claim.get("staging_relative_path")
    if (
        not isinstance(relative, str)
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
    ):
        raise ValueError("final-test data claim staging identity is invalid")
    path = final_root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(final_root.resolve()):
        raise ValueError("final-test data claim staging identity is invalid")
    return path


def _recover_publishing_claim(
    final_root: Path,
    *,
    target: Path,
    claim_path: Path,
    claim: dict[str, object],
    authorization: FinalTestAuthorization,
    period: Period,
) -> None:
    if not target.exists():
        staging_target = _claim_staging_path(final_root, claim) / "validation_evaluation/daily_panel"
        manifest_record = claim.get("data_manifest")
        expected_manifest_sha256 = (
            manifest_record.get("sha256")
            if isinstance(manifest_record, dict)
            else None
        )
        if not isinstance(expected_manifest_sha256, str):
            raise ValueError("publishing claim lacks bound data manifest identity")
        snapshot = _bind_existing_panel(
            staging_target,
            final_root=final_root,
            expected_manifest_sha256=expected_manifest_sha256,
            period=period,
        )
        try:
            snapshot_files = snapshot.read_frozen_files()
            _publish_built_panel(
                staging_target,
                target,
                expected_source_identity=snapshot.root_identity,
                expected_source_files=snapshot_files,
            )
            snapshot.read_frozen_files()
        finally:
            snapshot.close()
    resolution = resolve_final_test_data_panel(final_root)
    if not resolution.requires_recovery:
        raise ValueError("publishing final-test data claim recovery state is inconsistent")
    _update_claim(
        claim_path,
        authorization,
        status="published",
        data_manifest=claim.get("data_manifest") if isinstance(claim.get("data_manifest"), dict) else None,
    )


def _bind_existing_panel(
    target: Path,
    *,
    final_root: Path,
    expected_manifest_sha256: str,
    period: Period,
) -> FrozenPanelSnapshot:
    relative_parent = target.parent.relative_to(final_root)
    with opened_directory(final_root, label="final-test root") as final_fd:
        descriptors: list[int] = []
        current_fd = final_fd
        try:
            for part in relative_parent.parts:
                current_fd = open_directory_at(
                    current_fd, part, label="final-test data staging parent"
                )
                descriptors.append(current_fd)
            return bind_panel_snapshot(
                target,
                datasets_fd=current_fd,
                expected_manifest_sha256=expected_manifest_sha256,
                period=period,
            )
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)


def _verify_authorization(
    config: ResearchConfig,
    authorization: FinalTestAuthorization,
    code_root: Path,
) -> None:
    code_root = code_root.resolve()
    if _git(code_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("final-test data build requires a clean Git identity")
    commit = _git(code_root, "rev-parse", "HEAD")
    tree = _git(code_root, "rev-parse", "HEAD^{tree}")
    if commit != authorization.git_commit or tree != authorization.git_tree:
        raise ValueError("current Git commit/tree differs from final-test authorization")

    robustness = resolve_current(config.paths.processed / "robustness")
    if robustness.run_id != authorization.robustness_release:
        raise ValueError("current robustness release differs from final-test authorization")
    if (
        robustness.manifest_sha256 != authorization.robustness_manifest_sha256
        or sha256_file(robustness.lineage) != authorization.robustness_lineage_sha256
    ):
        raise ValueError("current robustness identity differs from final-test authorization")
    try:
        sealed = json.loads(
            (robustness.artifacts / "sealed_test_protocol.json").read_text(
                encoding="utf-8"
            )
        )
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError("invalid current sealed final-test protocol") from error
    if sealed.get("sealed_protocol_sha256") != authorization.sealed_protocol_sha256:
        raise ValueError("current sealed protocol differs from final-test authorization")

    attempts = config.paths.processed / "final_test/attempts"
    record_path = attempts / f"{authorization.attempt_id}.json"
    if (
        attempts.is_symlink()
        or record_path.is_symlink()
        or record_path.parent != attempts
        or not record_path.is_file()
    ):
        raise ValueError("canonical final-test attempt record is missing")
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("invalid canonical final-test attempt record") from error
    expected = {
        "attempt_id": authorization.attempt_id,
        "approval_id": authorization.approval_id,
        "registered_at": authorization.registered_at,
        "git_commit": authorization.git_commit,
        "git_tree": authorization.git_tree,
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "robustness_release": authorization.robustness_release,
        "robustness_manifest_sha256": authorization.robustness_manifest_sha256,
        "robustness_lineage_sha256": authorization.robustness_lineage_sha256,
        "status": "registered",
        "authoritative": False,
    }
    if not isinstance(record, dict) or any(
        record.get(key) != value for key, value in expected.items()
    ):
        raise ValueError("canonical final-test attempt record differs from authorization")


def _validate_target_paths(config: ResearchConfig) -> tuple[Path, Path]:
    processed = config.paths.processed
    final_root = processed / "final_test"
    target = final_root / "daily_panel"
    if processed.is_symlink() or final_root.is_symlink() or target.is_symlink():
        raise ValueError("final-test output path uses a symlink alias")
    processed_resolved = processed.resolve()
    expected_final = processed_resolved / "final_test"
    expected_target = expected_final / "daily_panel"
    if final_root.resolve() != expected_final or target.resolve() != expected_target:
        raise ValueError("final-test output path escapes configured processed root")
    for raw in (
        config.paths.raw_unadjusted.resolve(),
        config.paths.raw_backward_adjusted.resolve(),
    ):
        if _paths_overlap(expected_final, raw):
            raise ValueError("final-test output path overlaps raw input")
    if target.exists():
        raise FileExistsError(f"final-test daily panel already exists: {target}")
    return final_root, target


def _input_inventory(
    config: ResearchConfig,
    start: date,
    end: date,
) -> dict[str, object]:
    return build_input_inventory(
        config,
        start,
        end,
        discover=discover_daily_pairs,
    )


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
