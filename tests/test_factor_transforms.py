import math
import statistics
from collections.abc import Callable

import polars as pl
import pytest

from ashare_multifactor.factors import transforms


def _transform(name: str) -> Callable[..., pl.Expr]:
    function = getattr(transforms, name, None)
    assert callable(function), f"missing transform: {name}"
    return function


@pytest.mark.parametrize(
    ("current", "previous", "expected"),
    [
        (12.0, 10.0, 0.2),
        (8.0, 10.0, -0.2),
        (0.0, 10.0, None),
        (-1.0, 10.0, None),
        (float("nan"), 10.0, None),
        (float("inf"), 10.0, None),
        (10.0, 0.0, None),
        (10.0, -1.0, None),
        (10.0, float("nan"), None),
        (10.0, float("inf"), None),
    ],
)
def test_safe_return_rejects_non_positive_or_non_finite_prices(
    current: float,
    previous: float,
    expected: float | None,
) -> None:
    safe_return = _transform("safe_return")

    actual = (
        pl.DataFrame({"current": [current], "previous": [previous]})
        .select(safe_return(pl.col("current"), pl.col("previous")).alias("return"))
        .item()
    )

    if expected is None:
        assert actual is None
    else:
        assert actual == pytest.approx(expected)


def test_rolling_sample_standard_deviation_requires_a_complete_window() -> None:
    rolling_sample_standard_deviation = _transform("rolling_sample_standard_deviation")
    values = [0.01, -0.02, 0.03, 0.04]

    actual = (
        pl.DataFrame({"value": values})
        .select(rolling_sample_standard_deviation(pl.col("value"), 3).alias("std"))
        .get_column("std")
        .to_list()
    )

    assert actual[:2] == [None, None]
    assert actual[2] == pytest.approx(statistics.stdev(values[:3]))
    assert actual[3] == pytest.approx(statistics.stdev(values[1:]))


def test_rolling_sample_standard_deviation_rejects_an_incomplete_non_null_window() -> None:
    rolling_sample_standard_deviation = _transform("rolling_sample_standard_deviation")

    actual = (
        pl.DataFrame({"value": [0.01, None, 0.03]})
        .select(rolling_sample_standard_deviation(pl.col("value"), 3).alias("std"))
        .item(2, 0)
    )

    assert actual is None


def test_rolling_downside_deviation_uses_root_mean_square_with_a_complete_window() -> None:
    rolling_downside_deviation = _transform("rolling_downside_deviation")
    values = [0.01, -0.02, 0.03, -0.04]

    actual = (
        pl.DataFrame({"value": values})
        .select(rolling_downside_deviation(pl.col("value"), 4).alias("downside"))
        .get_column("downside")
        .to_list()
    )

    expected = math.sqrt((0.0**2 + (-0.02) ** 2 + 0.0**2 + (-0.04) ** 2) / 4)
    assert actual[:3] == [None, None, None]
    assert actual[3] == pytest.approx(expected)


def test_rolling_downside_deviation_rejects_an_incomplete_non_null_window() -> None:
    rolling_downside_deviation = _transform("rolling_downside_deviation")

    actual = (
        pl.DataFrame({"value": [-0.01, None, -0.03]})
        .select(rolling_downside_deviation(pl.col("value"), 3).alias("downside"))
        .item(2, 0)
    )

    assert actual is None
