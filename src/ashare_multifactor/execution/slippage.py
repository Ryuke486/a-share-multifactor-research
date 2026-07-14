from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import math


@dataclass(frozen=True)
class ExecutionPrice:
    price: float
    fixed_slippage_bps: float
    impact_bps: float


def execution_price(
    *,
    open_price: float,
    side: str,
    order_amount: float,
    adv20: float,
    fixed_bps: float,
    reference_impact_bps: float,
    reference_participation: float,
    maximum_impact_bps: float,
) -> ExecutionPrice:
    if side not in {"buy", "sell"} or open_price <= 0 or adv20 <= 0:
        raise ValueError("invalid execution price input")
    participation = max(order_amount, 0.0) / adv20
    impact = min(
        maximum_impact_bps,
        reference_impact_bps * math.sqrt(participation / reference_participation),
    )
    total_bps = fixed_bps + impact
    direction = 1 if side == "buy" else -1
    raw_price = open_price * (1 + direction * total_bps / 10_000)
    price = float(Decimal(str(raw_price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return ExecutionPrice(price, fixed_bps, impact)
