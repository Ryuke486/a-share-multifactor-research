from datetime import date

import polars as pl
import pytest

from ashare_multifactor.combination.eligibility import candidate_registry
from ashare_multifactor.combination.panel import (
    build_composite_scores,
    build_rolling_composite_scores,
)
from ashare_multifactor.combination.rolling_ic import rolling_ic_weights


def _classifications() -> pl.DataFrame:
    candidates = [
        ("reversal_5", "reversal"),
        ("reversal_20", "reversal"),
        ("turnover_20", "liquidity"),
        ("amihud_20", "liquidity"),
        ("volatility_20", "low_volatility"),
        ("volatility_60", "low_volatility"),
    ]
    return pl.DataFrame(
        {
            "factor_name": [name for name, _ in candidates] + ["ep_ttm"],
            "family": [family for _, family in candidates] + ["value"],
            "classification": ["candidate"] * 6 + ["watch"],
            "primary_score_variant": ["score_size_neutral"] * 7,
        }
    )


def test_candidate_registry_rejects_watch_factor() -> None:
    with pytest.raises(ValueError, match="non-candidate"):
        candidate_registry(_classifications(), requested=("reversal_5", "ep_ttm"))


def test_family_equal_reweights_available_factors_and_requires_two_families() -> None:
    panel = pl.DataFrame(
        {
            "date": [date(2005, 1, 31)] * 5,
            "symbol": ["000001", "000001", "000002", "000002", "000002"],
            "factor_name": [
                "reversal_5",
                "turnover_20",
                "reversal_5",
                "turnover_20",
                "volatility_60",
            ],
            "family": ["reversal", "liquidity", "reversal", "liquidity", "low_volatility"],
            "score_size_neutral": [1.0, 2.0, -1.0, 0.0, 1.0],
        }
    )
    classifications = _classifications()

    result = build_composite_scores(panel, classifications, methods=("family_equal",))

    assert result.select("date", "symbol", "method").is_duplicated().any() is False
    assert result.get_column("symbol").to_list() == ["000001", "000002"]
    assert result.get_column("family_count").to_list() == [2, 3]
    assert result.get_column("score").mean() == pytest.approx(0.0)
    assert result.get_column("score").std(ddof=0) == pytest.approx(1.0)


def test_rolling_ic_weights_use_only_strictly_prior_months() -> None:
    dates = [date(2005, month, 28) for month in range(1, 5)]
    rank_ic = pl.DataFrame(
        {
            "date": dates * 2,
            "factor_name": ["reversal_5"] * 4 + ["reversal_20"] * 4,
            "family": ["reversal"] * 8,
            "score_variant": ["score_size_neutral"] * 8,
            "horizon": [20] * 8,
            "rank_ic": [0.1, 0.1, 0.1, 99.0, 0.3, 0.3, 0.3, -99.0],
        }
    )

    weights = rolling_ic_weights(
        rank_ic,
        weight_dates=(date(2005, 4, 28),),
        factors={"reversal": ("reversal_5", "reversal_20")},
        window_months=36,
        minimum_months=3,
        shrinkage=0.5,
        realization_dates=pl.DataFrame({"date": dates, "realization_date": dates}),
    )

    actual = dict(zip(weights["factor_name"], weights["factor_weight"]))
    assert actual == pytest.approx({"reversal_5": 0.375, "reversal_20": 0.625})
    assert weights.get_column("history_end").unique().item() == date(2005, 3, 28)


def test_rolling_ic_excludes_ic_not_realized_before_weight_date() -> None:
    rank_ic = pl.DataFrame({
        "date": [date(2005, 1, 31), date(2005, 2, 28)] * 2,
        "factor_name": ["reversal_5"] * 2 + ["reversal_20"] * 2,
        "family": ["reversal"] * 4,
        "score_variant": ["score_size_neutral"] * 4,
        "horizon": [20] * 4,
        "rank_ic": [1.0, 99.0, 3.0, -99.0],
    })
    realization = pl.DataFrame({
        "date": [date(2005, 1, 31), date(2005, 2, 28)],
        "realization_date": [date(2005, 3, 10), date(2005, 4, 10)],
    })

    weights = rolling_ic_weights(
        rank_ic, weight_dates=(date(2005, 4, 1),),
        factors={"reversal": ("reversal_5", "reversal_20")},
        window_months=36, minimum_months=1, shrinkage=0.5,
        realization_dates=realization,
    )

    actual = dict(zip(weights["factor_name"], weights["factor_weight"]))
    assert actual == pytest.approx({"reversal_5": 0.375, "reversal_20": 0.625})


