from datetime import date
from pathlib import Path

import polars as pl
import pytest

from ashare_multifactor.validation.pipeline import (
    CORE_VALIDATION_FILES,
    assert_validation_runtime_gates,
    assert_validation_upstream_releases,
    assert_validation_outputs_sealed,
    certify_full_validation_runs,
    compare_validation_outputs,
    finalize_validation_run,
    resolve_validation_data_root,
    resolve_validation_predecessor,
    run_validation_release,
    validate_run_id,
)
from ashare_multifactor.audit.publication import publish_release
from ashare_multifactor.validation.report import render_validation_report


def test_publish_gate_rejects_any_parquet_date_after_2021(tmp_path: Path) -> None:
    datasets = tmp_path / "datasets"
    datasets.mkdir()
    pl.DataFrame(
        {"date": [date(2021, 12, 31), date(2022, 1, 4)], "value": [1.0, 2.0]}
    ).write_parquet(datasets / "leak.parquet")

    with pytest.raises(ValueError, match="sealed final test"):
        assert_validation_outputs_sealed(tmp_path)


def test_independent_outputs_compare_by_relative_path_and_hash(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root in (first, second):
        root.mkdir()
        pl.DataFrame({"date": [date(2021, 12, 31)], "value": [1.0]}).write_parquet(
            root / "metrics.parquet"
        )

    compare_validation_outputs(first, second, ("metrics.parquet",))

    pl.DataFrame({"date": [date(2021, 12, 31)], "value": [2.0]}).write_parquet(
        second / "metrics.parquet"
    )
    with pytest.raises(ValueError, match="validation runs differ"):
        compare_validation_outputs(first, second, ("metrics.parquet",))


def test_validation_report_does_not_overstate_out_of_sample_evidence() -> None:
    metrics = pl.DataFrame(
        {
            "candidate": ["family_equal_size_stratified_buffered"],
            "net_annual_return": [-0.01],
            "maximum_drawdown": [-0.2],
            "turnover": [0.7],
            "cost_erosion": [0.02],
            "rank_ic": [0.08],
            "icir": [1.2],
            "monotonicity": [0.6],
        }
    )
    decision = {
        "selected_candidate": "family_equal_size_stratified_buffered",
        "default_candidate": "family_equal_size_stratified_buffered",
    }

    report = render_validation_report(metrics, decision)

    assert "-1.00%" in report
    assert "0.0800" in report
    assert "验证期结果不是最终样本外证据" in report
    assert "2022–2025仍保持封存" in report


def test_validation_pipeline_locates_data_outside_worktree(tmp_path: Path) -> None:
    manifest = tmp_path / "processed/validation_evaluation/daily_panel/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}\n", encoding="utf-8")
    worktree = tmp_path / ".worktrees/stage7"
    worktree.mkdir(parents=True)

    assert resolve_validation_data_root(worktree) == tmp_path


def test_validation_runtime_gates_require_ready_shadow_and_stale_audits(
    tmp_path: Path,
) -> None:
    candidate = "family_equal_size_stratified_buffered"
    artifacts = tmp_path / "backtests" / candidate / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "shadow_nav_audit.json").write_text(
        '{"status": "ready"}\n', encoding="utf-8"
    )
    (artifacts / "stale_audit.json").write_text(
        '{"status": "ready", "unexplained_symbols": 0}\n', encoding="utf-8"
    )

    assert_validation_runtime_gates(tmp_path, (candidate,))

    (artifacts / "stale_audit.json").write_text(
        '{"status": "blocked", "unexplained_symbols": 1}\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="runtime gate"):
        assert_validation_runtime_gates(tmp_path, (candidate,))


def test_validation_release_path_enforces_approved_stage_six_pointer(
    tmp_path: Path,
) -> None:
    formal = tmp_path / "processed/formal_backtest"
    formal.mkdir(parents=True)
    (formal / "CURRENT.json").write_text(
        '{"run_id": "unapproved"}\n', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="unapproved stage six release"):
        assert_validation_upstream_releases(tmp_path)


def test_validation_publication_is_blocked_until_full_pipeline_is_reproducible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ashare_multifactor.validation.pipeline.code_identity",
        lambda root: {"dirty": False},
    )

    with pytest.raises(ValueError, match="full validation pipeline reproducibility"):
        run_validation_release(tmp_path, publish=True)


