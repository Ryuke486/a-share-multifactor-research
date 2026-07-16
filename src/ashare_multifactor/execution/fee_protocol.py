from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import math

from ashare_multifactor.execution.fees import FeeSchedule


@dataclass(frozen=True)
class FeeProtocolEvidence:
    coverage_start: date
    coverage_end: date
    markets: tuple[str, ...]
    checked_days: int


def validate_fee_protocol(
    schedule: FeeSchedule,
    start: date,
    end: date,
    *,
    markets: tuple[str, ...] = ("sh", "sz"),
) -> FeeProtocolEvidence:
    """Prove that every calendar day has one finite non-negative fee rule."""
    if start > end:
        raise ValueError("fee protocol start must not follow end")
    if len(markets) != 2 or set(markets) != {"sh", "sz"}:
        raise ValueError("formal fee protocol must cover both sh and sz")

    checked = 0
    current = start
    while current <= end:
        for side in ("buy", "sell"):
            rate = schedule.stamp_duty_rate(current, side)
            if not math.isfinite(rate) or rate < 0:
                raise ValueError("stamp-duty rate must be finite and non-negative")
        for market in markets:
            fee = schedule.transfer_fee(current, market, 10_000.0, 1_000)
            if not math.isfinite(fee) or fee < 0:
                raise ValueError("transfer fee must be finite and non-negative")
        checked += 1
        current += timedelta(days=1)

    return FeeProtocolEvidence(start, end, markets, checked)
