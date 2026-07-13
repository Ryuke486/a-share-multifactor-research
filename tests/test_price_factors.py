import math
import statistics
from collections.abc import Callable, Sequence
from datetime import date, timedelta

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from ashare_multifactor.factors import low_volatility, momentum, reversal
from ashare_multifactor.factors.definitions import FACTOR_DEFINITIONS


PRICE_FACTOR_COLUMNS = (
    "momentum_60",
    "momentum_120",
    "momentum_12_1",
    "reversal_5",
    "reversal_20",
    "volatility_20",
    "volatility_60",
    "downside_volatility_60",
)


def _factor_function(module: object, name: str) -> Callable[[pl.DataFrame], pl.DataFrame]:
    function = getattr(module, name, None)
    assert callable(function), f"missing factor function: {name}"
    return function


def _compute_momentum(frame: pl.DataFrame) -> pl.DataFrame:
    return _factor_function(momentum, "compute_momentum_factors")(frame)


def _compute_reversal(frame: pl.DataFrame) -> pl.DataFrame:
    return _factor_function(reversal, "compute_reversal_factors")(frame)


def _compute_low_volatility(frame: pl.DataFrame) -> pl.DataFrame:
    return _factor_function(low_volatility, "compute_low_volatility_factors")(frame)


PRICE_FAMILY_CASES = (
    (
        "momentum",
        momentum,
        _compute_momentum,
        ("momentum_60", "momentum_120", "momentum_12_1"),
    ),
    ("reversal", reversal, _compute_reversal, ("reversal_5", "reversal_20")),
    (
        "low_volatility",
        low_volatility,
        _compute_low_volatility,
        ("volatility_20", "volatility_60", "downside_volatility_60"),
    ),
)


def _registered_source_columns(factor_names: Sequence[str]) -> tuple[str, ...]:
    definitions_by_name = {definition.name: definition for definition in FACTOR_DEFINITIONS}
    return tuple(
        dict.fromkeys(
            column
            for factor_name in factor_names
            for column in definitions_by_name[factor_name].source_columns
        )
    )


def _prices_from_returns(returns: Sequence[float], initial: float = 100.0) -> list[float]:
    prices = [initial]
    for daily_return in returns[1:]:
        prices.append(prices[-1] * (1.0 + daily_return))
    return prices


def _panel(
    prices: Sequence[float],
    *,
    symbol: str = "000001",
    start: date = date(2003, 7, 1),
) -> pl.DataFrame:
    previous = [prices[0], *prices[:-1]]
    return pl.DataFrame(
        {
            "date": [start + timedelta(days=index) for index in range(len(prices))],
            "symbol": [symbol] * len(prices),
            "close_adj": prices,
            "prev_close_adj": previous,
            "volume": [1_000.0] * len(prices),
            "amount": [10_000.0] * len(prices),
            "ignored_column": ["not-a-factor-input"] * len(prices),
        }
    )


def _expected_factors(frame: pl.DataFrame, target_index: int) -> dict[str, float]:
    prices = frame.get_column("close_adj").to_list()
    previous = frame.get_column("prev_close_adj").to_list()
    daily_returns = [current / prior - 1.0 for current, prior in zip(prices, previous, strict=True)]
    returns_20 = daily_returns[target_index - 19 : target_index + 1]
    returns_60 = daily_returns[target_index - 59 : target_index + 1]
    return {
        "momentum_60": prices[target_index] / prices[target_index - 60] - 1.0,
        "momentum_120": prices[target_index] / prices[target_index - 120] - 1.0,
        "momentum_12_1": prices[target_index - 21] / prices[target_index - 252] - 1.0,
        "reversal_5": prices[target_index] / prices[target_index - 5] - 1.0,
        "reversal_20": prices[target_index] / prices[target_index - 20] - 1.0,
        "volatility_20": statistics.stdev(returns_20) * math.sqrt(252.0),
        "volatility_60": statistics.stdev(returns_60) * math.sqrt(252.0),
        "downside_volatility_60": math.sqrt(sum(min(value, 0.0) ** 2 for value in returns_60) / 60)
        * math.sqrt(252.0),
    }