def test_combination_rejects_validation_period_rows() -> None:
    panel = pl.DataFrame(
        {
            "date": [date(2017, 1, 31)],
            "symbol": ["000001"],
            "factor_name": ["reversal_5"],
            "family": ["reversal"],
            "score_size_neutral": [1.0],
        }
    )
    with pytest.raises(ValueError, match="research period"):
        build_composite_scores(panel, _classifications(), methods=("candidate_equal",))


def test_combination_uses_configured_minimum_families() -> None:
    panel = pl.DataFrame(
        {
            "date": [date(2005, 1, 31)] * 3,
            "symbol": ["000001"] * 3,
            "factor_name": ["reversal_5", "turnover_20", "volatility_60"],
            "family": ["reversal", "liquidity", "low_volatility"],
            "score_size_neutral": [1.0, 2.0, 3.0],
        }
    )

    result = build_composite_scores(
        panel,
        _classifications(),
        methods=("family_equal",),
        minimum_families=3,
        analysis_start=date(2005, 1, 1),
        analysis_end=date(2016, 12, 31),
    )

    assert result.height == 1


def test_candidate_registry_rejects_wrong_family_mapping() -> None:
    classifications = _classifications().with_columns(
        pl.when(pl.col("factor_name") == "reversal_5")
        .then(pl.lit("momentum"))
        .otherwise(pl.col("family"))
        .alias("family")
    )

    with pytest.raises(ValueError, match="families"):
        candidate_registry(classifications)


def test_static_composite_scores_are_bitwise_independent_of_input_order() -> None:
    factors = [
        ("reversal_5", "reversal"),
        ("reversal_20", "reversal"),
        ("turnover_20", "liquidity"),
        ("amihud_20", "liquidity"),
        ("volatility_20", "low_volatility"),
        ("volatility_60", "low_volatility"),
    ]
    rows = []
    for index in range(50):
        for factor_index, (factor, family) in enumerate(factors):
            rows.append(
                {
                    "date": date(2005, 1, 31),
                    "symbol": f"{index:06d}",
                    "factor_name": factor,
                    "family": family,
                    "score_size_neutral": (index + 1) / (factor_index + 3),
                }
            )
    panel = pl.DataFrame(rows)

    forward = build_composite_scores(
        panel, _classifications(), methods=("candidate_equal", "family_equal")
    )
    reverse = build_composite_scores(
        panel.reverse(), _classifications(), methods=("candidate_equal", "family_equal")
    )

    assert forward.equals(reverse)


def test_rolling_composite_scores_are_bitwise_independent_of_input_order() -> None:
    signal_date = date(2005, 1, 31)
    panel = pl.DataFrame(
        [
            {
                "date": signal_date,
                "symbol": f"{index:06d}",
                "factor_name": factor,
                "family": family,
                "score_size_neutral": (index + 1) / (factor_index + 3),
            }
            for index in range(50)
            for factor_index, (factor, family) in enumerate(
                [
                    ("reversal_5", "reversal"),
                    ("turnover_20", "liquidity"),
                    ("volatility_60", "low_volatility"),
                ]
            )
        ]
    )
    weights = panel.select("date", "factor_name").unique().with_columns(
        pl.lit(1.0).alias("factor_weight")
    )

    forward = build_rolling_composite_scores(panel, _classifications(), weights)
    reverse = build_rolling_composite_scores(panel.reverse(), _classifications(), weights)

    assert forward.equals(reverse)
