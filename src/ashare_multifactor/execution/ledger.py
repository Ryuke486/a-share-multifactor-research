from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from datetime import date
import math


@dataclass
class _Lot:
    quantity: int
    available_date: date


@dataclass(frozen=True)
class LedgerSnapshot:
    date: date
    cash: float
    receivables: float
    holdings_value: float
    nav: float


class Ledger:
    def __init__(self, *, initial_cash: float) -> None:
        if not math.isfinite(initial_cash) or initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        self.cash = initial_cash
        self._lots: dict[str, list[_Lot]] = {}
        self._dividend_receivables: dict[str, float] = {}

    def quantity(self, symbol: str) -> int:
        return sum(lot.quantity for lot in self._lots.get(symbol, []))

    @property
    def receivables(self) -> float:
        return sum(self._dividend_receivables.values())

    def available_quantity(self, symbol: str, trade_date: date) -> int:
        return sum(
            lot.quantity
            for lot in self._lots.get(symbol, [])
            if lot.available_date <= trade_date
        )

    def buy(
        self,
        symbol: str,
        quantity: int,
        price: float,
        fees: float,
        trade_date: date,
        available_date: date,
    ) -> None:
        del trade_date
        required = quantity * price + fees
        if quantity <= 0 or required > self.cash + 1e-8:
            raise ValueError("buy exceeds available cash")
        self.cash -= required
        self._lots.setdefault(symbol, []).append(_Lot(quantity, available_date))

    def sell(
        self,
        symbol: str,
        quantity: int,
        price: float,
        fees: float,
        trade_date: date,
    ) -> None:
        if quantity <= 0 or quantity > self.available_quantity(symbol, trade_date):
            raise ValueError("sell exceeds available quantity")
        remaining = quantity
        for lot in self._lots.get(symbol, []):
            if lot.available_date <= trade_date and remaining:
                sold = min(lot.quantity, remaining)
                lot.quantity -= sold
                remaining -= sold
        self._lots[symbol] = [lot for lot in self._lots[symbol] if lot.quantity]
        self.cash += quantity * price - fees

    def apply_action(
        self,
        symbol: str,
        cash_per_share: float,
        share_ratio: float,
        effective_date: date,
    ) -> None:
        quantity = self.quantity(symbol)
        self.cash += quantity * cash_per_share
        bonus = int(
            (Decimal(quantity) * Decimal(str(share_ratio))).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        if bonus:
            self._lots.setdefault(symbol, []).append(_Lot(bonus, effective_date))

    def recognize_dividend(
        self, action_id: str, symbol: str, cash_per_share: float
    ) -> float:
        if action_id in self._dividend_receivables:
            raise ValueError(f"dividend already recognized: {action_id}")
        if cash_per_share < 0 or not math.isfinite(cash_per_share):
            raise ValueError("cash_per_share must be finite and non-negative")
        amount = self.quantity(symbol) * cash_per_share
        self._dividend_receivables[action_id] = amount
        return amount

    def pay_dividend(self, action_id: str) -> float:
        if action_id not in self._dividend_receivables:
            raise ValueError(f"dividend receivable not found: {action_id}")
        amount = self._dividend_receivables.pop(action_id)
        self.cash += amount
        return amount

    def cash_exit(self, symbol: str, cash_per_share: float) -> float:
        quantity = self.quantity(symbol)
        proceeds = quantity * cash_per_share
        self.cash += proceeds
        self._lots.pop(symbol, None)
        return proceeds

    def exchange_security(
        self,
        source_symbol: str,
        target_symbol: str,
        ratio: float,
        effective_date: date,
    ) -> int:
        if ratio <= 0 or source_symbol == target_symbol:
            raise ValueError("invalid security exchange")
        quantity = self.quantity(source_symbol)
        converted = round(quantity * ratio)
        self._lots.pop(source_symbol, None)
        if converted:
            self._lots.setdefault(target_symbol, []).append(_Lot(converted, effective_date))
        return converted

    def value(self, value_date: date, close_prices: dict[str, float]) -> LedgerSnapshot:
        missing = set(self._lots) - set(close_prices)
        if missing:
            raise ValueError(f"missing valuation prices: {sorted(missing)}")
        holdings = sum(self.quantity(symbol) * close_prices[symbol] for symbol in self._lots)
        receivables = self.receivables
        nav = self.cash + receivables + holdings
        if self.cash < -1e-8 or not math.isfinite(nav):
            raise ValueError("ledger invariant failed")
        return LedgerSnapshot(value_date, self.cash, receivables, holdings, nav)
