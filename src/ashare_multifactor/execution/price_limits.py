from decimal import Decimal, ROUND_HALF_UP


_TICK = Decimal("0.01")


def _price(value: Decimal) -> float:
    return float(value.quantize(_TICK, rounding=ROUND_HALF_UP))


def limit_prices(previous_close: float, limit_rate: float) -> tuple[float, float]:
    if previous_close <= 0 or not 0 < limit_rate < 1:
        raise ValueError("previous_close and limit_rate must be positive")
    close = Decimal(str(previous_close))
    rate = Decimal(str(limit_rate))
    return _price(close * (1 - rate)), _price(close * (1 + rate))

