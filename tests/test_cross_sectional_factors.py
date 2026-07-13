import importlib
import math
from collections.abc import Callable, Sequence
from datetime import date, timedelta

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from ashare_multifactor.data.field_audit import audit_factor_fields
from ashare_multifactor.factors.definitions import FACTOR_DEFINITIONS, factor_source_columns


VALUE_FACTORS = ("ep_ttm", "bp", "sp_ttm")
LIQUIDITY_FACTORS = ("turnover_20", "amihud_20")
SIZE_FACTORS = ("log_market_cap",)


def _factor_function(
    module_name: str, function_name: str
) -> Callable[[pl.DataFrame], pl.DataFrame]:
    try:
        module = importlib.import_module(f"ashare_multifactor.factors.{module_name}")
    except ModuleNotFoundError:
        pytest.fail(f"missing factor module: {module_name}")
    function = getattr(module, function_name, None)
    assert callable(function), f"missing factor function: {function_name}"
    return function


def _compute_value(frame: pl.DataFrame) -> pl.DataFrame:
    return _factor_function("value", "compute_value_factors")(frame)


def _compute_liquidity(frame: pl.DataFrame) -> pl.DataFrame:
    return _factor_function("liquidity", "compute_liquidity_factors")(frame)


def _compute_size(frame: pl.DataFrame) -> pl.DataFrame:
    return _factor_function("size", "compute_size_factor")(frame)


def _liquidity_panel(
    count: int,
    *,
    symbol: str = "000001",
    start: date = date(2005, 1, 1),
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [start + timedelta(days=index) for index in range(count)],
            "symbol": [symbol] * count,
            "turnover_rate": [index / 100.0 for index in range(count)],
            "close_adj": [101.0] * count,
            "prev_close_adj": [100.0] * count,
            "volume": [100.0] * count,
            "amount": [1_000.0] * count,
            "ignored": ["audit-only"] * count,
        }
    )


def _value_panel(*, symbol: str = "000001") -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [date(2005, 1, 4), date(2005, 1, 5)],
            "symbol": [symbol, symbol],
            "pe_ttm": [10.0, 20.0],
            "pb": [2.0, 4.0],
            "ps_ttm": [5.0, 10.0],
        }
    )


def _size_panel(*, symbol: str = "000001") -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [date(2005, 1, 4), date(2005, 1, 5)],
            "symbol": [symbol, symbol],
            "total_market_cap": [100.0, 1_000.0],
        }
    )


def _registered_projection(frame: pl.DataFrame, factor_names: Sequence[str]) -> pl.DataFrame:
    return frame.select("date", "symbol", *factor_source_columns(factor_names))


def test_value_factors_only_invert_finite_strictly_positive_denominators() -> None:
    denominators = [10.0, 0.0, -2.0, None, float("nan"), float("inf"), -float("inf")]
    frame = pl.DataFrame(
        {
            "date": [
                date(2005, 1, 1) + timedelta(days=index) for index in range(len(denominators))
            ],
            "symbol": ["000001"] * len(denominators),
            "pe_ttm": denominators,
            "pb": denominators,
            "ps_ttm": denominators,
        }
    )

    actual = _compute_value(frame)

    for factor_name in VALUE_FACTORS:
        assert actual.get_column(factor_name).to_list() == [0.1, None, None, None, None, None, None]


def test_value_numbers_survive_unverified_point_in_time_status() -> None:
    frame = _value_panel()

    readiness = audit_factor_fields(frame, valuation_verified=False)
    actual = _compute_value(frame)

    assert {name: readiness.factor_status[name] for name in VALUE_FACTORS} == {
        "ep_ttm": "unverified",
        "bp": "unverified",
        "sp_ttm": "unverified",
    }
    assert actual.select(VALUE_FACTORS).row(0) == pytest.approx((0.1, 0.5, 0.2))


def test_value_and_size_do_not_apply_trade_observation_filtering() -> None:
    frame = pl.DataFrame(
        {
            "date": [date(2005, 1, 4)],
            "symbol": ["000001"],
            "pe_ttm": [10.0],
            "pb": [2.0],
            "ps_ttm": [5.0],
            "total_market_cap": [100.0],
            "close_adj": [0.0],
            "volume": [0.0],
            "amount": [0.0],
        }
    )

    assert _compute_value(frame).select(VALUE_FACTORS).row(0) == pytest.approx((0.1, 0.5, 0.2))
    assert _compute_size(frame).get_column("log_market_cap").item() == pytest.approx(
        math.log(100.0)
    )


