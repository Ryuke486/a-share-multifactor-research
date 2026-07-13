from dataclasses import FrozenInstanceError, fields

import pytest

from ashare_multifactor.factors.definitions import FACTOR_DEFINITIONS, FactorDefinition


EXPECTED_DEFINITIONS = (
    ("ep_ttm", "value", ("pe_ttm",), 0, 1, True, True),
    ("bp", "value", ("pb",), 0, 1, True, True),
    ("sp_ttm", "value", ("ps_ttm",), 0, 1, True, True),
    (
        "momentum_60",
        "momentum",
        ("close_adj", "volume", "amount"),
        60,
        1,
        False,
        True,
    ),
    (
        "momentum_120",
        "momentum",
        ("close_adj", "volume", "amount"),
        120,
        1,
        False,
        True,
    ),
    (
        "momentum_12_1",
        "momentum",
        ("close_adj", "volume", "amount"),
        252,
        1,
        False,
        True,
    ),
    (
        "reversal_5",
        "reversal",
        ("close_adj", "volume", "amount"),
        5,
        -1,
        False,
        True,
    ),
    (
        "reversal_20",
        "reversal",
        ("close_adj", "volume", "amount"),
        20,
        -1,
        False,
        True,
    ),
    (
        "turnover_20",
        "liquidity",
        ("turnover", "close_adj", "volume", "amount"),
        20,
        -1,
        False,
        True,
    ),
    (
        "amihud_20",
        "liquidity",
        ("close_adj", "prev_close_adj", "volume", "amount"),
        20,
        1,
        False,
        True,
    ),
    (
        "volatility_20",
        "low_volatility",
        ("close_adj", "prev_close_adj", "volume", "amount"),
        20,
        -1,
        False,
        True,
    ),
    (
        "volatility_60",
        "low_volatility",
        ("close_adj", "prev_close_adj", "volume", "amount"),
        60,
        -1,
        False,
        True,
    ),
    (
        "downside_volatility_60",
        "low_volatility",
        ("close_adj", "prev_close_adj", "volume", "amount"),
        60,
        -1,
        False,
        True,
    ),
    ("log_market_cap", "size", ("total_market_cap",), 0, -1, False, False),
)


def test_factor_definition_has_exact_frozen_contract() -> None:
    assert tuple(field.name for field in fields(FactorDefinition)) == (
        "name",
        "family",
        "source_columns",
        "lookback",
        "direction",
        "requires_verified_pit",
        "size_neutralize",
    )


def test_registry_contains_exactly_the_fourteen_preregistered_factors() -> None:
    names = [definition.name for definition in FACTOR_DEFINITIONS]

    assert len(names) == 14
    assert len(set(names)) == 14
    assert names == [definition[0] for definition in EXPECTED_DEFINITIONS]


def test_registry_freezes_complete_preregistered_metadata_matrix() -> None:
    actual = tuple(
        (
            definition.name,
            definition.family,
            definition.source_columns,
            definition.lookback,
            definition.direction,
            definition.requires_verified_pit,
            definition.size_neutralize,
        )
        for definition in FACTOR_DEFINITIONS
    )

    assert actual == EXPECTED_DEFINITIONS
    assert all(isinstance(definition.lookback, int) for definition in FACTOR_DEFINITIONS)
    assert set(definition.direction for definition in FACTOR_DEFINITIONS) <= {-1, 1}


def test_valuation_requires_verified_point_in_time_evidence() -> None:
    by_name = {definition.name: definition for definition in FACTOR_DEFINITIONS}

    assert all(by_name[name].requires_verified_pit for name in ("ep_ttm", "bp", "sp_ttm"))
    assert not any(
        definition.requires_verified_pit
        for definition in FACTOR_DEFINITIONS
        if definition.name not in {"ep_ttm", "bp", "sp_ttm"}
    )


def test_size_factor_is_not_neutralized_against_itself() -> None:
    by_name = {definition.name: definition for definition in FACTOR_DEFINITIONS}

    assert by_name["log_market_cap"].size_neutralize is False
    assert all(
        definition.size_neutralize
        for definition in FACTOR_DEFINITIONS
        if definition.name != "log_market_cap"
    )


def test_factor_definitions_are_immutable() -> None:
    with pytest.raises(FrozenInstanceError):
        FACTOR_DEFINITIONS[0].direction = -1
