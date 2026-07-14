from __future__ import annotations

from typing import Protocol

from .sizing import target_quantity


class PositionLedger(Protocol):
    cash: float
    receivables: float
    _lots: dict[str, object]

    def quantity(self, symbol: str) -> int: ...


def build_target_order_specs(
    ledger: PositionLedger,
    weights: dict[str, float],
    market: dict[str, dict[str, object]],
    last_close: dict[str, float],
    *,
    symbols: set[str] | None = None,
) -> list[tuple[int, str, str, int, float]]:
    """Build target gaps from post-action holdings and pre-open information only."""
    holdings = set(ledger._lots)
    selected = sorted(symbols if symbols is not None else set(weights) | holdings)
    preopen_value = ledger.cash + ledger.receivables + sum(
        ledger.quantity(symbol)
        * float(market.get(symbol, {}).get("open_raw") or last_close.get(symbol, 0.0))
        for symbol in holdings
    )
    specs: list[tuple[int, str, str, int, float]] = []
    for symbol in selected:
        row = market.get(symbol)
        estimate = (
            float(row["open_raw"])
            if row and row.get("open_raw") is not None
            else last_close.get(symbol)
        )
        if not estimate:
            continue
        current = ledger.quantity(symbol)
        target = target_quantity(
            preopen_value * weights.get(symbol, 0.0),
            estimate,
            current_quantity=current,
        )
        difference = target - current
        if difference:
            side = "buy" if difference > 0 else "sell"
            specs.append((0 if side == "sell" else 1, symbol, side, abs(difference), estimate))
    return specs
