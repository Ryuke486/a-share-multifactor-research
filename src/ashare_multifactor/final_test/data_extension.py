from __future__ import annotations

from dataclasses import replace
from datetime import date
import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

from ashare_multifactor.audit.publication import resolve_current
from ashare_multifactor.config import ResearchConfig
from ashare_multifactor.data.build import BuildManifest, build_parquet_dataset
from ashare_multifactor.data.discovery import discover_daily_pairs
from ashare_multifactor.final_test.data_inventory import (
    build_data_manifest as _build_data_manifest,
    build_input_inventory,
    file_identity as _file_identity,
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

    processed = config.paths.processed
    final_root, target = _validate_target_paths(config)
    _verify_authorization(config, authorization, code_root)
    claim_path = _claim_build(final_root, authorization)

    temporary_processed = processed / f".final-test-data-{uuid4().hex}.tmp"
    temporary_target = temporary_processed / "validation_evaluation/daily_panel"
    build_config = replace(
        config,
        paths=replace(config.paths, processed=temporary_processed),
        validation=config.test,
    )
    published = False
    try:
        inventory = _input_inventory(config, start, end)
        manifest = build_parquet_dataset(
            build_config,
            start,
            end,
            output_root=temporary_target,
        )
        if _input_inventory(config, start, end) != inventory:
            raise ValueError("final-test raw input files changed during build")
        _write_json(temporary_target / "input_files.json", inventory)
        _write_json(
            temporary_target / "data_manifest.json",
            _build_data_manifest(temporary_target),
        )
        verify_final_test_data_panel(temporary_target)
        data_manifest_identity = _file_identity(
            temporary_target / "data_manifest.json",
            relative_path="daily_panel/data_manifest.json",
        )
        _validate_target_paths(config)
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"final-test daily panel already exists: {target}")
        _update_claim(
            claim_path,
            authorization,
            status="publishing",
            data_manifest=data_manifest_identity,
        )
        os.replace(temporary_target, target)
        published = True
        try:
            _update_claim(
                claim_path,
                authorization,
                status="published",
                data_manifest=data_manifest_identity,
            )
        except BaseException as error:
            raise RuntimeError(
                "final-test data is a published artifact that requires recovery/audit"
            ) from error
        return manifest
    except BaseException as error:
        if not published:
            _update_claim(
                claim_path,
                authorization,
                status="failed",
                error=error,
            )
        raise
    finally:
        shutil.rmtree(temporary_processed, ignore_errors=True)


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
