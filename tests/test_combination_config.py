from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml

from ashare_multifactor.config import load_config


def _config(tmp_path: Path, section: str, **overrides: object) -> Path:
    raw = yaml.safe_load(Path("configs/research_protocol.yaml").read_text(encoding="utf-8"))
    raw[section].update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_official_combination_and_portfolio_settings_are_frozen() -> None:
    config = load_config(Path("configs/research_protocol.yaml"))
    assert config.factor_combination.methods == (
        "candidate_equal",
        "family_equal",
        "representative_equal",
        "rolling_ic_family",
    )
    assert config.factor_combination.primary_method == "family_equal"
    assert config.factor_combination.ic_window_months == 36
    assert config.factor_combination.ic_minimum_months == 24
    assert config.factor_combination.ic_shrinkage == 0.5
    assert config.portfolio_construction.portfolio_size == 100
    assert config.portfolio_construction.size_groups == 5
    assert config.portfolio_construction.per_group == 20
    assert config.portfolio_construction.buffer_rank == 30
    assert config.portfolio_construction.minimum_coverage == 0.8
    assert config.portfolio_construction.turnover_warning == 0.8
    with pytest.raises(FrozenInstanceError):
        config.factor_combination.primary_method = "candidate_equal"


@pytest.mark.parametrize(
    ("section", "overrides", "message"),
    [
        ("factor_combination", {"analysis_end": "2017-01-01"}, "research period"),
        ("factor_combination", {"primary_method": "unknown"}, "primary_method"),
        ("factor_combination", {"ic_window_months": 0}, "ic_window_months"),
        ("factor_combination", {"ic_minimum_months": 37}, "minimum"),
        ("factor_combination", {"ic_shrinkage": 1.1}, "ic_shrinkage"),
        ("portfolio_construction", {"size_groups": 0}, "size_groups"),
        ("portfolio_construction", {"portfolio_size": 99}, "portfolio_size"),
        ("portfolio_construction", {"minimum_coverage": 1.1}, "minimum_coverage"),
        ("portfolio_construction", {"turnover_warning": -0.1}, "turnover_warning"),
    ],
)
def test_invalid_stage_five_settings_fail_early(
    tmp_path: Path, section: str, overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        load_config(_config(tmp_path, section, **overrides))
