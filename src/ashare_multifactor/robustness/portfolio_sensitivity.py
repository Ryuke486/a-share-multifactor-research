from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

import polars as pl

from ashare_multifactor.robustness.protocol import RobustnessProtocol
from ashare_multifactor.validation.portfolio_extension import (
    ValidationTargets,
    build_validation_targets,
)


@dataclass(frozen=True)
class PortfolioVariant:
    experiment_id: str
    parameters: Mapping[str, int]


class ExpectedExperimentUnavailability(ValueError):
    """A pre-registered scenario that the frozen upstream universe cannot support."""


def build_portfolio_variants(
    protocol: RobustnessProtocol,
) -> tuple[PortfolioVariant, ...]:
    definitions = [item for item in protocol.experiments if item.category == "portfolio"]
    baselines = [item for item in definitions if item.baseline]
    if len(baselines) != 1:
        raise ValueError("portfolio protocol requires one baseline")
    baseline = {key: int(value) for key, value in baselines[0].parameters.items()}
    required = {"portfolio_size", "buffer_rank", "universe_size", "rebalance_months"}
    if set(baseline) != required:
        raise ValueError("portfolio baseline parameters are incomplete")
    output = []
    for experiment in definitions:
        values = baseline | {
            key: int(value) for key, value in experiment.parameters.items()
        }
        changed = [key for key in required if values[key] != baseline[key]]
        if not experiment.baseline and len(changed) != 1:
            raise ValueError(
                f"portfolio variant must change one assumption: {experiment.experiment_id}"
            )
        output.append(
            PortfolioVariant(
                experiment.experiment_id,
                MappingProxyType(values),
            )
        )
    return tuple(output)


def apply_rebalance_schedule(
    scores: pl.DataFrame, *, every_months: int
) -> pl.DataFrame:
    if every_months <= 0:
        raise ValueError("rebalance interval must be positive")
    dates = scores.get_column("date").unique().sort().to_list()
    selected = dates[::every_months]
    return scores.filter(pl.col("date").is_in(selected)).sort("date", "symbol")


def restrict_liquid_universe(
    scores: pl.DataFrame,
    liquidity: pl.DataFrame,
    *,
    universe_size: int,
) -> pl.DataFrame:
    if universe_size <= 0:
        raise ValueError("universe size must be positive")
    joined = scores.join(
        liquidity.select("date", "symbol", "liquidity"),
        on=["date", "symbol"],
        how="left",
        validate="1:1",
    )
    counts = joined.group_by("date").len().get_column("len")
    if counts.is_empty() or counts.min() < universe_size:
        raise ExpectedExperimentUnavailability(
            "requested universe exceeds frozen upstream"
        )
    return (
        joined.sort(
            "date", "liquidity", "symbol", descending=[False, True, False]
        )
        .group_by("date", maintain_order=True)
        .head(universe_size)
        .drop("liquidity")
        .sort("date", "symbol")
    )


def build_variant_targets(
    scores: pl.DataFrame,
    log_size: pl.DataFrame,
    liquidity: pl.DataFrame,
    previous_targets: pl.DataFrame,
    variant: PortfolioVariant,
    *,
    method: str = "rolling_ic_family",
    size_groups: int = 5,
) -> ValidationTargets:
    values = variant.parameters
    portfolio_size = values["portfolio_size"]
    if portfolio_size % size_groups:
        raise ValueError("portfolio size must be divisible by size groups")
    selected = scores.filter(pl.col("method") == method)
    selected = restrict_liquid_universe(
        selected,
        liquidity,
        universe_size=values["universe_size"],
    )
    selected = apply_rebalance_schedule(
        selected,
        every_months=values["rebalance_months"],
    )
    return build_validation_targets(
        selected,
        log_size,
        method=method,
        portfolio_name="size_stratified_buffered",
        previous_targets=previous_targets,
        portfolio_size=portfolio_size,
        size_groups=size_groups,
        per_group=portfolio_size // size_groups,
        buffer_rank=values["buffer_rank"],
    )
