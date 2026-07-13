from __future__ import annotations

from dataclasses import dataclass
from datetime import date
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
    if config.mvp.portfolio_size > config.mvp.universe_size:
        raise ValueError("portfolio size exceeds universe size")
    return config