@pytest.mark.parametrize(
    "prices",
    [
        pytest.param([100.0 + index for index in range(253)], id="monotonic-increase"),
        pytest.param([400.0 - index for index in range(253)], id="monotonic-decrease"),
        pytest.param(
            _prices_from_returns([0.0, 0.01, -0.02, 0.03, -0.01] * 51)[:253],
            id="mixed-returns",
        ),
    ],
)
def test_all_price_factors_match_hand_calculation_across_years(prices: list[float]) -> None:
    frame = _panel(prices)
    target_index = 252
    target_date = frame.item(target_index, "date")
    expected = _expected_factors(frame, target_index)

    actual = {}
    for computed in (
        _compute_momentum(frame),
        _compute_reversal(frame),
        _compute_low_volatility(frame),
    ):
        actual.update(computed.filter(pl.col("date") == target_date).row(0, named=True))

    for factor_name in PRICE_FACTOR_COLUMNS:
        assert actual[factor_name] == pytest.approx(expected[factor_name])


def test_momentum_12_1_uses_exact_t_minus_21_and_t_minus_252_observations() -> None:
    frame = _panel([100.0 + index for index in range(253)])
    target_date = frame.item(252, "date")
    baseline = _compute_momentum(frame).filter(pl.col("date") == target_date).row(0, named=True)
    changed_recent = frame.with_columns(
        pl.when(pl.int_range(pl.len()) >= 232)
        .then(pl.col("close_adj") * 10.0)
        .otherwise(pl.col("close_adj"))
        .alias("close_adj")
    )
    changed_boundary = frame.with_columns(
        pl.when(pl.int_range(pl.len()) == 231)
        .then(pl.col("close_adj") * 2.0)
        .otherwise(pl.col("close_adj"))
        .alias("close_adj")
    )

    recent = (
        _compute_momentum(changed_recent).filter(pl.col("date") == target_date).row(0, named=True)
    )
    boundary = (
        _compute_momentum(changed_boundary).filter(pl.col("date") == target_date).row(0, named=True)
    )

    assert recent["momentum_12_1"] == pytest.approx(baseline["momentum_12_1"])
    assert boundary["momentum_12_1"] == pytest.approx(
        (frame.item(231, "close_adj") * 2.0) / frame.item(0, "close_adj") - 1.0
    )
    assert baseline["momentum_60"] > 0.0


def test_invalid_rows_are_retained_as_null_and_excluded_from_observation_windows() -> None:
    frame = _panel([100.0 + index for index in range(62)]).with_columns(
        pl.when(pl.int_range(pl.len()) == 30).then(0.0).otherwise(pl.col("volume")).alias("volume")
    )
    invalid_date = frame.item(30, "date")
    target_date = frame.item(61, "date")
    outputs = (
        _compute_momentum(frame),
        _compute_reversal(frame),
        _compute_low_volatility(frame),
    )

    invalid_values: dict[str, float | None] = {}
    target_values: dict[str, float | None] = {}
    for output in outputs:
        invalid_values.update(output.filter(pl.col("date") == invalid_date).row(0, named=True))
        target_values.update(output.filter(pl.col("date") == target_date).row(0, named=True))

    assert [invalid_values[name] for name in PRICE_FACTOR_COLUMNS] == [None] * 8
    assert target_values["momentum_60"] == pytest.approx(161.0 / 100.0 - 1.0)
    assert target_values["momentum_120"] is None
    assert target_values["momentum_12_1"] is None
    assert target_values["reversal_5"] == pytest.approx(161.0 / 156.0 - 1.0)
    assert target_values["reversal_20"] == pytest.approx(161.0 / 141.0 - 1.0)

    valid_returns = (
        frame.filter(pl.col("volume") > 0)
        .select((pl.col("close_adj") / pl.col("prev_close_adj") - 1.0).alias("return"))
        .get_column("return")
        .to_list()
    )
    assert target_values["volatility_20"] == pytest.approx(
        statistics.stdev(valid_returns[-20:]) * math.sqrt(252.0)
    )
    assert target_values["volatility_60"] == pytest.approx(
        statistics.stdev(valid_returns[-60:]) * math.sqrt(252.0)
    )
    assert target_values["downside_volatility_60"] == pytest.approx(0.0)


