import math


def target_quantity(
    target_amount: float,
    estimated_price: float,
    *,
    current_quantity: int,
    lot_size: int = 100,
) -> int:
    if target_amount < 0 or estimated_price <= 0 or current_quantity < 0:
        raise ValueError("invalid sizing input")
    if target_amount == 0:
        return 0
    return math.floor(target_amount / estimated_price / lot_size) * lot_size


def proportional_buy_quantities(
    requests: dict[str, tuple[int, float]],
    *,
    available_cash: float,
    lot_size: int = 100,
) -> dict[str, int]:
    if available_cash < 0 or lot_size <= 0:
        raise ValueError("invalid proportional sizing input")
    if any(quantity < 0 or price <= 0 for quantity, price in requests.values()):
        raise ValueError("invalid buy request")
    requested_value = sum(quantity * price for quantity, price in requests.values())
    if requested_value <= available_cash:
        return {symbol: quantity for symbol, (quantity, _) in sorted(requests.items())}
    scale = available_cash / requested_value if requested_value else 0.0
    allocated = {
        symbol: math.floor(quantity * scale / lot_size) * lot_size
        for symbol, (quantity, _) in requests.items()
    }
    remaining_cash = available_cash - sum(
        allocated[symbol] * price for symbol, (_, price) in requests.items()
    )
    candidates = sorted(
        requests,
        key=lambda symbol: (
            -(requests[symbol][0] - allocated[symbol]) * requests[symbol][1],
            symbol,
        ),
    )
    for symbol in candidates:
        requested, price = requests[symbol]
        if allocated[symbol] + lot_size <= requested and price * lot_size <= remaining_cash:
            allocated[symbol] += lot_size
            remaining_cash -= price * lot_size
    return {symbol: allocated[symbol] for symbol in sorted(allocated)}
