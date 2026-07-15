from __future__ import annotations

from datetime import date
import math

import numpy as np
import polars as pl


def validation_portfolio_metrics(
    candidate: str,
    result: dict[str, pl.DataFrame],
    target_diagnostics: pl.DataFrame,
    *,
    validation_start: date = date(2017, 1, 1),
    validation_end: date = date(2021, 12, 31),
) -> dict[str, object]:
    """Summarize the validation slice while retaining the research boundary NAV."""
    nav = result["nav"].sort("date")
    validation = nav.filter(pl.col("date").is_between(validation_start, validation_end))
    prior = nav.filter(pl.col("date") < validation_start).tail(1)
    if validation.is_empty() or prior.is_empty():
        raise ValueError("validation NAV requires a research-period boundary row")
    boundary_nav = float(prior.item(0, "nav"))
    boundary_zero = float(prior.item(0, "zero_cost_nav"))
    end_nav = float(validation.get_column("nav").tail(1).item())
    end_zero = float(validation.get_column("zero_cost_nav").tail(1).item())
    years = (validation_end - validation_start).days / 365.25
    annual_return = (end_nav / boundary_nav) ** (1.0 / years) - 1.0
    zero_annual_return = (end_zero / boundary_zero) ** (1.0 / years) - 1.0
    path = np.concatenate(([boundary_nav], validation.get_column("nav").to_numpy()))
    daily_returns = path[1:] / path[:-1] - 1.0
    running_peak = np.maximum.accumulate(path)
    maximum_drawdown = float(np.min(path / running_peak - 1.0))
    diagnostics = target_diagnostics.filter(
        pl.col("date").is_between(validation_start, validation_end)
    )
    execution_diagnostics = result["target_diagnostics"].filter(
        pl.col("date").is_between(validation_start, validation_end)
    )
    reconciliation = result["scenario_reconciliation"].filter(
        pl.col("date").is_between(validation_start, validation_end)
    )
    return {
        "candidate": candidate,
        "net_annual_return": annual_return,
        "annual_volatility": (
            float(np.std(daily_returns, ddof=1) * math.sqrt(252.0))
            if len(daily_returns) > 1
            else None
        ),
        "maximum_drawdown": maximum_drawdown,
        "turnover": diagnostics.get_column("one_way_turnover").mean(),
        "cost_erosion": zero_annual_return - annual_return,
        "average_cash_weight": (
            validation.get_column("cash") / validation.get_column("nav")
        ).mean(),
        "average_target_deviation": execution_diagnostics.get_column(
            "target_deviation_l1"
        ).mean(),
        "maximum_reconciliation_difference": reconciliation.get_column(
            "difference"
        ).abs().max(),
    }
