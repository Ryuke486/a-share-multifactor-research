from dataclasses import FrozenInstanceError

import pytest

from ashare_multifactor.factors.definitions import FACTOR_DEFINITIONS


EXPECTED_REQUIREMENTS = {
    "ep_ttm": ("pe_ttm",),
    "bp": ("pb",),
    "sp_ttm": ("ps_ttm",),
    "momentum_60": ("close_adj",),
    "momentum_120": ("close_adj",),
    "momentum_12_1": ("close_adj",),
    "reversal_5": ("close_adj",),
    "reversal_20": ("close_adj",),
    "turnover_20": ("turnover",),
    "amihud_20": ("close_adj", "amount"),
    "volatility_20": ("close_adj",),
    "volatility_60": ("close_adj",),
    "downside_volatility_60": ("close_adj",),
    "log_market_cap": ("total_market_cap",),
}


def test_registry_contains_exactly_the_fourteen_preregistered_factors() -> None:
    names = [definition.name for definition in FACTOR_DEFINITIONS]

    assert len(names) == 14
    assert len(set(names)) == 14
    assert set(names) == set(EXPECTED_REQUIREMENTS)


def test_registry_freezes_direction_and_data_requirements() -> None:
    directions = {definition.name: definition.direction for definition in FACTOR_DEFINITIONS}
    requirements = {
        definition.name: definition.required_fields for definition in FACTOR_DEFINITIONS
    }

    assert set(directions.values()) <= {-1, 1}
    assert directions == {
        "ep_ttm": 1,
        "bp": 1,
        "sp_ttm": 1,
        "momentum_60": 1,
        "momentum_120": 1,
        "momentum_12_1": 1,
        "reversal_5": -1,
        "reversal_20": -1,
        "turnover_20": -1,
        "amihud_20": 1,
        "volatility_20": -1,
        "volatility_60": -1,
        "downside_volatility_60": -1,
        "log_market_cap": -1,
    }
    assert requirements == EXPECTED_REQUIREMENTS


def test_valuation_requires_verified_point_in_time_evidence() -> None:
    by_name = {definition.name: definition for definition in FACTOR_DEFINITIONS}

    assert all(
        by_name[name].requires_verified_pit for name in ("ep_ttm", "bp", "sp_ttm")
    )
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
