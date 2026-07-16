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
class FactorResearchSettings:
    data_start: date
    analysis_start: date
    analysis_end: date
    universe_size: int
    minimum_history: int
    liquidity_lookback: int
    require_valid_trade_observation: bool
    signal_frequency: str
    forward_horizons: tuple[int, ...]
    primary_horizon: int
    winsor_lower: float
    winsor_upper: float
    quantile_count: int
    minimum_coverage: float
    minimum_valid_months: int
    fdr_q_threshold: float
    redundancy_threshold: float
    supported_markets: tuple[str, ...] = ("sh", "sz")


@dataclass(frozen=True)
class FactorCombinationSettings:
    analysis_start: date
    analysis_end: date
    methods: tuple[str, ...]
    primary_method: str
    minimum_families: int
    ic_window_months: int
    ic_minimum_months: int
    ic_shrinkage: float


@dataclass(frozen=True)
class PortfolioConstructionSettings:
    portfolio_size: int
    size_groups: int
    per_group: int
    buffer_rank: int
    minimum_coverage: float
    turnover_warning: float


@dataclass(frozen=True)
class FormalBacktestSettings:
    analysis_start: date
    analysis_end: date
    initial_cash: float
    maximum_participation: float
    fixed_slippage_bps: float
    reference_impact_bps: float
    reference_participation: float
    maximum_impact_bps: float
    commission_rate: float
    minimum_commission: float
    buy_lot_size: int
    adv_lookback: int
    shadow_divergence_threshold: float
    stale_review_days: int


@dataclass(frozen=True)
class ValidationEvaluationSettings:
    data_start: date
    analysis_start: date
    analysis_end: date
    primary_metric: str
    secondary_metrics: tuple[str, ...]
    candidates: tuple[str, ...]
    default_candidate: str
    minimum_return_improvement: float
    maximum_drawdown_deterioration: float
    maximum_turnover_increase: float
    approved_stage_six_releases: tuple[str, ...]


@dataclass(frozen=True)
class ResearchConfig:
    paths: Paths
    research: Period
    validation: Period
    test: Period
    smoke_data: Period
    smoke_analysis: Period
    mvp: MvpSettings
    factor_research: FactorResearchSettings | None = None
    factor_combination: FactorCombinationSettings | None = None
    portfolio_construction: PortfolioConstructionSettings | None = None
    formal_backtest: FormalBacktestSettings | None = None
    validation_evaluation: ValidationEvaluationSettings | None = None
    supported_markets: tuple[str, ...] = ("sh", "sz")


def _as_date(value: object) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ValueError("date value must be an ISO date string")


def _period(value: list[str | date]) -> Period:
    if len(value) != 2:
        raise ValueError("period must contain exactly two dates")
    period = Period(_as_date(value[0]), _as_date(value[1]))
    if period.end < period.start:
        raise ValueError("period end precedes start")
    return period


def _factor_research(
    value: dict[str, object], *, supported_markets: tuple[str, ...]
) -> FactorResearchSettings:
    horizons = value["forward_horizons"]
    if not isinstance(horizons, (list, tuple)):
        raise ValueError("factor_research.forward_horizons must be a non-empty sequence")
    return FactorResearchSettings(
        data_start=_as_date(value["data_start"]),
        analysis_start=_as_date(value["analysis_start"]),
        analysis_end=_as_date(value["analysis_end"]),
        universe_size=value["universe_size"],
        minimum_history=value["minimum_history"],
        liquidity_lookback=value["liquidity_lookback"],
        require_valid_trade_observation=value["require_valid_trade_observation"],
        signal_frequency=value["signal_frequency"],
        forward_horizons=tuple(horizons),
        primary_horizon=value["primary_horizon"],
        winsor_lower=value["winsor_lower"],
        winsor_upper=value["winsor_upper"],
        quantile_count=value["quantile_count"],
        minimum_coverage=value["minimum_coverage"],
        minimum_valid_months=value["minimum_valid_months"],
        fdr_q_threshold=value["fdr_q_threshold"],
        redundancy_threshold=value["redundancy_threshold"],
        supported_markets=supported_markets,
    )


def _factor_combination(value: dict[str, object]) -> FactorCombinationSettings:
    methods = value["methods"]
    if not isinstance(methods, (list, tuple)):
        raise ValueError("factor_combination.methods must be a sequence")
    return FactorCombinationSettings(
        analysis_start=_as_date(value["analysis_start"]),
        analysis_end=_as_date(value["analysis_end"]),
        methods=tuple(methods),
        primary_method=value["primary_method"],
        minimum_families=value["minimum_families"],
        ic_window_months=value["ic_window_months"],
        ic_minimum_months=value["ic_minimum_months"],
        ic_shrinkage=value["ic_shrinkage"],
    )


