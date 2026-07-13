"""Pure single-rebalance execution for the MVP backtest."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RebalanceExecution:
    cash: float
    positions: dict[str, float]
    trades: list[dict[str, Any]]
    blocked_orders: list[dict[str, Any]]


def execute_rebalance(
    *,
    signal_date: Any,
    execution_date: Any,
    execution_prices: dict[str, float | None],
    positions: dict[str, float],
    current_values: dict[str, float],
    weights: dict[str, float],
    opening_equity: float,
    cash: float,
    cost_rate: float,
) -> RebalanceExecution:
    """Execute tradable adjustments without replacing missing targets."""
    updated_positions = positions.copy()
    blocked_orders = _blocked_order_records(
        signal_date,
        execution_date,
        positions,
        weights,
        execution_prices,
    )
    tradable_symbols = {symbol for symbol, price in execution_prices.items() if price is not None}
    tradable_weights = {
        symbol: weight for symbol, weight in weights.items() if symbol in tradable_symbols
    }
    frozen_value = sum(
        current_values.get(symbol, 0.0)
        for symbol, price in execution_prices.items()
        if price is None
    )
    allocation = _maximum_feasible_allocation(
        opening_equity,
        frozen_value,
        current_values,
        tradable_symbols,
        tradable_weights,
        cost_rate,
    )
    target_values = {symbol: allocation * weight for symbol, weight in tradable_weights.items()}
    changes = {
        symbol: target_values.get(symbol, 0.0) - current_values.get(symbol, 0.0)
        for symbol in tradable_symbols
    }

    trades: list[dict[str, Any]] = []
    for symbol in sorted(symbol for symbol, change in changes.items() if change < -1e-10):
        gross = -changes[symbol]
        price = execution_prices[symbol]
        assert price is not None
        trade_units = -gross / price
        remaining_units = updated_positions[symbol] + trade_units
        if remaining_units <= 1e-10:
            updated_positions.pop(symbol)
        else:
            updated_positions[symbol] = remaining_units
        cost = gross * cost_rate
        cash += gross - cost
        trades.append(
            _trade_record(
                signal_date,
                execution_date,
                symbol,
                "sell",
                price,
                trade_units,
                gross,
                cost,
                weights.get(symbol, 0.0),
            )
        )

    intended_buys = {symbol: change for symbol, change in changes.items() if change > 1e-10}
    for symbol in sorted(intended_buys):
        gross = intended_buys[symbol]
        price = execution_prices[symbol]
        assert price is not None
        trade_units = gross / price
        updated_positions[symbol] = updated_positions.get(symbol, 0.0) + trade_units
        cost = gross * cost_rate
        cash -= gross + cost
        trades.append(
            _trade_record(
                signal_date,
                execution_date,
                symbol,
                "buy",
                price,
                trade_units,
                gross,
                cost,
                weights.get(symbol, 0.0),
            )
        )

    if cash < 0.0 and abs(cash) <= 1e-8:
        cash = 0.0
    return RebalanceExecution(cash, updated_positions, trades, blocked_orders)


def _maximum_feasible_allocation(
    opening_equity: float,
    frozen_value: float,
    current_values: dict[str, float],
    tradable_symbols: set[str],
    tradable_weights: dict[str, float],
    cost_rate: float,
) -> float:
    upper = opening_equity

    def ending_cash(allocation: float) -> float:
        target_values = {symbol: allocation * weight for symbol, weight in tradable_weights.items()}
        gross = sum(
            abs(target_values.get(symbol, 0.0) - current_values.get(symbol, 0.0))
            for symbol in tradable_symbols
        )
        return opening_equity - frozen_value - sum(target_values.values()) - gross * cost_rate

    if ending_cash(upper) >= 0.0:
        return upper

    low = 0.0
    high = upper
    for _ in range(80):
        midpoint = (low + high) / 2.0
        if ending_cash(midpoint) >= 0.0:
            low = midpoint
        else:
            high = midpoint
    return low


def _blocked_order_records(
    signal_date: Any,
    execution_date: Any,
    positions: dict[str, float],
    weights: dict[str, float],
    execution_prices: dict[str, float | None],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for symbol in sorted(execution_prices):
        if execution_prices[symbol] is not None:
            continue
        is_held = symbol in positions
        is_targeted = symbol in weights
        side = "rebalance" if is_held and is_targeted else "sell" if is_held else "buy"
        records.append(
            {
                "signal_date": signal_date,
                "execution_date": execution_date,
                "symbol": symbol,
                "side": side,
                "reason": "missing_open_price",
                "target_weight": weights.get(symbol, 0.0),
                "units_held": positions.get(symbol, 0.0),
            }
        )
    return records


def _trade_record(
    signal_date: Any,
    execution_date: Any,
    symbol: str,
    side: str,
    price: float,
    trade_units: float,
    gross: float,
    cost: float,
    target_weight: float,
) -> dict[str, Any]:
    return {
        "signal_date": signal_date,
        "execution_date": execution_date,
        "symbol": symbol,
        "side": side,
        "price": price,
        "trade_units": trade_units,
        "gross_amount": gross,
        "cost": cost,
        "target_weight": target_weight,
    }