def test_liquidity_uses_exactly_twenty_valid_trade_observations() -> None:
    frame = _liquidity_panel(21).with_columns(
        pl.when(pl.int_range(pl.len()) == 5).then(0.0).otherwise(pl.col("volume")).alias("volume")
    )

    actual = _compute_liquidity(frame)
    invalid = actual.filter(pl.col("date") == frame.item(5, "date"))
    target = actual.filter(pl.col("date") == frame.item(20, "date"))

    expected_turnover = sum(index / 100.0 for index in range(21) if index != 5) / 20
    assert invalid.select(LIQUIDITY_FACTORS).row(0) == (None, None)
    assert target.get_column("turnover_20").item() == pytest.approx(expected_turnover)
    assert target.get_column("amihud_20").item() == pytest.approx(0.01 / 1_000.0)


def test_invalid_factor_values_remain_inside_valid_trade_windows_as_nulls() -> None:
    frame = _liquidity_panel(20).with_columns(
        pl.when(pl.int_range(pl.len()) == 3)
        .then(None)
        .otherwise(pl.col("turnover_rate"))
        .alias("turnover_rate"),
        pl.when(pl.int_range(pl.len()) == 4)
        .then(0.0)
        .otherwise(pl.col("prev_close_adj"))
        .alias("prev_close_adj"),
    )

    target = _compute_liquidity(frame).filter(pl.col("date") == frame.item(19, "date"))

    assert target.select(LIQUIDITY_FACTORS).row(0) == (None, None)


@pytest.mark.parametrize(
    "invalid_turnover",
    [-0.01, float("nan"), float("inf"), -float("inf")],
)
def test_invalid_turnover_rate_nulls_the_complete_window_without_compressing_it(
    invalid_turnover: float,
) -> None:
    frame = _liquidity_panel(20).with_columns(
        pl.when(pl.int_range(pl.len()) == 3)
        .then(invalid_turnover)
        .otherwise(pl.col("turnover_rate"))
        .alias("turnover_rate")
    )

    target = _compute_liquidity(frame).row(19, named=True)

    assert target["turnover_20"] is None
    assert target["amihud_20"] == pytest.approx(0.01 / 1_000.0)


def test_liquidity_requires_a_complete_twenty_observation_window() -> None:
    target = _compute_liquidity(_liquidity_panel(19)).row(18, named=True)

    assert target["turnover_20"] is None
    assert target["amihud_20"] is None


@pytest.mark.parametrize(
    ("column", "invalid_value"),
    [
        ("amount", 0.0),
        ("amount", -1.0),
        ("amount", 5e-324),
        ("amount", float("nan")),
        ("amount", float("inf")),
        ("close_adj", 0.0),
        ("close_adj", -1.0),
        ("close_adj", float("nan")),
        ("close_adj", float("inf")),
        ("prev_close_adj", 0.0),
        ("prev_close_adj", -1.0),
        ("prev_close_adj", float("nan")),
        ("prev_close_adj", float("inf")),
        ("volume", 0.0),
        ("volume", -1.0),
    ],
)
def test_amihud_is_null_for_invalid_prices_or_trade_amount(
    column: str,
    invalid_value: float,
) -> None:
    frame = _liquidity_panel(20).with_columns(
        pl.when(pl.int_range(pl.len()) == 19)
        .then(invalid_value)
        .otherwise(pl.col(column))
        .alias(column)
    )

    actual = _compute_liquidity(frame)

    assert actual.row(19, named=True)["amihud_20"] is None
    finite_values = actual.get_column("amihud_20").drop_nulls()
    assert finite_values.is_finite().all()


def test_amihud_uses_adjusted_close_return_and_original_amount() -> None:
    frame = _liquidity_panel(20).with_columns(
        pl.lit(110.0).alias("close_adj"),
        pl.lit(100.0).alias("prev_close_adj"),
        pl.lit(200.0).alias("amount"),
        pl.lit(1_000_000.0).alias("total_market_cap"),
    )

    actual = _compute_liquidity(frame).row(19, named=True)

    assert actual["amihud_20"] == pytest.approx(abs(110.0 / 100.0 - 1.0) / 200.0)


