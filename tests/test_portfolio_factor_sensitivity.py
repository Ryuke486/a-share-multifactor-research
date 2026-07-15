from datetime import date
from pathlib import Path

import polars as pl
import pytest

from ashare_multifactor.robustness.factor_ablation import build_ablation_scores
from ashare_multifactor.robustness.portfolio_sensitivity import (
    apply_rebalance_schedule,
    build_portfolio_variants,
    restrict_liquid_universe,
)
from ashare_multifactor.robustness.protocol import load_robustness_protocol


def test_portfolio_variants_are_pre_registered_one_change_designs() -> None:
    protocol = load_robustness_protocol(Path("configs/robustness_protocol.yaml"))
    variants = {item.experiment_id: item for item in build_portfolio_variants(protocol)}
    baseline = variants["portfolio_baseline"].parameters

    assert baseline == {
        "portfolio_size": 100,
        "buffer_rank": 30,
        "universe_size": 1000,
        "rebalance_months": 1,
    }
    assert variants["portfolio_size_low"].parameters["portfolio_size"] == 80
    assert variants["portfolio_size_low"].parameters["buffer_rank"] == 30
    assert variants["portfolio_buffer_high"].parameters["buffer_rank"] == 40


def test_rebalance_and_liquidity_filters_are_stable() -> None:
    dates = [date(2017, month, 28) for month in range(1, 5)]
    scores = pl.DataFrame(
        {
            "date": [value for value in dates for _ in range(3)],
            "symbol": ["000001", "000002", "000003"] * 4,
            "score": [3.0, 2.0, 1.0] * 4,
        }
    )
    liquidity = scores.select("date", "symbol").with_columns(
        pl.Series("liquidity", [1.0, 3.0, 2.0] * 4)
    )

    scheduled = apply_rebalance_schedule(scores, every_months=2)
    restricted = restrict_liquid_universe(scores, liquidity, universe_size=2)

    assert scheduled.get_column("date").unique().sort().to_list() == dates[::2]
    assert restricted.filter(pl.col("date") == dates[0]).get_column("symbol").to_list() == [
        "000002",
        "000003",
    ]
    with pytest.raises(ValueError, match="requested universe exceeds frozen upstream"):
        restrict_liquid_universe(scores, liquidity, universe_size=4)


def test_family_ablation_removes_only_registered_family() -> None:
    panel = pl.DataFrame(
        {
            "date": [date(2017, 1, 31)] * 6,
            "symbol": ["000001"] * 3 + ["000002"] * 3,
            "factor_name": ["liq", "rev", "vol"] * 2,
            "family": ["liquidity", "reversal", "low_volatility"] * 2,
            "score_size_neutral": [100.0, 1.0, 3.0, -100.0, 2.0, 4.0],
        }
    )
    weights = pl.DataFrame(
        {
            "date": [date(2017, 1, 31)] * 3,
            "factor_name": ["liq", "rev", "vol"],
            "family": ["liquidity", "reversal", "low_volatility"],
            "weight": [1 / 3, 1 / 3, 1 / 3],
        }
    )

    result = build_ablation_scores(panel, weights, removed_family="liquidity")
    changed = panel.with_columns(
        pl.when(pl.col("family") == "liquidity")
        .then(pl.col("score_size_neutral") * 1000)
        .otherwise(pl.col("score_size_neutral"))
        .alias("score_size_neutral")
    )

    assert result.get_column("family_count").to_list() == [2, 2]
    assert result.select("date", "symbol", "score").equals(
        build_ablation_scores(
            changed, weights, removed_family="liquidity"
        ).select("date", "symbol", "score")
    )
    with pytest.raises(ValueError, match="not in frozen candidate families"):
        build_ablation_scores(panel, weights, removed_family="new_factor_family")
