from __future__ import annotations

from datetime import date
from pathlib import Path
import json
import shutil
import tomllib

import polars as pl
import pytest
import yaml

from factor_pipeline_fixtures import (
    OBSERVATIONS,
    SYMBOLS,
    file_hashes,
    write_raw_pair,
    write_prepared_daily_panel,
)
from ashare_multifactor.cli import factors as factors_cli
from ashare_multifactor.factors.definitions import FACTOR_DEFINITIONS
import ashare_multifactor.research.factor_pipeline as pipeline_module
from ashare_multifactor.research.factor_pipeline import STAGES, run_factor_pipeline


def _write_config(tmp_path: Path) -> Path:
    raw = yaml.safe_load(Path("configs/research_protocol.yaml").read_text(encoding="utf-8"))
    raw["paths"] = {
        "raw_unadjusted": str(tmp_path / "raw"),
        "raw_backward_adjusted": str(tmp_path / "adj"),
        "processed": str(tmp_path / "processed"),
        "artifacts": str(tmp_path / "artifacts"),
    }
    config_path = tmp_path / "research_protocol.yaml"
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return config_path


def test_factor_pipeline_exposes_only_preregistered_stages() -> None:
    assert STAGES == ("build", "audit", "factors", "evaluate", "report", "all")


def test_unknown_factor_stage_is_rejected_before_creating_outputs(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)

    with pytest.raises(ValueError, match="unsupported factor research stage"):
        run_factor_pipeline(config_path, "backtest")

    assert not (tmp_path / "processed").exists()
    assert not (tmp_path / "artifacts").exists()


def test_all_stage_builds_complete_auditable_synthetic_research(tmp_path: Path) -> None:
    config_path, daily_root, source = write_prepared_daily_panel(tmp_path)
    assert source.get_column("date").n_unique() >= OBSERVATIONS
    assert source.get_column("symbol").n_unique() == SYMBOLS
    mvp_sentinel = tmp_path / "processed" / "mvp" / "keep.txt"
    mvp_sentinel.parent.mkdir(parents=True)
    mvp_sentinel.write_text("untouched", encoding="utf-8")

    run_factor_pipeline(config_path, "all")

    processed = tmp_path / "processed" / "factor_research"
    artifacts = tmp_path / "artifacts" / "factor_research"
    assert daily_root.exists()
    assert mvp_sentinel.read_text(encoding="utf-8") == "untouched"
    expected_processed = {
        "data_readiness.json",
        "factor_features.parquet",
        "factor_panel.parquet",
        "forward_returns.parquet",
        "rank_ic.parquet",
        "quantile_returns.parquet",
        "subperiod_metrics.parquet",
        "factor_turnover.parquet",
        "factor_summary.parquet",
        "factor_classifications.parquet",
        "factor_correlations.parquet",
        "redundancy_flags.parquet",
        "report_manifest.json",
        "lineage.json",
    }
    assert all((processed / name).is_file() for name in expected_processed)
    assert {
        path.parent.name for path in (processed / "monthly_raw").glob("*/part-000.parquet")
    } == {f"family={definition.family}" for definition in FACTOR_DEFINITIONS}

    features = pl.read_parquet(processed / "factor_features.parquet")
    panel = pl.read_parquet(processed / "factor_panel.parquet")
    labels = pl.read_parquet(processed / "forward_returns.parquet")
    assert not any(column.startswith("forward_") for column in features.columns)
    assert not any(
        column.startswith("forward_")
        for path in (processed / "monthly_raw").glob("*/part-000.parquet")
        for column in pl.read_parquet(path).columns
    )
    assert set(panel.get_column("factor_name")) == {
        definition.name for definition in FACTOR_DEFINITIONS
    }
    assert {f"forward_return_{horizon}" for horizon in (5, 20, 60)} <= set(panel.columns)
    assert {f"forward_return_{horizon}" for horizon in (5, 20, 60)} <= set(labels.columns)
    assert not panel.select("date", "symbol", "factor_name").is_duplicated().any()
    assert panel.equals(panel.sort("date", "symbol", "factor_name"))

    summary = pl.read_csv(artifacts / "factor_summary.csv")
    classifications = pl.read_csv(artifacts / "factor_classifications.csv")
    correlations = pl.read_csv(artifacts / "factor_correlations.csv")
    assert set(summary.get_column("factor_name")) == {
        definition.name for definition in FACTOR_DEFINITIONS
    }
    assert classifications.height == len(FACTOR_DEFINITIONS)
    assert correlations.height == len(FACTOR_DEFINITIONS) ** 2
    assert len(list((artifacts / "cards").glob("*.md"))) == len(FACTOR_DEFINITIONS)
    assert len(list((artifacts / "figures").glob("*/*.png"))) == 4 * len(FACTOR_DEFINITIONS)
    assert (artifacts / "report.md").is_file()
    assert (artifacts / "manifest.json").is_file()
    lineage = json.loads((processed / "lineage.json").read_text(encoding="utf-8"))
    assert lineage["factor_config"]["sha256"]
    assert lineage["daily_panel"]["manifest"]["sha256"]
    assert len(lineage["daily_panel"]["partitions"]) == 2
    assert not [path for path in tmp_path.rglob("*") if path.name.endswith((".tmp", ".backup"))]


