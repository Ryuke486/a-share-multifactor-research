from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
from pathlib import Path

import yaml


@dataclass(frozen=True)
class FeeBreakdown:
    commission: float
    stamp_duty: float
    transfer_fee: float

    @property
    def total(self) -> float:
        return self.commission + self.stamp_duty + self.transfer_fee


@dataclass(frozen=True)
class _DatedRate:
    start: date
    end: date
    buy_rate: float
    sell_rate: float

    def matches(self, value: date) -> bool:
        return self.start <= value <= self.end


@dataclass(frozen=True)
class _TransferRule:
    start: date
    end: date
    market: str
    basis: str
    rate: float
    minimum: float

    def matches(self, value: date, market: str) -> bool:
        return self.market == market and self.start <= value <= self.end


@dataclass(frozen=True)
class FeeSchedule:
    stamp_duty: tuple[_DatedRate, ...]
    transfer_fees: tuple[_TransferRule, ...]
    commission_rate: float
    minimum_commission: float

    def stamp_duty_rate(self, trade_date: date, side: str) -> float:
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        matches = [rule for rule in self.stamp_duty if rule.matches(trade_date)]
        if len(matches) != 1:
            raise ValueError(f"no unique stamp duty rule for {trade_date}")
        return matches[0].buy_rate if side == "buy" else matches[0].sell_rate

    def transfer_fee(
        self, trade_date: date, market: str, amount: float, quantity: int
    ) -> float:
        if market not in {"sh", "sz"}:
            raise ValueError(f"unknown market: {market}")
        matches = [
            rule for rule in self.transfer_fees if rule.matches(trade_date, market)
        ]
        if len(matches) != 1:
            raise ValueError(f"no unique transfer fee rule for {trade_date} {market}")
        rule = matches[0]
        basis = amount if rule.basis == "amount" else quantity
        return max(rule.minimum, basis * rule.rate)

    def calculate(
        self,
        *,
        trade_date: date,
        market: str,
        side: str,
        price: float,
        quantity: int,
    ) -> FeeBreakdown:
        if not math.isfinite(price) or price <= 0 or quantity <= 0:
            raise ValueError("price and quantity must be positive")
        amount = price * quantity
        commission = max(self.minimum_commission, amount * self.commission_rate)
        stamp = amount * self.stamp_duty_rate(trade_date, side)
        transfer = self.transfer_fee(trade_date, market, amount, quantity)
        return FeeBreakdown(commission, stamp, transfer)


def _parse_date(value: object) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _validate_contiguous(rules: list[_DatedRate], start: date, end: date) -> None:
    ordered = sorted(rules, key=lambda rule: rule.start)
    cursor = start
    for rule in ordered:
        if rule.start != cursor or rule.end < rule.start:
            raise ValueError("fee intervals contain a gap, overlap, or invalid range")
        cursor = date.fromordinal(rule.end.toordinal() + 1)
    if cursor != date.fromordinal(end.toordinal() + 1):
        raise ValueError("fee intervals do not cover the research period")


def load_market_rules(path: Path) -> FeeSchedule:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    period_start, period_end = map(_parse_date, raw["research_period"])
    stamp = [
        _DatedRate(
            _parse_date(item["start"]),
            _parse_date(item["end"]),
            float(item["buy_rate"]),
            float(item["sell_rate"]),
        )
        for item in raw["stamp_duty"]
    ]
    _validate_contiguous(stamp, period_start, period_end)
    transfer = [
        _TransferRule(
            _parse_date(item["start"]),
            _parse_date(item["end"]),
            item["market"],
            item["basis"],
            float(item["rate"]),
            float(item.get("minimum", 0.0)),
        )
        for item in raw["transfer_fee"]
    ]
    if any(
        item.rate < 0
        or item.minimum < 0
        or item.market not in {"sh", "sz"}
        or item.basis not in {"amount", "shares"}
        for item in transfer
    ):
        raise ValueError("invalid transfer fee rule")
    for market in ("sh", "sz"):
        market_rules = [item for item in transfer if item.market == market]
        _validate_contiguous(
            [
                _DatedRate(item.start, item.end, item.rate, item.rate)
                for item in market_rules
            ],
            period_start,
            period_end,
        )
    assumptions = raw["model_assumptions"]
    return FeeSchedule(
        tuple(stamp),
        tuple(transfer),
        float(assumptions["commission_rate"]),
        float(assumptions["minimum_commission"]),
    )