def _validation_evaluation(value: dict[str, object]) -> ValidationEvaluationSettings:
    return ValidationEvaluationSettings(
        data_start=_as_date(value["data_start"]),
        analysis_start=_as_date(value["analysis_start"]),
        analysis_end=_as_date(value["analysis_end"]),
        primary_metric=value["primary_metric"],
        secondary_metrics=tuple(value["secondary_metrics"]),
        candidates=tuple(value["candidates"]),
        default_candidate=value["default_candidate"],
        minimum_return_improvement=value["minimum_return_improvement"],
        maximum_drawdown_deterioration=value["maximum_drawdown_deterioration"],
        maximum_turnover_increase=value["maximum_turnover_increase"],
        approved_stage_six_releases=tuple(value["approved_stage_six_releases"]),
    )


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


def _is_finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _validate_factor_research(
    settings: FactorResearchSettings,
    research: Period,
    validation: Period,
) -> None:
    analysis = Period(settings.analysis_start, settings.analysis_end)
    if analysis.end < analysis.start or not research.contains(analysis):
        raise ValueError("factor research analysis must stay inside research")
    if settings.data_start >= validation.start:
        raise ValueError("factor research data must stay before validation and test periods")
    if settings.analysis_start >= validation.start or settings.analysis_end >= validation.start:
        raise ValueError("factor research analysis must stay before validation and test periods")
    if settings.data_start >= settings.analysis_start:
        raise ValueError("factor research data_start must precede analysis_start")

    positive_integer_fields = (
        "universe_size",
        "minimum_history",
        "liquidity_lookback",
        "minimum_valid_months",
    )
    for field in positive_integer_fields:
        value = getattr(settings, field)
        if type(value) is not int or value <= 0:
            raise ValueError(f"factor_research.{field} must be a positive integer")

    if type(settings.quantile_count) is not int or settings.quantile_count < 2:
        raise ValueError("factor_research.quantile_count must be an integer of at least two")
    if not settings.forward_horizons or any(
        type(horizon) is not int or horizon <= 0 for horizon in settings.forward_horizons
    ):
        raise ValueError("factor_research.forward_horizons must contain positive integers")
    if type(settings.primary_horizon) is not int or settings.primary_horizon <= 0:
        raise ValueError("factor_research.primary_horizon must be a positive integer")
    if settings.primary_horizon not in settings.forward_horizons:
        raise ValueError("factor research primary_horizon must belong to forward_horizons")
    if settings.signal_frequency != "month_end":
        raise ValueError("factor research signal_frequency must be month_end")
    if settings.require_valid_trade_observation is not True:
        raise ValueError("factor research require_valid_trade_observation must be true")

    if not (
        _is_finite_number(settings.winsor_lower)
        and _is_finite_number(settings.winsor_upper)
        and 0.0 <= settings.winsor_lower < settings.winsor_upper <= 1.0
    ):
        raise ValueError("factor research winsor bounds must satisfy 0 <= lower < upper <= 1")
    for field in ("minimum_coverage", "fdr_q_threshold", "redundancy_threshold"):
        value = getattr(settings, field)
        if not _is_finite_number(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"factor_research.{field} must be finite and inside [0, 1]")


def _validate_stage_five(
    combination: FactorCombinationSettings,
    portfolio: PortfolioConstructionSettings,
    research: Period,
) -> None:
    if not research.contains(Period(combination.analysis_start, combination.analysis_end)):
        raise ValueError("factor combination must stay inside the research period")
    allowed = {
        "candidate_equal", "family_equal", "representative_equal", "rolling_ic_family"
    }
    if not combination.methods or set(combination.methods) != allowed:
        raise ValueError("factor_combination.methods must match the frozen methods")
    if combination.primary_method not in combination.methods:
        raise ValueError("factor_combination.primary_method must belong to methods")
    for field in ("minimum_families", "ic_window_months", "ic_minimum_months"):
        value = getattr(combination, field)
        if type(value) is not int or value <= 0:
            raise ValueError(f"factor_combination.{field} must be a positive integer")
    if combination.ic_minimum_months > combination.ic_window_months:
        raise ValueError("factor combination minimum history exceeds its window")
    if not _is_finite_number(combination.ic_shrinkage) or not 0 <= combination.ic_shrinkage <= 1:
        raise ValueError("factor_combination.ic_shrinkage must be inside [0, 1]")
    for field in ("portfolio_size", "size_groups", "per_group", "buffer_rank"):
        value = getattr(portfolio, field)
        if type(value) is not int or value <= 0:
            raise ValueError(f"portfolio_construction.{field} must be a positive integer")
    if portfolio.portfolio_size != portfolio.size_groups * portfolio.per_group:
        raise ValueError("portfolio_construction.portfolio_size must equal size_groups * per_group")
    if portfolio.buffer_rank < portfolio.per_group:
        raise ValueError("portfolio buffer_rank must be at least per_group")
    for field in ("minimum_coverage", "turnover_warning"):
        value = getattr(portfolio, field)
        if not _is_finite_number(value) or not 0 <= value <= 1:
            raise ValueError(f"portfolio_construction.{field} must be inside [0, 1]")


