from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Period:
    start: date
    end: date

    def overlaps(self, other: Period) -> bool:
        return max(self.start, other.start) <= min(self.end, other.end)

    def contains(self, other: Period) -> bool:
        return self.start <= other.start and other.end <= self.end


@dataclass(frozen=True)
class Paths:
    raw_unadjusted: Path
    raw_backward_adjusted: Path
    processed: Path
    artifacts: Path


@dataclass(frozen=True)
class MvpSettings:
    universe_size: int
    momentum_lookback: int
    forward_horizon: int
    portfolio_size: int
    transaction_cost_bps: float
    initial_cash: float


@dataclass(frozen=True)
class ResearchConfig:
    paths: Paths
    research: Period
    validation: Period
    test: Period
    smoke_data: Period
    smoke_analysis: Period
    mvp: MvpSettings


def _as_date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _period(value: list[str | date]) -> Period:
    if len(value) != 2:
        raise ValueError("period must contain exactly two dates")
    period = Period(_as_date(value[0]), _as_date(value[1]))
    if period.end < period.start:
        raise ValueError("period end precedes start")
    return period


def _validate_mvp(settings: MvpSettings) -> None:
    positive_integer_fields = (
        "universe_size",
        "momentum_lookback",
        "forward_horizon",
        "portfolio_size",
    )
    for field in positive_integer_fields:
        value = getattr(settings, field)
        if type(value) is not int or value <= 0:
            raise ValueError(f"mvp.{field} must be a positive integer")

    initial_cash = settings.initial_cash
    if (
        isinstance(initial_cash, bool)
        or not isinstance(initial_cash, (int, float))
        or not math.isfinite(initial_cash)
        or initial_cash <= 0
    ):
        raise ValueError("mvp.initial_cash must be positive and finite")

    transaction_cost_bps = settings.transaction_cost_bps
    if (
        isinstance(transaction_cost_bps, bool)
        or not isinstance(transaction_cost_bps, (int, float))
        or not math.isfinite(transaction_cost_bps)
        or transaction_cost_bps < 0
    ):
        raise ValueError("mvp.transaction_cost_bps must be non-negative and finite")


def load_config(path: Path) -> ResearchConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    periods = raw["periods"]
    config = ResearchConfig(
        paths=Paths(**{key: Path(value) for key, value in raw["paths"].items()}),
        research=_period(periods["research"]),
        validation=_period(periods["validation"]),
        test=_period(periods["test"]),
        smoke_data=_period(periods["smoke_data"]),
        smoke_analysis=_period(periods["smoke_analysis"]),
        mvp=MvpSettings(**raw["mvp"]),
    )
    if config.research.overlaps(config.validation):
        raise ValueError("research and validation periods overlap")
    if config.validation.overlaps(config.test):
        raise ValueError("validation and test periods overlap")
    if config.research.overlaps(config.test):
        raise ValueError("research and test periods overlap")
    if config.smoke_data.overlaps(config.test) or config.smoke_analysis.overlaps(config.test):
        raise ValueError("smoke periods must not overlap test period")
    if not config.research.contains(config.smoke_data):
        raise ValueError("research period must contain smoke data period")
    if not config.smoke_data.contains(config.smoke_analysis):
        raise ValueError("smoke data period must contain smoke analysis period")
    _validate_mvp(config.mvp)
    if config.mvp.portfolio_size > config.mvp.universe_size:
        raise ValueError("portfolio size exceeds universe size")
    return config
