from datetime import date
from pathlib import Path

import pytest
import polars as pl

from ashare_multifactor.robustness.protocol import (
    assert_robustness_read_allowed,
    assert_authoritative_period_contracts,
    load_robustness_protocol,
    read_bounded_parquet,
)


def test_official_protocol_is_frozen_and_deterministic() -> None:
    first = load_robustness_protocol(Path("configs/robustness_protocol.yaml"))
    second = load_robustness_protocol(Path("configs/robustness_protocol.yaml"))

    assert first.main_candidate == "rolling_ic_family_size_stratified_buffered"
    assert first.analysis_start == date(2005, 1, 1)
    assert first.analysis_end == date(2021, 12, 31)
    assert first.experiments == second.experiments
    assert first.protocol_sha256 == second.protocol_sha256
    assert len({item.experiment_id for item in first.experiments}) == len(
        first.experiments
    )
    assert {item.category for item in first.experiments} == {
        "execution",
        "portfolio",
        "factor_ablation",
        "rolling_window",
        "regime",
    }
    assert all(item.metrics for item in first.experiments)


def test_protocol_rejects_duplicate_ids_and_main_candidate_changes(tmp_path: Path) -> None:
    config = Path("configs/robustness_protocol.yaml").read_text(encoding="utf-8")
    duplicate = config.replace(
        "execution_zero_cost:", "execution_full_cost:", 1
    )
    path = tmp_path / "duplicate.yaml"
    path.write_text(duplicate, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate experiment ID"):
        load_robustness_protocol(path)

    changed = config.replace(
        "rolling_ic_family_size_stratified_buffered",
        "family_equal_size_stratified_buffered",
        1,
    )
    path.write_text(changed, encoding="utf-8")
    with pytest.raises(ValueError, match="frozen validation main candidate"):
        load_robustness_protocol(path)


def test_read_gate_rejects_final_test_before_scanning() -> None:
    scanned = False

    def scanner() -> None:
        nonlocal scanned
        scanned = True

    with pytest.raises(ValueError, match="final test period is sealed"):
        assert_robustness_read_allowed(
            date(2005, 1, 1), date(2022, 1, 1), before_read=scanner
        )
    assert scanned is False


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2004, 12, 31), date(2021, 12, 31)),
        (date(2021, 12, 31), date(2021, 1, 1)),
    ],
)
def test_read_gate_rejects_dates_outside_robustness_sample(
    start: date, end: date
) -> None:
    with pytest.raises(ValueError):
        assert_robustness_read_allowed(start, end)


def test_bounded_parquet_reader_never_materializes_sealed_rows(tmp_path: Path) -> None:
    path = tmp_path / "mixed.parquet"
    pl.DataFrame(
        {
            "date": [date(2021, 12, 31), date(2022, 1, 4)],
            "value": [1.0, 999.0],
        }
    ).write_parquet(path)

    frame = read_bounded_parquet(path, start=date(2005, 1, 1), end=date(2021, 12, 31))

    assert frame.to_dicts() == [{"date": date(2021, 12, 31), "value": 1.0}]


def test_authoritative_period_contract_rejects_test_period_before_parquet_scan() -> None:
    validation = {
        "period": ["2017-01-01", "2022-01-03"],
        "sealed_final_test_start": "2022-01-01",
    }
    stage5 = {"config": {"factor_combination": {"analysis_end": "2016-12-31"}}}
    factors = {"factor_config": {"payload": {"analysis_end": "2016-12-31"}}}

    with pytest.raises(ValueError, match="validation input contract"):
        assert_authoritative_period_contracts(validation, stage5, factors)
