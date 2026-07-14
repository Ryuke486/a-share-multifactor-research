from __future__ import annotations

from typing import Final


CANDIDATE_FACTORS: Final = {
    "reversal": ("reversal_5", "reversal_20"),
    "liquidity": ("turnover_20", "amihud_20"),
    "low_volatility": ("volatility_20", "volatility_60"),
}
REPRESENTATIVE_FACTORS: Final = (
    "reversal_20",
    "turnover_20",
    "volatility_60",
)
METHODS: Final = (
    "candidate_equal",
    "family_equal",
    "representative_equal",
    "rolling_ic_family",
)