def test_size_factor_only_logs_finite_strictly_positive_market_cap() -> None:
    market_caps = [100.0, 1.0, 0.0, -1.0, None, float("nan"), float("inf")]
    frame = pl.DataFrame(
        {
            "date": [date(2005, 1, 1) + timedelta(days=index) for index in range(len(market_caps))],
            "symbol": ["000001"] * len(market_caps),
            "total_market_cap": market_caps,
            "close_adj": [999.0] * len(market_caps),
            "total_shares": [999.0] * len(market_caps),
        }
    )

    actual = _compute_size(frame).get_column("log_market_cap").to_list()

    assert actual[:2] == pytest.approx([math.log(100.0), 0.0])
    assert actual[2:] == [None, None, None, None, None]


@pytest.mark.parametrize(
    ("compute", "frame", "factor_names"),
    [
        pytest.param(_compute_value, _value_panel(), VALUE_FACTORS, id="value"),
        pytest.param(_compute_liquidity, _liquidity_panel(20), LIQUIDITY_FACTORS, id="liquidity"),
        pytest.param(_compute_size, _size_panel(), SIZE_FACTORS, id="size"),
    ],
)
def test_registered_minimal_projection_is_sufficient_and_output_is_thin(
    compute: Callable[[pl.DataFrame], pl.DataFrame],
    frame: pl.DataFrame,
    factor_names: tuple[str, ...],
) -> None:
    projected = _registered_projection(frame, factor_names)

    try:
        actual = compute(projected)
    except pl.exceptions.ColumnNotFoundError as error:
        pytest.fail(f"registered source projection is incomplete: {error}")

    assert actual.columns == ["date", "symbol", *factor_names]


@pytest.mark.parametrize(
    ("compute", "frame"),
    [
        pytest.param(
            _compute_value,
            pl.concat([_value_panel(symbol="000002"), _value_panel(symbol="000001")]),
            id="value",
        ),
        pytest.param(
            _compute_liquidity,
            pl.concat(
                [
                    _liquidity_panel(20, symbol="000002"),
                    _liquidity_panel(20, symbol="000001"),
                ]
            ),
            id="liquidity",
        ),
        pytest.param(
            _compute_size,
            pl.concat([_size_panel(symbol="000002"), _size_panel(symbol="000001")]),
            id="size",
        ),
    ],
)
def test_cross_sectional_factor_results_are_stable_for_shuffled_multiple_symbols(
    compute: Callable[[pl.DataFrame], pl.DataFrame],
    frame: pl.DataFrame,
) -> None:
    expected = compute(frame)
    shuffled = frame.sample(fraction=1.0, shuffle=True, seed=42)

    assert_frame_equal(compute(shuffled), expected)
    assert expected.select("date", "symbol").equals(
        expected.select("date", "symbol").sort("date", "symbol")
    )


@pytest.mark.parametrize(
    ("compute", "frame"),
    [
        pytest.param(_compute_value, _value_panel(), id="value"),
        pytest.param(_compute_liquidity, _liquidity_panel(20), id="liquidity"),
        pytest.param(_compute_size, _size_panel(), id="size"),
    ],
)
def test_cross_sectional_factor_families_reject_duplicate_date_symbol_keys(
    compute: Callable[[pl.DataFrame], pl.DataFrame],
    frame: pl.DataFrame,
) -> None:
    duplicate = pl.concat([frame, frame.head(1)])

    with pytest.raises(ValueError, match="duplicate.*date.*symbol"):
        compute(duplicate)


def test_liquidity_registry_declares_exact_runtime_dependencies() -> None:
    definitions = {definition.name: definition for definition in FACTOR_DEFINITIONS}

    assert definitions["turnover_20"].source_columns == (
        "turnover_rate",
        "close_adj",
        "volume",
        "amount",
    )
    assert definitions["amihud_20"].source_columns == (
        "close_adj",
        "prev_close_adj",
        "volume",
        "amount",
    )


@pytest.mark.parametrize(
    ("module_name", "factor_names"),
    [
        ("value", VALUE_FACTORS),
        ("liquidity", LIQUIDITY_FACTORS),
        ("size", SIZE_FACTORS),
    ],
)
def test_runtime_source_columns_are_derived_from_the_registry(
    module_name: str,
    factor_names: tuple[str, ...],
) -> None:
    module = importlib.import_module(f"ashare_multifactor.factors.{module_name}")

    assert module._SOURCE_COLUMNS == factor_source_columns(factor_names)
