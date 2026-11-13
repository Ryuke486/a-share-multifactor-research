from __future__ import annotations

from pathlib import Path

from ashare_multifactor import protocol_dates
from ashare_multifactor.config import load_config


def test_frozen_boundaries_match_the_tracked_research_protocol() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "configs/research_protocol.yaml")

    assert (config.research.start, config.research.end) == (
        protocol_dates.RESEARCH_START,
        protocol_dates.RESEARCH_END,
    )
    assert (config.validation.start, config.validation.end) == (
        protocol_dates.VALIDATION_START,
        protocol_dates.VALIDATION_END,
    )
    assert (config.test.start, config.test.end) == (
        protocol_dates.FINAL_TEST_START,
        protocol_dates.FINAL_TEST_END,
    )
    assert config.factor_research is not None
    assert config.factor_research.data_start == protocol_dates.FACTOR_WARMUP_START


def test_sample_splits_are_ordered_and_contiguous_without_overlap() -> None:
    assert protocol_dates.FACTOR_WARMUP_START < protocol_dates.RESEARCH_START
    assert protocol_dates.RESEARCH_END < protocol_dates.VALIDATION_START
    assert protocol_dates.VALIDATION_END < protocol_dates.FINAL_TEST_START
    assert protocol_dates.FINAL_TEST_START < protocol_dates.FINAL_TEST_END
    assert (protocol_dates.VALIDATION_START - protocol_dates.RESEARCH_END).days == 1
    assert (protocol_dates.FINAL_TEST_START - protocol_dates.VALIDATION_END).days == 1
