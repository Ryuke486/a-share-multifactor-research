"""Monthly raw factor-panel construction."""

from __future__ import annotations

from collections.abc import Callable, Sequence

import polars as pl

from ashare_multifactor.config import FactorResearchSettings
from ashare_multifactor.factors.definitions import FactorDefinition
from ashare_multifactor.factors.liquidity import compute_liquidity_factors
from ashare_multifactor.factors.low_volatility import compute_low_volatility_factors
from ashare_multifactor.factors.momentum import compute_momentum_factors
from ashare_multifactor.factors.reversal import compute_reversal_factors
from ashare_multifactor.factors.size import compute_size_factor
from ashare_multifactor.factors.value import compute_value_factors
from ashare_multifactor.research.research_universe import build_research_universe


_FAMILY_COMPUTERS: dict[str, Callable[[pl.DataFrame], pl.DataFrame]] = {
    "value": compute_value_factors,
    "momentum": compute_momentum_factors,
    "reversal": compute_reversal_factors,
    "liquidity": compute_liquidity_factors,
    "low_volatility": compute_low_volatility_factors,
    "size": compute_size_factor,
}
_KEY_COLUMNS = ("date", "symbol")
_UNIVERSE_COLUMNS = (
    *_KEY_COLUMNS,
    "close_adj",
    "volume",
    "amount",
    "is_st",
    "open_raw",
    "high_raw",
    "low_raw",
    "close_raw",
)
_RAW_COLUMNS = (*_KEY_COLUMNS, "factor_name", "family", "raw_value")
_RAW_SCHEMA = {
    "date": pl.Date,
    "symbol": pl.String,
    "factor_name": pl.String,
    "family": pl.String,
    "raw_value": pl.Float64,
}


def build_monthly_factor_panel(
    frame: pl.DataFrame,
    definitions: Sequence[FactorDefinition],
    settings: FactorResearchSettings,
) -> pl.DataFrame:
    """Build the eligible global-month-end raw panel one factor family at a time."""
    definitions = tuple(definitions)
    if not definitions:
        return _empty_raw_panel()
    _validate_inputs(frame, definitions, settings)

    bounded = frame.filter(pl.col("date") >= settings.data_start)
    universe = build_research_universe(bounded.select(*_UNIVERSE_COLUMNS), settings)
    signal_dates = (
        bounded.filter(pl.col("date").is_between(settings.analysis_start, settings.analysis_end))
        .select("date")
        .unique()
        .with_columns(
            pl.col("date").dt.year().alias("_year"),
            pl.col("date").dt.month().alias("_month"),
        )
        .group_by("_year", "_month")
        .agg(pl.col("date").max())
        .select("date")
    )
    eligible_keys = (
        universe.join(signal_dates, on="date", how="inner", validate="m:1")
        .filter("is_research_eligible")
        .select(*_KEY_COLUMNS)
        .sort(*_KEY_COLUMNS)
    )

    monthly_families: list[pl.DataFrame] = []
    for family, family_definitions in _definitions_by_family(definitions):
        computer = _FAMILY_COMPUTERS.get(family)
        if computer is None:
            raise ValueError(f"unsupported factor family: {family}")
        factor_names = tuple(definition.name for definition in family_definitions)
        source_columns = tuple(
            dict.fromkeys(
                column for definition in family_definitions for column in definition.source_columns
            )
        )
        family_inputs = bounded.select(*_KEY_COLUMNS, *source_columns)
        daily_family = computer(family_inputs)
        monthly_family = (
            eligible_keys.join(
                daily_family.select(*_KEY_COLUMNS, *factor_names),
                on=list(_KEY_COLUMNS),
                how="left",
                validate="1:1",
            )
            .unpivot(
                on=factor_names,
                index=_KEY_COLUMNS,
                variable_name="factor_name",
                value_name="raw_value",
            )
            .with_columns(pl.lit(family).alias("family"))
            .select(*_RAW_COLUMNS)
        )
        monthly_families.append(monthly_family)
        del family_inputs, daily_family, monthly_family

    if not monthly_families:
        return _empty_raw_panel()
    return (
        pl.concat(monthly_families, how="vertical")
        .cast(_RAW_SCHEMA)
        .sort(*_KEY_COLUMNS, "factor_name")
    )


def _validate_inputs(
    frame: pl.DataFrame,
    definitions: Sequence[FactorDefinition],
    settings: FactorResearchSettings,
) -> None:
    names = [definition.name for definition in definitions]
    if len(names) != len(set(names)):
        raise ValueError("duplicate factor definitions are not allowed")
    if frame.select(*_KEY_COLUMNS).is_duplicated().any():
        raise ValueError("duplicate date and symbol keys are not allowed")
    latest_date = frame.get_column("date").max()
    if latest_date is not None and latest_date > settings.analysis_end:
        raise ValueError("factor panel contains rows after factor research analysis_end")


def _definitions_by_family(
    definitions: Sequence[FactorDefinition],
) -> tuple[tuple[str, tuple[FactorDefinition, ...]], ...]:
    families: dict[str, list[FactorDefinition]] = {}
    for definition in definitions:
        families.setdefault(definition.family, []).append(definition)
    return tuple((family, tuple(items)) for family, items in families.items())


def _empty_raw_panel() -> pl.DataFrame:
    return pl.DataFrame(schema=_RAW_SCHEMA)
