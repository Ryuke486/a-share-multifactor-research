"""Single authoritative registry for stage-four factor metadata."""

from dataclasses import dataclass
from typing import Final, Sequence


@dataclass(frozen=True)
class FactorDefinition:
    name: str
    family: str
    source_columns: tuple[str, ...]
    lookback: int
    direction: int
    requires_verified_pit: bool = False
    size_neutralize: bool = True


FACTOR_DEFINITIONS: Final[Sequence[FactorDefinition]] = (
    FactorDefinition("ep_ttm", "value", ("pe_ttm",), 0, 1, requires_verified_pit=True),
    FactorDefinition("bp", "value", ("pb",), 0, 1, requires_verified_pit=True),
    FactorDefinition("sp_ttm", "value", ("ps_ttm",), 0, 1, requires_verified_pit=True),
    FactorDefinition("momentum_60", "momentum", ("close_adj",), 60, 1),
    FactorDefinition("momentum_120", "momentum", ("close_adj",), 120, 1),
    FactorDefinition(
        "momentum_12_1",
        "momentum",
        ("close_adj",),
        252,
        1,
    ),
    FactorDefinition("reversal_5", "reversal", ("close_adj",), 5, -1),
    FactorDefinition("reversal_20", "reversal", ("close_adj",), 20, -1),
    FactorDefinition("turnover_20", "liquidity", ("turnover",), 20, -1),
    FactorDefinition(
        "amihud_20",
        "liquidity",
        ("close_adj", "amount"),
        20,
        1,
    ),
    FactorDefinition("volatility_20", "low_volatility", ("close_adj",), 20, -1),
    FactorDefinition("volatility_60", "low_volatility", ("close_adj",), 60, -1),
    FactorDefinition(
        "downside_volatility_60",
        "low_volatility",
        ("close_adj",),
        60,
        -1,
    ),
    FactorDefinition(
        "log_market_cap",
        "size",
        ("total_market_cap",),
        0,
        -1,
        size_neutralize=False,
    ),
)