@pytest.mark.parametrize("invalid_close", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_current_price_nulls_every_price_factor(invalid_close: float) -> None:
    frame = _panel([100.0 + index for index in range(253)]).with_columns(
        pl.when(pl.int_range(pl.len()) == 252)
        .then(invalid_close)
        .otherwise(pl.col("close_adj"))
        .alias("close_adj")
    )
    target_date = frame.item(252, "date")
    values: dict[str, float | None] = {}

    for output in (
        _compute_momentum(frame),
        _compute_reversal(frame),
        _compute_low_volatility(frame),
    ):
        values.update(output.filter(pl.col("date") == target_date).row(0, named=True))

    assert [values[name] for name in PRICE_FACTOR_COLUMNS] == [None] * 8


@pytest.mark.parametrize("invalid_previous", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_previous_adjusted_close_nulls_affected_volatility_windows(
    invalid_previous: float,
) -> None:
    frame = _panel([100.0 + index for index in range(61)]).with_columns(
        pl.when(pl.int_range(pl.len()) == 60)
        .then(invalid_previous)
        .otherwise(pl.col("prev_close_adj"))
        .alias("prev_close_adj")
    )

    target = _compute_low_volatility(frame).row(60, named=True)

    assert target["volatility_20"] is None
    assert target["volatility_60"] is None
    assert target["downside_volatility_60"] is None


def test_each_factor_family_returns_only_keys_and_its_raw_columns() -> None:
    frame = _panel([100.0 + index for index in range(253)])

    assert _compute_momentum(frame).columns == [
        "date",
        "symbol",
        "momentum_60",
        "momentum_120",
        "momentum_12_1",
    ]
    assert _compute_reversal(frame).columns == [
        "date",
        "symbol",
        "reversal_5",
        "reversal_20",
    ]
    assert _compute_low_volatility(frame).columns == [
        "date",
        "symbol",
        "volatility_20",
        "volatility_60",
        "downside_volatility_60",
    ]


@pytest.mark.parametrize(
    ("family", "module", "compute", "factor_names"),
    PRICE_FAMILY_CASES,
    ids=[case[0] for case in PRICE_FAMILY_CASES],
)
def test_registered_source_projection_is_sufficient_for_each_price_family(
    family: str,
    module: object,
    compute: Callable[[pl.DataFrame], pl.DataFrame],
    factor_names: tuple[str, ...],
) -> None:
    del family, module
    frame = _panel([100.0 + index for index in range(253)])
    source_columns = _registered_source_columns(factor_names)
    projected = frame.select("date", "symbol", *source_columns)
    expected = _expected_factors(frame, 252)

    try:
        target = compute(projected).row(252, named=True)
    except pl.exceptions.ColumnNotFoundError as error:
        pytest.fail(f"registered source projection is incomplete: {error}")

    for factor_name in factor_names:
        assert target[factor_name] == pytest.approx(expected[factor_name])


@pytest.mark.parametrize(
    ("family", "module", "compute", "factor_names"),
    PRICE_FAMILY_CASES,
    ids=[case[0] for case in PRICE_FAMILY_CASES],
)
def test_runtime_source_columns_are_declared_by_the_factor_registry(
    family: str,
    module: object,
    compute: Callable[[pl.DataFrame], pl.DataFrame],
    factor_names: tuple[str, ...],
) -> None:
    del family, compute
    runtime_source_columns = getattr(module, "_SOURCE_COLUMNS", None)

    assert runtime_source_columns is not None
    assert set(runtime_source_columns) <= set(_registered_source_columns(factor_names))


@pytest.mark.parametrize(
    "compute",
    [_compute_momentum, _compute_reversal, _compute_low_volatility],
    ids=["momentum", "reversal", "low-volatility"],
)
def test_factor_results_are_stable_for_multiple_symbols_and_shuffled_input(
    compute: Callable[[pl.DataFrame], pl.DataFrame],
) -> None:
    frame = pl.concat(
        [
            _panel([100.0 + index for index in range(253)], symbol="000001"),
            _panel([400.0 - index for index in range(253)], symbol="000002"),
        ]
    )
    shuffled = frame.sample(fraction=1.0, shuffle=True, seed=42)

    assert_frame_equal(compute(shuffled), compute(frame))


@pytest.mark.parametrize(
    "compute",
    [_compute_momentum, _compute_reversal, _compute_low_volatility],
    ids=["momentum", "reversal", "low-volatility"],
)
def test_factor_families_reject_duplicate_date_symbol_keys(
    compute: Callable[[pl.DataFrame], pl.DataFrame],
) -> None:
    frame = _panel([100.0, 101.0, 102.0])
    duplicate = pl.concat([frame, frame.head(1)])

    with pytest.raises(ValueError, match="duplicate.*date.*symbol"):
        compute(duplicate)
