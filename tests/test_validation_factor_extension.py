from dataclasses import replace
from datetime import date
from pathlib import Path

import polars as pl
from polars.testing import assert_frame_equal

from factor_pipeline_fixtures import synthetic_panel
from test_combination_core import _classifications

from ashare_multifactor.config import load_config
from ashare_multifactor.data.field_audit import audit_factor_fields
from ashare_multifactor.factors.definitions import FACTOR_DEFINITIONS
from ashare_multifactor.validation.combination_extension import (
    build_validation_combinations,
)
from ashare_multifactor.validation.factor_extension import (
    build_validation_factor_extension,
)


def test_factor_extension_reuses_registry_and_keeps_validation_dates_only() -> None:
    daily = synthetic_panel().with_columns(pl.col("date") + pl.duration(days=12 * 365))
    settings = replace(
        load_config(Path("configs/research_protocol.yaml")).factor_research,
        data_start=daily.get_column("date").min(),
        analysis_start=date(2017, 1, 1),
        analysis_end=date(2017, 12, 31),
        minimum_history=20,
        universe_size=25,
        minimum_valid_months=1,
    )
    readiness = audit_factor_fields(
        daily,
        FACTOR_DEFINITIONS,
        valuation_verified=False,
        industry_verified=False,
        historical_st_verified=False,
    )

    result = build_validation_factor_extension(daily, readiness, settings)

    assert result.panel.get_column("date").min() >= date(2017, 1, 1)
    assert result.panel.get_column("date").max() <= date(2017, 12, 31)
    assert set(result.panel.get_column("factor_name")) == {
        definition.name for definition in FACTOR_DEFINITIONS
    }
    assert (
        result.panel.filter(pl.col("family") == "value")
        .get_column("point_in_time_status")
        .unique()
        .to_list()
        == ["unverified"]
    )
    assert result.audit.filter(pl.col("factor_name") == "reversal_20").height == 1


def test_future_validation_ic_cannot_change_past_rolling_weights() -> None:
    factors = [
        ("reversal_5", "reversal"),
        ("reversal_20", "reversal"),
        ("turnover_20", "liquidity"),
        ("amihud_20", "liquidity"),
        ("volatility_20", "low_volatility"),
        ("volatility_60", "low_volatility"),
    ]
    signal_dates = [date(2017, 1, 31), date(2017, 2, 28)]
    panel = pl.DataFrame(
        [
            {
                "date": signal_date,
                "symbol": f"{symbol:06d}",
                "factor_name": factor,
                "family": family,
                "score_size_neutral": float(symbol + factor_index),
            }
            for signal_date in signal_dates
            for symbol in range(1, 5)
            for factor_index, (factor, family) in enumerate(factors)
        ]
    )
    history_date = date(2016, 11, 30)
    historical = _rank_ic_rows(history_date, factors, 0.1)
    validation = pl.concat(
        (
            _rank_ic_rows(signal_dates[0], factors, 0.2),
            _rank_ic_rows(signal_dates[1], factors, 0.3),
        )
    )
    realization = pl.DataFrame(
        {
            "date": [history_date, *signal_dates],
            "realization_date": [
                date(2016, 12, 20),
                date(2017, 2, 28),
                date(2017, 3, 31),
            ],
        }
    )

    first = build_validation_combinations(
        panel,
        _classifications(),
        historical,
        validation,
        realization,
        minimum_families=2,
        window_months=36,
        minimum_months=1,
        shrinkage=0.5,
    )
    changed_future = validation.with_columns(
        pl.when(pl.col("date") == signal_dates[1])
        .then(999.0)
        .otherwise(pl.col("rank_ic"))
        .alias("rank_ic")
    )
    second = build_validation_combinations(
        panel,
        _classifications(),
        historical,
        changed_future,
        realization,
        minimum_families=2,
        window_months=36,
        minimum_months=1,
        shrinkage=0.5,
    )

    assert_frame_equal(
        first.weights.filter(pl.col("date") == signal_dates[0]),
        second.weights.filter(pl.col("date") == signal_dates[0]),
    )


def _rank_ic_rows(
    signal_date: date, factors: list[tuple[str, str]], value: float
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [signal_date] * len(factors),
            "factor_name": [factor for factor, _ in factors],
            "family": [family for _, family in factors],
            "score_variant": ["score_size_neutral"] * len(factors),
            "horizon": [20] * len(factors),
            "rank_ic": [value + index / 100 for index in range(len(factors))],
        }
    )
