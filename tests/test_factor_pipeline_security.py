from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import pytest
import polars as pl
import yaml

from factor_pipeline_fixtures import write_prepared_daily_panel
from ashare_multifactor.factors.definitions import FACTOR_DEFINITIONS
from ashare_multifactor.research.factor_outputs import expected_stage_outputs
import ashare_multifactor.research.factor_outputs as outputs_module
import ashare_multifactor.research.factor_pipeline as pipeline_module
from ashare_multifactor.research.factor_pipeline import run_factor_pipeline


def _rewrite_paths(config_path: Path, **overrides: Path) -> None:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["paths"].update({name: str(path) for name, path in overrides.items()})
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _tree_hashes(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _read_lineage(factor_root: Path) -> dict[str, object]:
    return json.loads((factor_root / "lineage.json").read_text(encoding="utf-8"))


def _write_lineage(factor_root: Path, lineage: dict[str, object]) -> None:
    (factor_root / "lineage.json").write_text(
        json.dumps(lineage, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _fail_if_daily_is_read(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("daily panel was read before lineage rejection")


@pytest.mark.parametrize(
    ("override", "target"),
    (
        ("processed", "raw_unadjusted"),
        ("artifacts", "raw_backward_adjusted"),
        ("artifacts", "processed_child"),
    ),
)
def test_unsafe_factor_roots_are_rejected_without_writes(
    tmp_path: Path,
    override: str,
    target: str,
) -> None:
    config_path, _, _ = write_prepared_daily_panel(tmp_path)
    raw_root = tmp_path / "raw"
    adjusted_root = tmp_path / "adj"
    raw_root.mkdir()
    adjusted_root.mkdir()
    sentinel = raw_root / "sentinel.txt"
    sentinel.write_text("read-only", encoding="utf-8")
    before = _tree_hashes(tmp_path)
    targets = {
        "raw_unadjusted": raw_root,
        "raw_backward_adjusted": adjusted_root,
        "processed_child": tmp_path / "processed" / "nested-artifacts",
    }
    _rewrite_paths(config_path, **{override: targets[target]})
    before = _tree_hashes(tmp_path)

    with pytest.raises(ValueError, match="overlap"):
        run_factor_pipeline(config_path, "audit")

    assert sentinel.read_text(encoding="utf-8") == "read-only"
    assert _tree_hashes(tmp_path) == before


def test_factor_research_symlink_escape_is_rejected_without_writes(tmp_path: Path) -> None:
    config_path, daily_root, _ = write_prepared_daily_panel(tmp_path)
    factor_root = daily_root.parent
    escaped_root = tmp_path / "escaped-factor-research"
    shutil.move(factor_root, escaped_root)
    factor_root.symlink_to(escaped_root, target_is_directory=True)
    before = _tree_hashes(tmp_path)

    with pytest.raises(ValueError, match="symlink"):
        run_factor_pipeline(config_path, "audit")

    assert _tree_hashes(tmp_path) == before


def test_lineage_absolute_input_path_is_rejected_before_data_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, daily_root, _ = write_prepared_daily_panel(tmp_path)
    factor_root = daily_root.parent
    run_factor_pipeline(config_path, "audit")
    lineage = _read_lineage(factor_root)
    audit = lineage["stages"]["audit"]
    audit["inputs"][0]["path"] = str(factor_root / "daily_panel" / "manifest.json")
    _write_lineage(factor_root, lineage)
    monkeypatch.setattr(pipeline_module, "_read_daily", _fail_if_daily_is_read)

    with pytest.raises(ValueError, match="lineage.*path"):
        run_factor_pipeline(config_path, "factors")


def test_lineage_parent_escape_output_is_rejected_before_data_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, daily_root, _ = write_prepared_daily_panel(tmp_path)
    factor_root = daily_root.parent
    run_factor_pipeline(config_path, "audit")
    readiness = factor_root / "data_readiness.json"
    escaped = factor_root.parent / "escaped-readiness.json"
    shutil.copy2(readiness, escaped)
    lineage = _read_lineage(factor_root)
    lineage["stages"]["audit"]["outputs"][0]["path"] = "../escaped-readiness.json"
    _write_lineage(factor_root, lineage)
    monkeypatch.setattr(pipeline_module, "_read_daily", _fail_if_daily_is_read)

    with pytest.raises(ValueError, match="lineage.*path"):
        run_factor_pipeline(config_path, "factors")


def test_lineage_symlink_output_is_rejected_before_data_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, daily_root, _ = write_prepared_daily_panel(tmp_path)
    factor_root = daily_root.parent
    run_factor_pipeline(config_path, "audit")
    readiness = factor_root / "data_readiness.json"
    escaped = tmp_path / "escaped-readiness.json"
    shutil.copy2(readiness, escaped)
    readiness.unlink()
    readiness.symlink_to(escaped)
    monkeypatch.setattr(pipeline_module, "_read_daily", _fail_if_daily_is_read)

    with pytest.raises(ValueError, match="symlink"):
        run_factor_pipeline(config_path, "factors")


def test_lineage_duplicate_output_record_is_rejected_before_data_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, daily_root, _ = write_prepared_daily_panel(tmp_path)
    factor_root = daily_root.parent
    run_factor_pipeline(config_path, "audit")
    lineage = _read_lineage(factor_root)
    outputs = lineage["stages"]["audit"]["outputs"]
    outputs.append(dict(outputs[0]))
    _write_lineage(factor_root, lineage)
    monkeypatch.setattr(pipeline_module, "_read_daily", _fail_if_daily_is_read)

    with pytest.raises(ValueError, match="duplicate"):
        run_factor_pipeline(config_path, "factors")


def test_lineage_input_digest_is_validated_before_data_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, daily_root, _ = write_prepared_daily_panel(tmp_path)
    factor_root = daily_root.parent
    run_factor_pipeline(config_path, "audit")
    lineage = _read_lineage(factor_root)
    lineage["stages"]["audit"]["inputs"][0]["sha256"] = "0" * 64
    _write_lineage(factor_root, lineage)
    monkeypatch.setattr(pipeline_module, "_read_daily", _fail_if_daily_is_read)

    with pytest.raises(ValueError, match="digest"):
        run_factor_pipeline(config_path, "factors")


def test_lineage_empty_audit_outputs_are_rejected_before_data_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, daily_root, _ = write_prepared_daily_panel(tmp_path)
    factor_root = daily_root.parent
    run_factor_pipeline(config_path, "audit")
    lineage = _read_lineage(factor_root)
    lineage["stages"]["audit"]["outputs"] = []
    _write_lineage(factor_root, lineage)
    monkeypatch.setattr(pipeline_module, "_read_daily", _fail_if_daily_is_read)

    with pytest.raises(ValueError, match="output set"):
        run_factor_pipeline(config_path, "factors")


def test_lineage_missing_registered_factor_output_is_rejected_before_evaluation_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, daily_root, _ = write_prepared_daily_panel(tmp_path)
    factor_root = daily_root.parent
    run_factor_pipeline(config_path, "audit")
    run_factor_pipeline(config_path, "factors")
    lineage = _read_lineage(factor_root)
    outputs = lineage["stages"]["factors"]["outputs"]
    outputs.pop(0)
    _write_lineage(factor_root, lineage)

    def fail_if_parquet_is_read(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("factor output was read before lineage rejection")

    monkeypatch.setattr(pipeline_module.pl, "read_parquet", fail_if_parquet_is_read)

    with pytest.raises(ValueError, match="output set"):
        run_factor_pipeline(config_path, "evaluate")


def test_report_manifest_binds_research_context_artifacts_and_lineage(tmp_path: Path) -> None:
    config_path, daily_root, _ = write_prepared_daily_panel(tmp_path)
    factor_root = daily_root.parent
    artifact_root = tmp_path / "artifacts" / "factor_research"

    run_factor_pipeline(config_path, "all")

    report_manifest = factor_root / "report_manifest.json"
    artifact_manifest = artifact_root / "manifest.json"
    assert report_manifest.read_bytes() == artifact_manifest.read_bytes()
    manifest = json.loads(report_manifest.read_text(encoding="utf-8"))
    assert len(manifest["research_identity"]) == 64
    assert len(manifest["evaluate_outputs_sha256"]) == 64
    assert manifest["files"]
    lineage = _read_lineage(factor_root)
    report_outputs = lineage["stages"]["report"]["outputs"]
    assert [record["path"] for record in report_outputs] == ["report_manifest.json"]
    assert report_outputs[0]["sha256"] == hashlib.sha256(report_manifest.read_bytes()).hexdigest()
    assert report_outputs[0]["size_bytes"] == report_manifest.stat().st_size


def test_second_report_rejects_tampered_artifact_before_republishing(tmp_path: Path) -> None:
    config_path, _, _ = write_prepared_daily_panel(tmp_path)
    artifact = tmp_path / "artifacts/factor_research/factor_summary.csv"
    run_factor_pipeline(config_path, "all")
    artifact.write_bytes(artifact.read_bytes() + b"\ntampered")
    tampered = artifact.read_bytes()

    with pytest.raises(ValueError, match="digest"):
        run_factor_pipeline(config_path, "report")

    assert artifact.read_bytes() == tampered


def test_second_report_rejects_manifest_context_mismatch(tmp_path: Path) -> None:
    config_path, daily_root, _ = write_prepared_daily_panel(tmp_path)
    factor_root = daily_root.parent
    artifact_manifest = tmp_path / "artifacts/factor_research/manifest.json"
    report_manifest = factor_root / "report_manifest.json"
    run_factor_pipeline(config_path, "all")
    manifest = json.loads(report_manifest.read_text(encoding="utf-8"))
    manifest["research_identity"] = "0" * 64
    encoded = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    report_manifest.write_text(encoded, encoding="utf-8")
    artifact_manifest.write_text(encoded, encoding="utf-8")
    lineage = _read_lineage(factor_root)
    report_record = lineage["stages"]["report"]["outputs"][0]
    report_record["sha256"] = hashlib.sha256(report_manifest.read_bytes()).hexdigest()
    report_record["size_bytes"] = report_manifest.stat().st_size
    _write_lineage(factor_root, lineage)

    with pytest.raises(ValueError, match="research identity"):
        run_factor_pipeline(config_path, "report")


def test_second_report_rejects_empty_report_output_lineage(tmp_path: Path) -> None:
    config_path, daily_root, _ = write_prepared_daily_panel(tmp_path)
    factor_root = daily_root.parent
    run_factor_pipeline(config_path, "all")
    lineage = _read_lineage(factor_root)
    lineage["stages"]["report"]["outputs"] = []
    _write_lineage(factor_root, lineage)

    with pytest.raises(ValueError, match="output set"):
        run_factor_pipeline(config_path, "report")


def test_failed_audit_removes_current_and_downstream_completions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, daily_root, _ = write_prepared_daily_panel(tmp_path)
    factor_root = daily_root.parent
    artifact_root = tmp_path / "artifacts/factor_research"
    run_factor_pipeline(config_path, "all")

    def fail_readiness_write(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected audit write failure")

    monkeypatch.setattr(pipeline_module, "write_data_readiness", fail_readiness_write)
    with pytest.raises(RuntimeError, match="injected audit write failure"):
        run_factor_pipeline(config_path, "audit")

    lineage = _read_lineage(factor_root)
    assert lineage["stages"] == {}
    for stage in ("audit", "factors", "evaluate", "report"):
        assert not any(
            path.exists()
            for path in expected_stage_outputs(factor_root, stage, tuple(FACTOR_DEFINITIONS))
        )
    assert not artifact_root.exists()


def test_failed_factor_write_removes_partial_outputs_and_downstream_completions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, daily_root, _ = write_prepared_daily_panel(tmp_path)
    factor_root = daily_root.parent
    artifact_root = tmp_path / "artifacts/factor_research"
    run_factor_pipeline(config_path, "all")

    def fail_factor_write(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected factor write failure")

    monkeypatch.setattr(pipeline_module, "write_parquet_atomic", fail_factor_write)
    with pytest.raises(RuntimeError, match="injected factor write failure"):
        run_factor_pipeline(config_path, "factors")

    lineage = _read_lineage(factor_root)
    assert set(lineage["stages"]) == {"audit"}
    for stage in ("factors", "evaluate", "report"):
        assert not any(
            path.exists()
            for path in expected_stage_outputs(factor_root, stage, tuple(FACTOR_DEFINITIONS))
        )
    assert not artifact_root.exists()


def test_failed_evaluation_write_removes_partial_outputs_and_report_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, daily_root, _ = write_prepared_daily_panel(tmp_path)
    factor_root = daily_root.parent
    artifact_root = tmp_path / "artifacts/factor_research"
    run_factor_pipeline(config_path, "all")
    original_write = pipeline_module.write_parquet_atomic
    writes = 0

    def fail_second_write(path: Path, frame: object) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise RuntimeError("injected evaluation write failure")
        original_write(path, frame)

    monkeypatch.setattr(pipeline_module, "write_parquet_atomic", fail_second_write)
    with pytest.raises(RuntimeError, match="injected evaluation write failure"):
        run_factor_pipeline(config_path, "evaluate")

    lineage = _read_lineage(factor_root)
    assert set(lineage["stages"]) == {"audit", "factors"}
    for stage in ("evaluate", "report"):
        assert not any(
            path.exists()
            for path in expected_stage_outputs(factor_root, stage, tuple(FACTOR_DEFINITIONS))
        )
    assert not artifact_root.exists()


def test_report_publish_failure_never_records_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, daily_root, _ = write_prepared_daily_panel(tmp_path)
    factor_root = daily_root.parent
    for stage in ("audit", "factors", "evaluate"):
        run_factor_pipeline(config_path, stage)

    def fail_publish(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected report publish failure")

    monkeypatch.setattr(pipeline_module, "publish_report", fail_publish)
    with pytest.raises(RuntimeError, match="injected report publish failure"):
        run_factor_pipeline(config_path, "report")

    lineage = _read_lineage(factor_root)
    assert "report" not in lineage["stages"]
    assert not (factor_root / "report_manifest.json").exists()
    assert not (tmp_path / "artifacts/factor_research").exists()


def test_report_republish_failure_removes_previously_valid_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, daily_root, _ = write_prepared_daily_panel(tmp_path)
    factor_root = daily_root.parent
    artifact_root = tmp_path / "artifacts/factor_research"
    run_factor_pipeline(config_path, "all")
    assert artifact_root.is_dir()

    def fail_publish(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected report republish failure")

    monkeypatch.setattr(pipeline_module, "publish_report", fail_publish)
    with pytest.raises(RuntimeError, match="injected report republish failure"):
        run_factor_pipeline(config_path, "report")

    lineage = _read_lineage(factor_root)
    assert "report" not in lineage["stages"]
    assert not (factor_root / "report_manifest.json").exists()
    assert not artifact_root.exists()


@pytest.mark.parametrize("file_type", ("json", "parquet"))
def test_atomic_file_write_failure_removes_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_type: str,
) -> None:
    output = tmp_path / f"result.{file_type}"

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected atomic replace failure")

    monkeypatch.setattr(outputs_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected atomic replace failure"):
        if file_type == "json":
            outputs_module.write_json_atomic(output, {"status": "ready"})
        else:
            outputs_module.write_parquet_atomic(output, pl.DataFrame({"value": [1]}))

    assert not output.exists()
    assert not output.with_suffix(output.suffix + ".tmp").exists()


def test_directory_publish_recovers_from_failed_rollback_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "published"
    staging = tmp_path / "staging"
    target.mkdir()
    staging.mkdir()
    (target / "state.txt").write_text("old", encoding="utf-8")
    (staging / "state.txt").write_text("new", encoding="utf-8")
    original_replace = outputs_module.os.replace
    calls = 0

    def fail_publish_and_rollback(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls in {2, 3}:
            raise OSError(f"injected rename failure {calls}")
        original_replace(source, destination)

    monkeypatch.setattr(outputs_module.os, "replace", fail_publish_and_rollback)

    with pytest.raises(RuntimeError, match="restored target from backup copy"):
        outputs_module.replace_directory(staging, target)

    assert (target / "state.txt").read_text(encoding="utf-8") == "old"
    backups = list(tmp_path.glob(".published-*.backup"))
    assert len(backups) == 1
    assert (backups[0] / "state.txt").read_text(encoding="utf-8") == "old"