def test_second_all_run_has_identical_machine_readable_hashes(tmp_path: Path) -> None:
    config_path, _, _ = write_prepared_daily_panel(tmp_path)
    run_factor_pipeline(config_path, "all")
    processed = tmp_path / "processed/factor_research"
    artifacts = tmp_path / "artifacts/factor_research"
    machine_paths = tuple(
        sorted(
            (
                path
                for path in processed.rglob("*")
                if path.is_file() and not path.is_relative_to(processed / "daily_panel")
            ),
            key=str,
        )
    ) + tuple(
        sorted(
            (
                path
                for path in artifacts.rglob("*")
                if path.is_file() and path.suffix in {".csv", ".json", ".md"}
            ),
            key=str,
        )
    )
    machine_files = tuple(path.relative_to(tmp_path).as_posix() for path in machine_paths)
    assert len(machine_files) > 30
    assert "processed/factor_research/factor_panel.parquet" in machine_files
    assert "artifacts/factor_research/report.md" in machine_files
    first = file_hashes(tmp_path, machine_files)

    run_factor_pipeline(config_path, "all")

    assert file_hashes(tmp_path, machine_files) == first


@pytest.mark.parametrize("forbidden_date", ["2017-01-03", "2022-01-04"])
def test_manifest_date_leak_is_rejected_before_parquet_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forbidden_date: str,
) -> None:
    config_path, daily_root, _ = write_prepared_daily_panel(tmp_path)
    manifest_path = daily_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["max_date"] = forbidden_date
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def forbidden_scan(*args: object, **kwargs: object) -> None:
        raise AssertionError("parquet content was scanned before the manifest date gate")

    monkeypatch.setattr("ashare_multifactor.data.manifest.pl.scan_parquet", forbidden_scan)
    with pytest.raises(ValueError, match="manifest max_date exceeds allowed period"):
        run_factor_pipeline(config_path, "audit")


def test_replaced_factor_stage_input_is_rejected_before_read(tmp_path: Path) -> None:
    config_path, _, _ = write_prepared_daily_panel(tmp_path)
    run_factor_pipeline(config_path, "all")
    factor_panel = tmp_path / "processed/factor_research/factor_panel.parquet"
    factor_panel.write_bytes(factor_panel.read_bytes() + b"tampered")

    with pytest.raises(ValueError, match="upstream file digest mismatch"):
        run_factor_pipeline(config_path, "evaluate")


def test_changed_factor_config_is_rejected_before_daily_frame_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, _, _ = write_prepared_daily_panel(tmp_path)
    run_factor_pipeline(config_path, "audit")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["factor_research"]["winsor_lower"] = 0.02
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    def forbidden_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("daily frame was read before the frozen config gate")

    monkeypatch.setattr(pipeline_module, "_read_daily", forbidden_read)
    with pytest.raises(ValueError, match="config does not match frozen lineage"):
        run_factor_pipeline(config_path, "factors")


