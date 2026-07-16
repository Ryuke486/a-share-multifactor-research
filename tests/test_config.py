from dataclasses import FrozenInstanceError
from datetime import date
from pathlib import Path

import pytest

from ashare_multifactor.config import load_config


BASE_CONFIG = """
paths:
  raw_unadjusted: Data/每天一个文件/不复权
  raw_backward_adjusted: Data/每天一个文件/后复权
  processed: processed
  artifacts: artifacts
periods:
  research: [2005-01-01, 2016-12-31]
  validation: [2017-01-01, 2021-12-31]
  test: [2022-01-01, 2025-12-31]
  smoke_data: [2012-01-01, 2015-12-31]
  smoke_analysis: [2014-01-01, 2015-12-31]
research_scope:
  supported_markets: [sh, sz]
mvp:
  universe_size: 200
  momentum_lookback: 60
  forward_horizon: 20
  portfolio_size: 20
  transaction_cost_bps: 10.0
  initial_cash: 1000000.0
"""


def test_smoke_period_must_not_overlap_final_test(tmp_path: Path):
    config = tmp_path / "bad.yaml"
    config.write_text(
        BASE_CONFIG.replace(
            "smoke_data: [2012-01-01, 2015-12-31]",
            "smoke_data: [2020-01-01, 2023-12-31]",
        ).replace(
            "smoke_analysis: [2014-01-01, 2015-12-31]",
            "smoke_analysis: [2022-01-01, 2023-12-31]",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="smoke periods must not overlap test period"):
        load_config(config)


def test_official_config_loads_unquoted_yaml_dates():
    config = load_config(Path("configs/research_protocol.yaml"))

    assert config.smoke_analysis.start == date(2014, 1, 1)
    assert config.smoke_analysis.end == date(2015, 12, 31)
    assert config.formal_backtest is not None
    assert config.formal_backtest.analysis_end == date(2016, 12, 31)
    assert config.formal_backtest.maximum_participation == 0.10
    assert config.formal_backtest.stale_review_days == 30
    assert config.supported_markets == ("sh", "sz")


def test_config_is_immutable():
    config = load_config(Path("configs/research_protocol.yaml"))

    with pytest.raises(FrozenInstanceError):
        config.test = config.research


def test_smoke_data_must_be_inside_research_period(tmp_path: Path):
    config = tmp_path / "bad.yaml"
    config.write_text(
        BASE_CONFIG.replace(
            "smoke_data: [2012-01-01, 2015-12-31]",
            "smoke_data: [2012-01-01, 2017-01-01]",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="research period must contain smoke data period"):
        load_config(config)


def test_portfolio_cannot_exceed_universe(tmp_path: Path):
    config = tmp_path / "bad.yaml"
    config.write_text(
        BASE_CONFIG.replace("portfolio_size: 20", "portfolio_size: 201"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="portfolio size exceeds universe size"):
        load_config(config)


@pytest.mark.parametrize(
    "field",
    ["universe_size", "momentum_lookback", "forward_horizon", "portfolio_size"],
)
@pytest.mark.parametrize("invalid_value", [0, -1])
def test_mvp_integer_settings_must_be_positive(
    tmp_path: Path, field: str, invalid_value: int
) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text(
        BASE_CONFIG.replace(
            f"{field}: {dict(universe_size=200, momentum_lookback=60, forward_horizon=20, portfolio_size=20)[field]}",
            f"{field}: {invalid_value}",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=rf"mvp\.{field} must be a positive integer"):
        load_config(config)


@pytest.mark.parametrize("invalid_value", ["60.5", "'60'", "true"])
def test_mvp_window_setting_rejects_non_integers(tmp_path: Path, invalid_value: str) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text(
        BASE_CONFIG.replace("momentum_lookback: 60", f"momentum_lookback: {invalid_value}"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"mvp\.momentum_lookback must be a positive integer"):
        load_config(config)


@pytest.mark.parametrize("invalid_value", ["0", "-1", ".inf", ".nan"])
def test_initial_cash_must_be_positive_and_finite(tmp_path: Path, invalid_value: str) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text(
        BASE_CONFIG.replace("initial_cash: 1000000.0", f"initial_cash: {invalid_value}"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"mvp\.initial_cash must be positive and finite"):
        load_config(config)


@pytest.mark.parametrize("invalid_value", ["-1", ".inf", ".nan"])
def test_transaction_cost_must_be_non_negative_and_finite(
    tmp_path: Path, invalid_value: str
) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text(
        BASE_CONFIG.replace("transaction_cost_bps: 10.0", f"transaction_cost_bps: {invalid_value}"),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match=r"mvp\.transaction_cost_bps must be non-negative and finite",
    ):
        load_config(config)
