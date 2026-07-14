from datetime import date
import json
from pathlib import Path

import polars as pl
import pytest
import sys

from ashare_multifactor.research.combination_evaluation import evaluate_combinations
from ashare_multifactor.config import load_config
from ashare_multifactor.research.combination_pipeline import (
    UPSTREAM_FILES,
    _realization_dates,
    _validate_upstream,
)
from ashare_multifactor.research.combination_inputs import validate_stage_five_inputs
from ashare_multifactor.research.combination_lineage import build_legacy_lineage
from ashare_multifactor.cli import combinations as combinations_cli


def test_combination_evaluation_computes_rank_ic_with_current_polars() -> None:
    scores = pl.DataFrame({
        "date": [date(2005, 1, 31)] * 3,
        "symbol": ["A", "B", "C"],
        "method": ["family_equal"] * 3,
        "score": [-1.0, 0.0, 1.0],
    })
    returns = pl.DataFrame({
        "date": [date(2005, 1, 31)] * 3,
        "symbol": ["A", "B", "C"],
        "forward_return_5": [-0.1, 0.0, 0.1],
        "forward_return_20": [-0.2, 0.0, 0.2],
        "forward_return_60": [-0.3, 0.0, 0.3],
    })

    result = evaluate_combinations(scores, returns)

    assert result.ic.get_column("rank_ic").to_list() == pytest.approx([1.0, 1.0, 1.0])


def test_combination_lineage_is_json_serializable(tmp_path: Path) -> None:
    for name in UPSTREAM_FILES:
        (tmp_path / name).write_bytes(b"input")

    lineage = build_legacy_lineage(
        tmp_path,
        load_config(Path("configs/research_protocol.yaml")),
        UPSTREAM_FILES,
    )

    json.dumps(lineage)


def test_upstream_validation_rejects_file_not_matching_frozen_lineage(tmp_path: Path) -> None:
    panel = pl.DataFrame({"date": [date(2005, 1, 31)]})
    for name in UPSTREAM_FILES:
        if name.endswith(".parquet"):
            panel.write_parquet(tmp_path / name)
    records = []
    for name in UPSTREAM_FILES:
        if name != "lineage.json":
            path = tmp_path / name
            records.append({"path": name, "sha256": "0" * 64, "size_bytes": path.stat().st_size})
    (tmp_path / "lineage.json").write_text(json.dumps({"stages": {
        "factors": {"outputs": records}, "evaluate": {"outputs": records}
    }}))

    with pytest.raises(ValueError, match="digest mismatch"):
        _validate_upstream(tmp_path)


def test_realization_dates_are_scalar_dates() -> None:
    returns = pl.DataFrame({
        "date": [date(2005, 1, 31), date(2005, 1, 31)],
        "forward_calendar_days_20": [30, 35],
    })

    result = _realization_dates(returns)

    assert result.schema["realization_date"] == pl.Date
    assert result.get_column("realization_date").item() == date(2005, 3, 7)


def test_stage_five_input_gate_rejects_replaced_daily_partition(tmp_path: Path) -> None:
    daily = tmp_path / "daily_panel"
    partition = daily / "year=2005" / "part-000.parquet"
    partition.parent.mkdir(parents=True)
    partition.write_bytes(b"original partition")
    manifest = daily / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    issues = daily / "quality_issues.json"
    issues.write_text("[]", encoding="utf-8")

    def record(path: Path) -> dict[str, object]:
        import hashlib

        return {
            "path": path.relative_to(tmp_path).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size_bytes": path.stat().st_size,
        }

    (tmp_path / "lineage.json").write_text(
        json.dumps(
            {
                "daily_panel": {
                    "manifest": record(manifest),
                    "quality_issues": record(issues),
                    "partitions": [record(partition)],
                }
            }
        ),
        encoding="utf-8",
    )
    partition.write_bytes(b"replaced partition")

    with pytest.raises(ValueError, match="mismatch"):
        validate_stage_five_inputs(tmp_path, validate_core=False)


def test_combination_correlations_average_monthly_cross_sections_equally() -> None:
    first = date(2005, 1, 31)
    second = date(2005, 2, 28)
    scores = pl.DataFrame(
        {
            "date": [first] * 3 + [second] * 6,
            "symbol": ["A", "B", "C"] + list("ABCDEF"),
            "method": ["left"] * 9,
            "score": [1.0, 2.0, 3.0] + [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
        }
    )
    right = pl.DataFrame(
        {
            "date": [first] * 3 + [second] * 6,
            "symbol": ["A", "B", "C"] + list("ABCDEF"),
            "method": ["right"] * 9,
            "score": [1.0, 2.0, 3.0] + [6.0, 5.0, 4.0, 3.0, 2.0, 1.0],
        }
    )
    combined = pl.concat((scores, right))
    returns = combined.select("date", "symbol").unique().sort("date", "symbol").with_row_index(
        "return_rank"
    ).with_columns(
        pl.col("return_rank").cast(pl.Float64).alias("forward_return_5"),
        pl.col("return_rank").cast(pl.Float64).alias("forward_return_20"),
        pl.col("return_rank").cast(pl.Float64).alias("forward_return_60"),
    ).drop("return_rank")

    result = evaluate_combinations(combined, returns)
    row = result.correlations.filter(
        (pl.col("left_name") == "left") & (pl.col("right_name") == "right")
    ).row(0, named=True)

    assert row["mean_correlation"] == pytest.approx(0.0)
    assert row["common_months"] == 2
    assert result.correlation_monthly.select("date", "left_name", "right_name").is_duplicated().any() is False


def test_combination_cli_runs_only_complete_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(
        combinations_cli,
        "run_combination_pipeline",
        lambda config, upstream_root=None: calls.append((config, upstream_root)),
    )
    monkeypatch.setattr(sys, "argv", ["ashare-combine", "--config", "config.yaml"])

    combinations_cli.main()

    assert calls == [(Path("config.yaml"), None)]


def test_quantile_evaluation_is_bitwise_independent_of_input_order() -> None:
    signal_date = date(2005, 1, 31)
    scores = pl.DataFrame(
        {
            "date": [signal_date] * 1000,
            "symbol": [f"{index:06d}" for index in range(1000)],
            "method": ["family_equal"] * 1000,
            "score": [index / 7 for index in range(1000)],
        }
    )
    returns = scores.select("date", "symbol").with_columns(
        (pl.col("symbol").str.to_integer() / 13).alias("forward_return_5"),
        (pl.col("symbol").str.to_integer() / 17).alias("forward_return_20"),
        (pl.col("symbol").str.to_integer() / 19).alias("forward_return_60"),
    )

    forward = evaluate_combinations(scores, returns)
    reverse = evaluate_combinations(scores.reverse(), returns.reverse())

    assert forward.quantiles.equals(reverse.quantiles)
    assert forward.summary.equals(reverse.summary)
