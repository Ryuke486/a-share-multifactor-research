from dataclasses import FrozenInstanceError
from datetime import date
from pathlib import Path

import pytest
import yaml

from ashare_multifactor.config import load_config


def _write_factor_config(tmp_path: Path, **overrides: object) -> Path:
    raw = yaml.safe_load(Path("configs/research_protocol.yaml").read_text(encoding="utf-8"))
    raw["paths"] = {
        "raw_unadjusted": str(tmp_path / "raw"),
        "raw_backward_adjusted": str(tmp_path / "adj"),
        "processed": str(tmp_path / "processed"),
        "artifacts": str(tmp_path / "artifacts"),
    }
    raw["factor_research"].update(overrides)
    path = tmp_path / "research_protocol.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


def test_factor_research_is_confined_to_research_period(tmp_path: Path) -> None:
    config = _write_factor_config(tmp_path, analysis_end="2017-01-03")

    with pytest.raises(ValueError, match="factor research analysis must stay inside research"):
        load_config(config)


def test_factor_research_warmup_cannot_reach_validation_or_test(tmp_path: Path) -> None:
    config = _write_factor_config(tmp_path, data_start="2017-01-01")

    with pytest.raises(ValueError, match="factor research data"):
        load_config(config)


def test_official_factor_research_settings_are_frozen() -> None:
    settings = load_config(Path("configs/research_protocol.yaml")).factor_research

    assert settings is not None
    assert settings.data_start == date(2003, 1, 1)
    assert settings.analysis_start == date(2005, 1, 1)
    assert settings.analysis_end == date(2016, 12, 31)
    assert settings.universe_size == 1000
    assert settings.minimum_history == 252
    assert settings.liquidity_lookback == 20
    assert settings.require_valid_trade_observation is True
    assert settings.signal_frequency == "month_end"
    assert settings.forward_horizons == (5, 20, 60)
    assert settings.primary_horizon == 20
    assert settings.winsor_lower == 0.01
    assert settings.winsor_upper == 0.99
    assert settings.quantile_count == 5
    assert settings.minimum_coverage == 0.80
    assert settings.minimum_valid_months == 120
    assert settings.fdr_q_threshold == 0.10
    assert settings.redundancy_threshold == 0.70

    with pytest.raises(FrozenInstanceError):
        settings.universe_size = 200


def test_factor_research_data_start_precedes_analysis(tmp_path: Path) -> None:
    config = _write_factor_config(tmp_path, data_start="2005-01-01")

    with pytest.raises(ValueError, match="data_start must precede analysis_start"):
        load_config(config)


@pytest.mark.parametrize(
    "field",
    ["universe_size", "minimum_history", "liquidity_lookback", "minimum_valid_months"],
)
@pytest.mark.parametrize("invalid_value", [0, -1, 1.5, True])
def test_factor_research_counts_must_be_positive_integers(
    tmp_path: Path, field: str, invalid_value: object
) -> None:
    config = _write_factor_config(tmp_path, **{field: invalid_value})

    with pytest.raises(ValueError, match=rf"factor_research\.{field}"):
        load_config(config)


@pytest.mark.parametrize("invalid_value", [0, 1, -1, 2.5, True])
def test_quantile_count_must_allow_at_least_two_groups(
    tmp_path: Path, invalid_value: object
) -> None:
    config = _write_factor_config(tmp_path, quantile_count=invalid_value)

    with pytest.raises(ValueError, match=r"factor_research\.quantile_count"):
        load_config(config)


@pytest.mark.parametrize("invalid_value", [[], [0, 20], [5, -20], [5, 20.5], [True, 20]])
def test_forward_horizons_must_be_positive_integers(
    tmp_path: Path, invalid_value: list[object]
) -> None:
    config = _write_factor_config(tmp_path, forward_horizons=invalid_value)

    with pytest.raises(ValueError, match=r"factor_research\.forward_horizons"):
        load_config(config)


def test_primary_horizon_must_be_preregistered(tmp_path: Path) -> None:
    config = _write_factor_config(tmp_path, primary_horizon=10)

    with pytest.raises(ValueError, match="primary_horizon must belong to forward_horizons"):
        load_config(config)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"winsor_lower": -0.01}, "winsor bounds"),
        ({"winsor_upper": 1.01}, "winsor bounds"),
        ({"winsor_lower": 0.99, "winsor_upper": 0.01}, "winsor bounds"),
        ({"minimum_coverage": -0.01}, "minimum_coverage"),
        ({"minimum_coverage": 1.01}, "minimum_coverage"),
        ({"fdr_q_threshold": -0.01}, "fdr_q_threshold"),
        ({"fdr_q_threshold": 1.01}, "fdr_q_threshold"),
        ({"redundancy_threshold": -0.01}, "redundancy_threshold"),
        ({"redundancy_threshold": 1.01}, "redundancy_threshold"),
    ],
)
def test_factor_research_proportions_stay_in_unit_interval(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    config = _write_factor_config(tmp_path, **overrides)

    with pytest.raises(ValueError, match=message):
        load_config(config)


def test_signal_frequency_is_frozen_to_month_end(tmp_path: Path) -> None:
    config = _write_factor_config(tmp_path, signal_frequency="weekly")

    with pytest.raises(ValueError, match="signal_frequency must be month_end"):
        load_config(config)


def test_factor_research_rejects_disabling_valid_trade_observations(tmp_path: Path) -> None:
    config = _write_factor_config(tmp_path, require_valid_trade_observation=False)

    with pytest.raises(
        ValueError,
        match="require_valid_trade_observation must be true",
    ):
        load_config(config)
