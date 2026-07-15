from datetime import date
import json
from pathlib import Path

import pytest

from ashare_multifactor.config import load_config
from ashare_multifactor.validation.protocol import (
    assert_stage_six_release_allowed,
    assert_validation_read_allowed,
)


def test_official_validation_protocol_is_frozen_before_results() -> None:
    protocol = load_config(Path("configs/research_protocol.yaml")).validation_evaluation

    assert protocol.analysis_start == date(2017, 1, 1)
    assert protocol.analysis_end == date(2021, 12, 31)
    assert protocol.primary_metric == "net_annual_return"
    assert protocol.candidates == (
        "family_equal_size_stratified_buffered",
        "rolling_ic_family_size_stratified_buffered",
        "family_equal_top100_equal",
    )
    assert protocol.default_candidate == "family_equal_size_stratified_buffered"
    assert protocol.minimum_return_improvement == 0.01
    assert protocol.maximum_drawdown_deterioration == 0.05
    assert protocol.maximum_turnover_increase == 0.10


def test_read_gate_rejects_final_test_before_scanner_is_called() -> None:
    scanner_called = False

    def scanner() -> None:
        nonlocal scanner_called
        scanner_called = True

    with pytest.raises(ValueError, match="final test period is sealed"):
        assert_validation_read_allowed(
            date(2021, 12, 31), date(2022, 1, 1), before_read=scanner
        )

    assert scanner_called is False


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2021, 1, 1), date(2020, 1, 1)),
        (date(2002, 12, 31), date(2017, 1, 1)),
    ],
)
def test_read_gate_rejects_invalid_or_pre_warmup_ranges(start: date, end: date) -> None:
    with pytest.raises(ValueError):
        assert_validation_read_allowed(start, end)


def test_read_gate_allows_warmup_through_validation() -> None:
    assert_validation_read_allowed(date(2003, 1, 1), date(2021, 12, 31))


def test_stage_six_gate_accepts_only_approved_current_release(tmp_path: Path) -> None:
    processed = tmp_path / "formal_backtest"
    processed.mkdir()
    current = processed / "CURRENT.json"
    current.write_text(
        json.dumps({"run_id": "b995878_stage6_authoritative"}), encoding="utf-8"
    )

    assert_stage_six_release_allowed(processed)

    current.write_text(json.dumps({"run_id": "de57558_stage6_final"}), encoding="utf-8")
    with pytest.raises(ValueError, match="unapproved stage six release"):
        assert_stage_six_release_allowed(processed)