def test_validation_successor_binds_verified_current_release(tmp_path: Path) -> None:
    processed = tmp_path / "processed/validation_evaluation"
    datasets = tmp_path / "staged/datasets"
    artifacts = tmp_path / "staged/artifacts"
    datasets.mkdir(parents=True)
    artifacts.mkdir(parents=True)
    (datasets / "data.txt").write_text("immutable\n", encoding="utf-8")
    published = publish_release(
        processed,
        run_id="stage7_original",
        staged_datasets=datasets,
        staged_artifacts=artifacts,
        lineage={"stage": "validation_evaluation"},
    )

    predecessor = resolve_validation_predecessor(tmp_path)

    assert predecessor == {
        "run_id": "stage7_original",
        "manifest_sha256": published.manifest_sha256,
        "lineage_sha256": predecessor["lineage_sha256"],
    }

    (published.root / "datasets/data.txt").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mismatch"):
        resolve_validation_predecessor(tmp_path)


def test_finalize_validation_run_records_every_full_pipeline_core_file(
    tmp_path: Path,
) -> None:
    run = tmp_path / "run_a"
    for relative in CORE_VALIDATION_FILES:
        path = run / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".parquet":
            pl.DataFrame(
                {"date": [date(2021, 12, 31)], "value": [1.0]}
            ).write_parquet(path)
        else:
            path.write_text("deterministic\n", encoding="utf-8")

    manifest = finalize_validation_run(
        run,
        run_id="run_a",
        identity={"commit": "abc", "dirty": True},
        inputs={"daily_panel": {"sha256": "input-sha"}},
        elapsed_seconds=1.25,
        maximum_rss_bytes=2048,
    )

    assert manifest["run_id"] == "run_a"
    assert manifest["core_file_count"] == len(CORE_VALIDATION_FILES)
    assert manifest["resource_usage"]["elapsed_seconds"] == 1.25
    assert (run / "run_manifest.json").is_file()

    (run / "artifacts/unhashed.txt").write_text("extra\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected files"):
        finalize_validation_run(
            run,
            run_id="run_a",
            identity={"commit": "abc", "dirty": True},
            inputs={"daily_panel": {"sha256": "input-sha"}},
            elapsed_seconds=1.25,
            maximum_rss_bytes=2048,
        )


def test_validation_run_id_cannot_escape_the_run_root() -> None:
    assert validate_run_id("stage7_full_1") == "stage7_full_1"
    for invalid in ("../escape", "/tmp/escape", "a/b", "..", ""):
        with pytest.raises(ValueError, match="safe slug"):
            validate_run_id(invalid)


def test_full_pipeline_certification_requires_all_core_hashes_and_marks_dirty_run_ineligible(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "artifacts/validation_evaluation/full_runs"
    for run_id in ("run_a", "run_b"):
        run = runs / run_id
        for relative in CORE_VALIDATION_FILES:
            path = run / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == ".parquet":
                pl.DataFrame(
                    {"date": [date(2021, 12, 31)], "value": [1.0]}
                ).write_parquet(path)
            else:
                path.write_text("deterministic\n", encoding="utf-8")
        finalize_validation_run(
            run,
            run_id=run_id,
            identity={"commit": "abc", "dirty": True},
            inputs={"daily_panel": {"sha256": "input-sha"}},
            elapsed_seconds=1.0,
            maximum_rss_bytes=1024,
        )

    result = certify_full_validation_runs(tmp_path, "run_a", "run_b")

    assert result["outputs_identical"] is True
    assert result["full_pipeline_reproducible"] is False
    assert result["release_eligible"] is False
    assert result["reason"] == "git_identity_is_dirty"
    assert result["core_file_count"] == len(CORE_VALIDATION_FILES)

    decision = runs / "run_b/artifacts/selection_decision.json"
    decision.write_text("changed non-backtest decision\n", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest hash mismatch"):
        certify_full_validation_runs(tmp_path, "run_a", "run_b")