def test_audit_uses_and_freezes_config_sibling_field_evidence(tmp_path: Path) -> None:
    config_path, _, _ = write_prepared_daily_panel(tmp_path)
    evidence_path = config_path.with_name("data_field_evidence.yaml")
    evidence = yaml.safe_load(evidence_path.read_text(encoding="utf-8"))
    evidence["field_groups"]["valuation"]["point_in_time_status"] = "verified"
    evidence_path.write_text(
        yaml.safe_dump(evidence, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    run_factor_pipeline(config_path, "audit")

    root = tmp_path / "processed/factor_research"
    readiness = json.loads((root / "data_readiness.json").read_text(encoding="utf-8"))
    lineage = json.loads((root / "lineage.json").read_text(encoding="utf-8"))
    assert readiness["factor_status"]["ep_ttm"] == "ready"
    assert lineage["field_evidence"]["statuses"]["valuation"] == "verified"
    assert lineage["field_evidence"]["sha256"]

    evidence["field_groups"]["valuation"]["evidence_note"] = "changed after audit"
    evidence_path.write_text(
        yaml.safe_dump(evidence, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="field evidence does not match frozen lineage"):
        run_factor_pipeline(config_path, "factors")


def test_rerunning_factors_physically_invalidates_evaluation_and_report(
    tmp_path: Path,
) -> None:
    config_path, _, _ = write_prepared_daily_panel(tmp_path)
    run_factor_pipeline(config_path, "all")
    processed = tmp_path / "processed/factor_research"
    artifacts = tmp_path / "artifacts/factor_research"
    assert artifacts.is_dir()
    assert (processed / "factor_summary.parquet").is_file()

    run_factor_pipeline(config_path, "factors")

    assert not artifacts.exists()
    assert not (processed / "factor_summary.parquet").exists()
    assert (processed / "factor_panel.parquet").is_file()


def test_audit_and_factors_use_projected_lazy_reads_and_narrow_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, _, _ = write_prepared_daily_panel(tmp_path)
    read_columns: list[tuple[str, ...]] = []
    label_columns: list[tuple[str, ...]] = []
    original_read = pipeline_module._read_daily
    original_labels = pipeline_module.add_forward_returns

    def tracked_read(source: object, columns: tuple[str, ...]) -> pl.DataFrame:
        read_columns.append(columns)
        return original_read(source, columns)

    def tracked_labels(frame: pl.DataFrame, horizons: tuple[int, ...]) -> pl.DataFrame:
        label_columns.append(tuple(frame.columns))
        return original_labels(frame, horizons)

    monkeypatch.setattr(pipeline_module, "_read_daily", tracked_read)
    monkeypatch.setattr(pipeline_module, "add_forward_returns", tracked_labels)

    run_factor_pipeline(config_path, "audit")
    run_factor_pipeline(config_path, "factors")

    assert read_columns == [
        pipeline_module._AUDIT_INPUT_COLUMNS,
        pipeline_module._FACTOR_INPUT_COLUMNS,
    ]
    assert label_columns == [("date", "symbol", "close_adj", "volume", "amount")]


def test_build_stage_invalidates_all_downstream_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, _, _ = write_prepared_daily_panel(tmp_path)
    run_factor_pipeline(config_path, "all")
    processed = tmp_path / "processed/factor_research"

    monkeypatch.setattr(
        "ashare_multifactor.research.factor_pipeline.build_parquet_dataset",
        lambda *args, **kwargs: object(),
    )
    run_factor_pipeline(config_path, "build")

    assert (processed / "daily_panel/manifest.json").is_file()
    assert not (processed / "lineage.json").exists()
    assert not (processed / "factor_panel.parquet").exists()


def test_build_stage_uses_paired_raw_csvs_without_overwriting_mvp_outputs(
    tmp_path: Path,
) -> None:
    config_path, daily_root, _ = write_prepared_daily_panel(tmp_path)
    shutil.rmtree(daily_root)
    write_raw_pair(config_path, date(2004, 1, 2))
    write_raw_pair(config_path, date(2004, 1, 5))
    stable_daily = tmp_path / "processed/daily_panel/keep.txt"
    stable_mvp = tmp_path / "processed/mvp/keep.txt"
    stable_daily.parent.mkdir(parents=True)
    stable_mvp.parent.mkdir(parents=True)
    stable_daily.write_text("daily", encoding="utf-8")
    stable_mvp.write_text("mvp", encoding="utf-8")

    run_factor_pipeline(config_path, "build")

    manifest = json.loads((daily_root / "manifest.json").read_text(encoding="utf-8"))
    assert (manifest["min_date"], manifest["max_date"]) == ("2004-01-02", "2004-01-05")
    assert stable_daily.read_text(encoding="utf-8") == "daily"
    assert stable_mvp.read_text(encoding="utf-8") == "mvp"
    assert not (tmp_path / "artifacts/factor_research").exists()


def test_cli_registers_and_dispatches_factor_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_config(tmp_path)
    calls: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        factors_cli, "run_factor_pipeline", lambda path, stage: calls.append((path, stage))
    )

    factors_cli.main(["--config", str(config_path), "--stage", "report"])

    assert calls == [(config_path, "report")]
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"]["ashare-factors"] == (
        "ashare_multifactor.cli.factors:main"
    )