def load_config(path: Path) -> ResearchConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    supported_markets = tuple(raw["research_scope"]["supported_markets"])
    if supported_markets != ("sh", "sz"):
        raise ValueError("research_scope.supported_markets must be ordered [sh, sz]")
    periods = raw["periods"]
    factor_research = raw.get("factor_research")
    factor_combination = raw.get("factor_combination")
    portfolio_construction = raw.get("portfolio_construction")
    formal_backtest = raw.get("formal_backtest")
    validation_evaluation = raw.get("validation_evaluation")
    config = ResearchConfig(
        paths=Paths(**{key: Path(value) for key, value in raw["paths"].items()}),
        research=_period(periods["research"]),
        validation=_period(periods["validation"]),
        test=_period(periods["test"]),
        smoke_data=_period(periods["smoke_data"]),
        smoke_analysis=_period(periods["smoke_analysis"]),
        mvp=MvpSettings(**raw["mvp"]),
        factor_research=(
            _factor_research(factor_research, supported_markets=supported_markets)
            if factor_research is not None
            else None
        ),
        factor_combination=(
            _factor_combination(factor_combination) if factor_combination is not None else None
        ),
        portfolio_construction=(
            PortfolioConstructionSettings(**portfolio_construction)
            if portfolio_construction is not None else None
        ),
        formal_backtest=(
            FormalBacktestSettings(
                analysis_start=_as_date(formal_backtest["analysis_start"]),
                analysis_end=_as_date(formal_backtest["analysis_end"]),
                **{
                    key: value
                    for key, value in formal_backtest.items()
                    if key not in {"analysis_start", "analysis_end"}
                },
            )
            if formal_backtest is not None
            else None
        ),
        validation_evaluation=(
            _validation_evaluation(validation_evaluation)
            if validation_evaluation is not None
            else None
        ),
        supported_markets=supported_markets,
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
    if config.factor_research is not None:
        _validate_factor_research(config.factor_research, config.research, config.validation)
    if (config.factor_combination is None) != (config.portfolio_construction is None):
        raise ValueError("stage five configuration sections must be provided together")
    if config.factor_combination is not None and config.portfolio_construction is not None:
        _validate_stage_five(
            config.factor_combination, config.portfolio_construction, config.research
        )
    if config.formal_backtest is not None:
        settings = config.formal_backtest
        if not config.research.contains(Period(settings.analysis_start, settings.analysis_end)):
            raise ValueError("formal backtest must stay inside research period")
        if settings.analysis_end >= config.validation.start:
            raise ValueError("formal backtest must stay before validation")
        for field in ("buy_lot_size", "adv_lookback", "stale_review_days"):
            value = getattr(settings, field)
            if type(value) is not int or value <= 0:
                raise ValueError(f"formal_backtest.{field} must be a positive integer")
        for field in (
            "initial_cash",
            "maximum_participation",
            "reference_participation",
        ):
            value = getattr(settings, field)
            if not _is_finite_number(value) or value <= 0:
                raise ValueError(f"formal_backtest.{field} must be positive and finite")
        if settings.maximum_participation > 1:
            raise ValueError("formal_backtest.maximum_participation must not exceed one")
        for field in (
            "fixed_slippage_bps",
            "reference_impact_bps",
            "maximum_impact_bps",
            "commission_rate",
            "minimum_commission",
            "shadow_divergence_threshold",
        ):
            value = getattr(settings, field)
            if not _is_finite_number(value) or value < 0:
                raise ValueError(f"formal_backtest.{field} must be non-negative and finite")
    if config.validation_evaluation is not None:
        settings = config.validation_evaluation
        if Period(settings.analysis_start, settings.analysis_end) != config.validation:
            raise ValueError("validation evaluation must match the frozen validation period")
        if settings.data_start != date(2003, 1, 1):
            raise ValueError("validation evaluation data_start must preserve 2003 warmup")
        if settings.primary_metric != "net_annual_return":
            raise ValueError("validation primary metric must be net_annual_return")
        expected_candidates = {
            "family_equal_size_stratified_buffered",
            "rolling_ic_family_size_stratified_buffered",
            "family_equal_top100_equal",
        }
        if set(settings.candidates) != expected_candidates:
            raise ValueError("validation candidates must match the frozen comparison set")
        if settings.default_candidate not in settings.candidates:
            raise ValueError("validation default candidate must belong to candidates")
        if not settings.approved_stage_six_releases:
            raise ValueError("validation requires an approved stage six release")
        for field in (
            "minimum_return_improvement",
            "maximum_drawdown_deterioration",
            "maximum_turnover_increase",
        ):
            value = getattr(settings, field)
            if not _is_finite_number(value) or value < 0:
                raise ValueError(f"validation_evaluation.{field} must be non-negative")
    return config
