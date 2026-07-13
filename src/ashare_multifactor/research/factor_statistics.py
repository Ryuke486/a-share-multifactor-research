"""Small statistical primitives used by single-factor evaluation."""

from __future__ import annotations

from collections.abc import Sequence
import math

import numpy as np
from scipy import stats


def newey_west_mean_test(
    values: Sequence[float | None],
    *,
    horizon: int,
) -> dict[str, float | int | str | None]:
    """Test a sample mean with a Bartlett-kernel Newey-West standard error."""
    lag = max(0, math.ceil(horizon / 21) - 1)
    sample = np.asarray(
        [value for value in values if value is not None and np.isfinite(value)],
        dtype=float,
    )
    n = int(sample.size)
    mean = float(np.mean(sample)) if n else None
    result: dict[str, float | int | str | None] = {
        "mean": mean,
        "se": None,
        "t": None,
        "p": None,
        "n": n,
        "lag": lag,
        "reason": None,
    }
    if n == 0:
        result["reason"] = "no_finite_observations"
        return result
    if n == 1:
        result["reason"] = "insufficient_observations"
        return result

    residuals = sample - mean
    effective_lag = min(lag, n - 1)
    long_run_variance = float(np.dot(residuals, residuals) / n)
    for offset in range(1, effective_lag + 1):
        autocovariance = float(np.dot(residuals[offset:], residuals[:-offset]) / n)
        weight = 1.0 - offset / (lag + 1.0)
        long_run_variance += 2.0 * weight * autocovariance
    if not np.isfinite(long_run_variance) or long_run_variance <= 0.0:
        result["reason"] = "non_positive_long_run_variance"
        return result

    standard_error = math.sqrt(long_run_variance / n)
    test_statistic = mean / standard_error
    result.update(
        {
            "se": standard_error,
            "t": test_statistic,
            "p": float(2.0 * stats.norm.sf(abs(test_statistic))),
        }
    )
    return result


def benjamini_hochberg(p_values: Sequence[float | None]) -> list[float | None]:
    """Return BH q-values in input order while retaining invalid entries as null."""
    result: list[float | None] = [None] * len(p_values)
    valid = [
        (index, float(value))
        for index, value in enumerate(p_values)
        if value is not None and np.isfinite(value) and 0.0 <= value <= 1.0
    ]
    if not valid:
        return result

    ordered = sorted(valid, key=lambda item: (item[1], item[0]))
    count = len(ordered)
    adjusted = [min(1.0, value * count / rank) for rank, (_, value) in enumerate(ordered, 1)]
    for index in range(count - 2, -1, -1):
        adjusted[index] = min(adjusted[index], adjusted[index + 1])
    for (original_index, _), q_value in zip(ordered, adjusted):
        result[original_index] = q_value
    return result
