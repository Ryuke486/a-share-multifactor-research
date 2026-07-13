import math

import pytest

from ashare_multifactor.research.factor_statistics import (
    benjamini_hochberg,
    newey_west_mean_test,
)


def test_newey_west_lag_zero_matches_manual_bartlett_calculation() -> None:
    result = newey_west_mean_test([1.0, 2.0, 3.0, 4.0], horizon=20)

    assert result["mean"] == pytest.approx(2.5)
    assert result["se"] == pytest.approx(math.sqrt(1.25 / 4.0))
    assert result["t"] == pytest.approx(2.5 / math.sqrt(1.25 / 4.0))
    assert result["p"] == pytest.approx(7.744216431044088e-06)
    assert result["n"] == 4
    assert result["lag"] == 0
    assert result["reason"] is None


def test_newey_west_uses_requested_horizon_lag_and_bartlett_weights() -> None:
    result = newey_west_mean_test([1.0, 2.0, 4.0, 8.0], horizon=60)
    manual_long_run_variance = 7.1875 + 2.0 * ((2.0 / 3.0) * 1.359375 + (1.0 / 3.0) * -2.03125)

    assert result["lag"] == 2
    assert result["se"] == pytest.approx(math.sqrt(manual_long_run_variance / 4.0))
    assert result["t"] == pytest.approx(3.75 / math.sqrt(manual_long_run_variance / 4.0))
    assert result["reason"] is None


@pytest.mark.parametrize(
    ("values", "expected_n", "expected_mean", "expected_reason"),
    [
        ([], 0, None, "no_finite_observations"),
        ([None, math.nan, math.inf], 0, None, "no_finite_observations"),
        ([1.5], 1, 1.5, "insufficient_observations"),
        ([2.0, 2.0, 2.0], 3, 2.0, "non_positive_long_run_variance"),
    ],
)
def test_newey_west_degenerate_inputs_never_fabricate_significance(
    values: list[float | None],
    expected_n: int,
    expected_mean: float | None,
    expected_reason: str,
) -> None:
    result = newey_west_mean_test(values, horizon=20)

    assert result["n"] == expected_n
    assert result["mean"] == expected_mean
    assert result["se"] is None
    assert result["t"] is None
    assert result["p"] is None
    assert result["reason"] == expected_reason


def test_benjamini_hochberg_preserves_order_nulls_and_monotone_q_values() -> None:
    q_values = benjamini_hochberg([0.01, None, 0.04, math.nan, 0.03, -0.1, 1.1])

    assert q_values == pytest.approx([0.03, None, 0.04, None, 0.04, None, None], nan_ok=True)
    valid_pairs = sorted(
        (p, q) for p, q in zip([0.01, 0.04, 0.03], [q_values[0], q_values[2], q_values[4]])
    )
    assert [q for _, q in valid_pairs] == sorted(q for _, q in valid_pairs)
    assert all(q is not None and 0.0 <= q <= 1.0 for q in q_values if q is not None)


def test_benjamini_hochberg_empty_family_returns_empty_list() -> None:
    assert benjamini_hochberg([]) == []
