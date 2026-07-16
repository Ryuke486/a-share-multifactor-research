from datetime import date
import json
from pathlib import Path

import polars as pl
import pytest

from ashare_multifactor.audit.publication import publish_release
from ashare_multifactor.audit.records import file_record
from ashare_multifactor.config import load_config
from ashare_multifactor.final_test.gate import FinalTestAuthorization
from ashare_multifactor.final_test.signal_inputs import resolve_final_test_signal_inputs


MAIN_CANDIDATE = "rolling_ic_family_size_stratified_buffered"


def _authorization(*, validation_run: str = "stage7-release") -> FinalTestAuthorization:
    return FinalTestAuthorization(
        attempt_id="attempt-001",
        approval_id="approved-stage9",
        registered_at="2026-07-16T00:00:00+00:00",
        git_commit="a" * 40,
        git_tree="b" * 40,
        sealed_protocol_sha256="c" * 64,
        robustness_release="stage8-release",
        test_period=(date(2022, 1, 1), date(2025, 12, 31)),
    )


def _fixture(
    tmp_path: Path,
    *,
    sealed_validation_run: str = "stage7-release",
    include_2020: bool = True,
    sparse_daily: bool = False,
    target_date: date = date(2021, 12, 31),
) -> tuple[Path, object]:
    data_root = tmp_path / "data-root"
    factor_root = data_root / "processed/factor_research"
    factor_root.mkdir(parents=True)
    _rank_ic(date(2016, 12, 30)).write_parquet(factor_root / "rank_ic.parquet")
    _forward_returns(date(2016, 12, 30)).write_parquet(
        factor_root / "forward_returns.parquet"
    )
    pl.DataFrame(
        {
            "factor_name": ["reversal_5"],
            "family": ["reversal"],
            "classification": ["candidate"],
            "primary_score_variant": ["score_size_neutral"],
        }
    ).write_parquet(factor_root / "factor_classifications.parquet")
    readiness = {
        "factor_status": {"reversal_5": "verified"},
        "industry_neutralization_enabled": False,
        "field_metrics": {},
    }
    (factor_root / "data_readiness.json").write_text(
        json.dumps(readiness) + "\n", encoding="utf-8"
    )
    stage_four_paths = [
        factor_root / "rank_ic.parquet",
        factor_root / "forward_returns.parquet",
        factor_root / "factor_classifications.parquet",
        factor_root / "data_readiness.json",
    ]
    stage_four_records = [
        file_record(path, root=factor_root, role="stage_four").to_dict()
        for path in stage_four_paths
    ]

    daily_root = data_root / "processed/validation_evaluation/daily_panel"
    years = [2021] if not include_2020 else [2020, 2021]
    daily_paths = []
    for year in years:
        path = daily_root / f"year={year}/part-000.parquet"
        path.parent.mkdir(parents=True)
        days = [date(2020, 1, 2)] if year == 2020 else [date(2021, 12, 31)]
        if not sparse_daily:
            days = _weekdays(year)
        pl.DataFrame(
            {"date": days, "symbol": ["000001"] * len(days)}
        ).write_parquet(path)
        daily_paths.append(path)
    validation_daily_records = [
        file_record(path, root=data_root, role="validation_daily").to_dict()
        for path in daily_paths
    ]

    staging = tmp_path / "stage7-staging"
    datasets = staging / "datasets/inputs"
    artifacts = staging / "artifacts/research"
    datasets.mkdir(parents=True)
    artifacts.mkdir(parents=True)
    _forward_returns(date(2021, 11, 30)).write_parquet(
        datasets / "forward_returns.parquet"
    )
    _rank_ic(date(2021, 11, 30)).write_parquet(
        artifacts / "factor_rank_ic.parquet"
    )
    pl.DataFrame(
        {
            "date": [target_date],
            "candidate": [MAIN_CANDIDATE],
            "symbol": ["000001"],
            "target_weight": [1.0],
        }
    ).write_parquet(
        datasets / f"target_weights_{MAIN_CANDIDATE}.parquet"
    )
    validation = publish_release(
        data_root / "processed/validation_evaluation",
        run_id="stage7-release",
        staged_datasets=staging / "datasets",
        staged_artifacts=staging / "artifacts",
        lineage={
            "reproducibility": {
                "inputs": {
                    "stage_four_files": stage_four_records,
                    "validation_daily_panel_files": validation_daily_records,
                }
            }
        },
    )

    robust_staging = tmp_path / "stage8-staging"
    (robust_staging / "datasets").mkdir(parents=True)
    (robust_staging / "artifacts").mkdir()
    (robust_staging / "datasets/placeholder.bin").write_bytes(b"robustness")
    (robust_staging / "artifacts/sealed_test_protocol.json").write_text(
        json.dumps(
            {
                "sealed_protocol_sha256": "c" * 64,
                "upstream_validation": {
                    "run_id": sealed_validation_run,
                    "manifest_sha256": validation.manifest_sha256,
                },
                "main_candidate": MAIN_CANDIDATE,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    publish_release(
        data_root / "processed/robustness",
        run_id="stage8-release",
        staged_datasets=robust_staging / "datasets",
        staged_artifacts=robust_staging / "artifacts",
        lineage={"stage": "robustness"},
    )
    return data_root, validation


def test_signal_inputs_resolve_only_manifest_bound_stage7_history(tmp_path: Path) -> None:
    data_root, _ = _fixture(tmp_path)
    config = load_config(Path("configs/research_protocol.yaml"))

    result = resolve_final_test_signal_inputs(
        config,
        _authorization(),
        data_root=data_root,
    )

    assert result.historical_daily.get_column("date").min() == date(2020, 1, 1)
    assert result.historical_daily.get_column("date").max() == date(2021, 12, 31)
    assert result.historical_daily.get_column("date").n_unique() >= 252
    assert result.previous_targets.get_column("date").unique().item() == date(
        2021, 12, 31
    )
    assert result.historical_rank_ic.get_column("date").max() == date(2021, 11, 30)


def test_signal_inputs_reject_stage7_file_tampering(tmp_path: Path) -> None:
    data_root, validation = _fixture(tmp_path)
    target = validation.datasets / "inputs/forward_returns.parquet"
    target.write_bytes(target.read_bytes() + b"tampered")

    with pytest.raises(ValueError, match="size mismatch|digest mismatch"):
        resolve_final_test_signal_inputs(
            load_config(Path("configs/research_protocol.yaml")),
            _authorization(),
            data_root=data_root,
        )


def test_signal_inputs_reject_validation_release_substitution(tmp_path: Path) -> None:
    data_root, _ = _fixture(tmp_path, sealed_validation_run="other-stage7")

    with pytest.raises(ValueError, match="sealed Stage-8 validation identity"):
        resolve_final_test_signal_inputs(
            load_config(Path("configs/research_protocol.yaml")),
            _authorization(),
            data_root=data_root,
        )


def test_signal_inputs_reject_missing_longest_lookback_year(tmp_path: Path) -> None:
    data_root, _ = _fixture(tmp_path, include_2020=False)

    with pytest.raises(ValueError, match="continuous 2020-2021"):
        resolve_final_test_signal_inputs(
            load_config(Path("configs/research_protocol.yaml")),
            _authorization(),
            data_root=data_root,
        )


def test_signal_inputs_reject_stale_main_candidate_holdings(tmp_path: Path) -> None:
    data_root, _ = _fixture(tmp_path, target_date=date(2021, 11, 30))

    with pytest.raises(ValueError, match="2021-12-31"):
        resolve_final_test_signal_inputs(
            load_config(Path("configs/research_protocol.yaml")),
            _authorization(),
            data_root=data_root,
        )


def test_signal_inputs_reject_sparse_two_year_warmup(tmp_path: Path) -> None:
    data_root, _ = _fixture(tmp_path, sparse_daily=True)

    with pytest.raises(ValueError, match="at least 252 distinct trading dates"):
        resolve_final_test_signal_inputs(
            load_config(Path("configs/research_protocol.yaml")),
            _authorization(),
            data_root=data_root,
        )


def _rank_ic(signal_date: date) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [signal_date],
            "factor_name": ["reversal_5"],
            "family": ["reversal"],
            "score_variant": ["score_size_neutral"],
            "horizon": [20],
            "rank_ic": [0.1],
        }
    )


def _forward_returns(signal_date: date) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [signal_date],
            "symbol": ["000001"],
            "forward_return_20": [0.1],
            "forward_calendar_days_20": [28],
        }
    )


def _weekdays(year: int) -> list[date]:
    days = pl.date_range(
        date(year, 1, 1),
        date(year, 12, 31),
        interval="1d",
        eager=True,
    ).to_list()
    return [day for day in days if day.weekday() < 5]
