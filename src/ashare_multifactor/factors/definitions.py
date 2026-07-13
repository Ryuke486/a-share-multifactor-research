"""Single authoritative registry for stage-four factor metadata."""

from dataclasses import dataclass
from typing import Final, Sequence


@dataclass(frozen=True)
class FactorDefinition:
    name: str
    family: str
    direction: int
    required_fields: tuple[str, ...]
    lookback: int | None = None
    skip_recent: int = 0
    requires_verified_pit: bool = False
    size_neutralize: bool = True


FACTOR_DEFINITIONS: Final[Sequence[FactorDefinition]] = (
    FactorDefinition("ep_ttm", "value", 1, ("pe_ttm",), requires_verified_pit=True),
    FactorDefinition("bp", "value", 1, ("pb",), requires_verified_pit=True),
    FactorDefinition("sp_ttm", "value", 1, ("ps_ttm",), requires_verified_pit=True),
    FactorDefinition("momentum_60", "momentum", 1, ("close_adj",), lookback=60),
    FactorDefinition("momentum_120", "momentum", 1, ("close_adj",), lookback=120),
    FactorDefinition(
        "momentum_12_1",
        "momentum",
        1,
        ("close_adj",),
        lookback=252,
        skip_recent=21,
    ),
    FactorDefinition("reversal_5", "reversal", -1, ("close_adj",), lookback=5),
    FactorDefinition("reversal_20", "reversal", -1, ("close_adj",), lookback=20),
    FactorDefinition("turnover_20", "liquidity", -1, ("turnover",), lookback=20),
    FactorDefinition(
        "amihud_20",
        "liquidity",
        1,
        ("close_adj", "amount"),
        lookback=20,
    ),
    FactorDefinition("volatility_20", "low_volatility", -1, ("close_adj",), lookback=20),
    FactorDefinition("volatility_60", "low_volatility", -1, ("close_adj",), lookback=60),
    FactorDefinition(
        "downside_volatility_60",
        "low_volatility",
        -1,
        ("close_adj",),
        lookback=60,
    ),
    FactorDefinition(
        "log_market_cap",
        "size",
        -1,
        ("total_market_cap",),
        size_neutralize=False,
    ),
)
