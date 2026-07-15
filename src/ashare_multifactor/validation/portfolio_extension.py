from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from ashare_multifactor.portfolio.diagnostics import portfolio_diagnostics
from ashare_multifactor.portfolio.ranking import top_n_equal
from ashare_multifactor.portfolio.size_stratified import size_stratified_buffered
from ashare_multifactor.validation.protocol import assert_validation_read_allowed


@dataclass(frozen=True)
class ValidationTargets:
    targets: pl.DataFrame
    diagnostics: pl.DataFrame


def build_validation_targets(
    scores: pl.DataFrame,
    log_size: pl.DataFrame,
    *,
    method: str,
    portfolio_name: str,
    previous_targets: pl.DataFrame,
    portfolio_size: int,
    size_groups: int,
    per_group: int,
    buffer_rank: int,
) -> ValidationTargets:
    selected = scores.filter(pl.col("method") == method).join(
        log_size, on=["date", "symbol"], how="left", validate="1:1"
    )
    if selected.is_empty():
        raise ValueError(f"no scores for validation method: {method}")
    assert_validation_read_allowed(
        selected.get_column("date").min(), selected.get_column("date").max()
    )
    prior = previous_targets.select("symbol", "target_weight")
    parts: list[pl.DataFrame] = []
    diagnostics: list[dict[str, object]] = []
    continued = not prior.is_empty()
    for signal_date in selected.get_column("date").unique().sort():
        cross = selected.filter(pl.col("date") == signal_date)
        if portfolio_name == "top100_equal":
            target = top_n_equal(
                cross,
                portfolio_size=portfolio_size,
                portfolio_name=portfolio_name,
            )
        elif portfolio_name == "size_stratified_buffered":
            target = size_stratified_buffered(
                cross,
                previous_targets=prior,
                size_groups=size_groups,
                per_group=per_group,
                buffer_rank=buffer_rank,
                portfolio_name=portfolio_name,
            )
        else:
            raise ValueError(f"unsupported validation portfolio: {portfolio_name}")
        target = target.with_columns(
            pl.lit(signal_date).alias("date"), pl.lit(method).alias("method")
        )
        diagnostics.append(
            {
                "date": signal_date,
                "method": method,
                "portfolio_name": portfolio_name,
                "continued_from_research": continued and not parts,
                **portfolio_diagnostics(target, prior),
            }
        )
        parts.append(
            target.select("date", "method", "portfolio_name", "symbol", "target_weight")
        )
        prior = target.select("symbol", "target_weight")
    targets = pl.concat(parts).sort("date", "symbol")
    sums = targets.group_by("date").agg(pl.col("target_weight").sum().alias("weight"))
    if sums.filter((pl.col("weight") - 1.0).abs() > 1e-12).height:
        raise ValueError("validation target weights must sum to one")
    return ValidationTargets(targets, pl.DataFrame(diagnostics).sort("date"))
