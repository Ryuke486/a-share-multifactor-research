from datetime import date

import polars as pl
import pytest

from ashare_multifactor.supplements.legs import quantile_leg_decomposition


def _quantiles(rows: list[tuple[date, str, int, int, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema=["date", "object_name", "quantile", "n_obs", "mean_return"],
        orient="row",
    )


def test_legs_split_the_spread_around_the_count_weighted_universe_mean() -> None:
    first, second = date(2017, 1, 26), date(2017, 2, 28)
    quantiles = _quantiles(
        [
            # universe mean (10*0.00 + 20*0.01 + 10*0.06) / 40 = 0.02
            (first, "factor", 1, 10, 0.00),
            (first, "factor", 2, 20, 0.01),
            (first, "factor", 3, 10, 0.06),
            # universe mean 0.00
            (second, "factor", 1, 10, -0.04),
            (second, "factor", 2, 20, 0.00),
            (second, "factor", 3, 10, 0.04),
        ]
    )

    result = quantile_leg_decomposition(quantiles, quantile_count=3).row(0, named=True)

    assert result["object_name"] == "factor"
    assert result["months"] == 2
    assert result["long_leg"] == pytest.approx((0.04 + 0.04) / 2)
    assert result["short_leg"] == pytest.approx((0.02 + 0.04) / 2)
    assert result["spread"] == pytest.approx(0.07)
    assert result["long_share"] == pytest.approx(0.04 / 0.07)


def test_long_share_is_undefined_without_a_positive_spread() -> None:
    day = date(2017, 1, 26)
    quantiles = _quantiles(
        [
            (day, "reversed", 1, 10, 0.03),
            (day, "reversed", 2, 10, 0.00),
        ]
    )

    result = quantile_leg_decomposition(quantiles, quantile_count=2).row(0, named=True)

    assert result["spread"] == pytest.approx(-0.03)
    assert result["long_share"] is None


def test_months_missing_an_extreme_quantile_are_excluded() -> None:
    first, second = date(2017, 1, 26), date(2017, 2, 28)
    quantiles = _quantiles(
        [
            (first, "factor", 1, 10, 0.00),
            (first, "factor", 2, 10, 0.02),
            (second, "factor", 1, 10, 0.50),
        ]
    )

    result = quantile_leg_decomposition(quantiles, quantile_count=2).row(0, named=True)

    assert result["months"] == 1
    assert result["long_leg"] == pytest.approx(0.01)


def test_leg_decomposition_rejects_duplicate_quantile_rows() -> None:
    day = date(2017, 1, 26)
    quantiles = _quantiles([(day, "factor", 1, 10, 0.0), (day, "factor", 1, 10, 0.0)])

    with pytest.raises(ValueError, match="duplicate"):
        quantile_leg_decomposition(quantiles, quantile_count=2)
