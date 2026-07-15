from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Any

import numpy as np
import polars as pl

from ashare_multifactor.execution.broker import BacktestSettings
from ashare_multifactor.execution.fees import FeeSchedule
from ashare_multifactor.robustness.protocol import RobustnessProtocol


@dataclass(frozen=True)
class ExecutionScenario:
    experiment_id: str
    settings: BacktestSettings
    fees: FeeSchedule
    result_scenario: str = "full_cost"


def build_execution_scenarios(
    protocol: RobustnessProtocol,
    baseline_settings: BacktestSettings,
    baseline_fees: FeeSchedule,
) -> tuple[ExecutionScenario, ...]:
    """Translate only pre-registered execution changes into immutable settings."""
    scenarios: list[ExecutionScenario] = []
    for experiment in protocol.experiments:
        if experiment.category != "execution":
            continue
        values = dict(experiment.parameters)
        cost_mode = str(values.pop("cost_mode", "full"))
        setting_fields = {
            key: values.pop(key)
            for key in tuple(values)
            if key in BacktestSettings.__dataclass_fields__
        }
        fee_fields = {
            key: values.pop(key)
            for key in tuple(values)
            if key in {"commission_rate", "minimum_commission"}
        }
        if values:
            raise ValueError(
                f"unsupported execution parameters for {experiment.experiment_id}: "
                + ", ".join(sorted(values))
            )
        scenario_name = {
            "full": "full_cost",
            "zero": "zero_cost",
            "explicit_only": "explicit_fee_only",
        }.get(cost_mode)
        if scenario_name is None:
            raise ValueError(f"unsupported cost mode: {cost_mode}")
        scenarios.append(
            ExecutionScenario(
                experiment.experiment_id,
                replace(baseline_settings, **setting_fields),
                replace(baseline_fees, **fee_fields),
                scenario_name,
            )
        )
    return tuple(scenarios)


def select_backtest_scenario(
    result: dict[str, pl.DataFrame], scenario: str
) -> dict[str, pl.DataFrame]:
    """Select a broker cost scenario without changing signals or orders."""
    if scenario == "full_cost":
        return {
            name: result[name]
            for name in ("nav", "orders", "trades", "target_diagnostics")
        }
    selected: dict[str, pl.DataFrame] = {}
    for name in ("nav", "orders", "trades", "target_diagnostics"):
        frame = result[f"scenario_{name}"]
        selected[name] = frame.filter(pl.col("scenario") == scenario).drop("scenario")
    return selected


def summarize_execution_result(
    experiment_id: str,
    result: dict[str, pl.DataFrame],
) -> dict[str, Any]:
    """Summarize return, trading friction, fill and target-deviation jointly."""
    nav = result["nav"].sort("date")
    if nav.height < 2:
        raise ValueError("execution sensitivity requires at least two NAV rows")
    start = nav.item(0, "date")
    end = nav.item(-1, "date")
    years = max(int(round((end - start).days / 365.25)), 1)
    start_nav = float(nav.item(0, "nav"))
    end_nav = float(nav.item(-1, "nav"))
    zero_column = "zero_cost_nav" if "zero_cost_nav" in nav.columns else "nav"
    zero_end = float(nav.item(-1, zero_column))
    annual_return = (end_nav / start_nav) ** (1.0 / years) - 1.0
    zero_annual = (zero_end / start_nav) ** (1.0 / years) - 1.0
    path = nav.get_column("nav").to_numpy()
    returns = path[1:] / path[:-1] - 1.0
    drawdown = path / np.maximum.accumulate(path) - 1.0
    trades = result["trades"]
    orders = result["orders"]
    diagnostics = result["target_diagnostics"]
    gross = float(trades.get_column("amount").sum()) if trades.height else 0.0
    total_cost = (
        float(trades.get_column("total_cost").sum())
        if trades.height and "total_cost" in trades.columns
        else 0.0
    )
    requested = float(orders.get_column("quantity").sum()) if orders.height else 0.0
    remaining = (
        float(orders.get_column("remaining_quantity").sum()) if orders.height else 0.0
    )
    return {
        "experiment_id": experiment_id,
        "annual_return": annual_return,
        "annual_volatility": (
            float(np.std(returns, ddof=1) * math.sqrt(252.0))
            if len(returns) > 1
            else None
        ),
        "maximum_drawdown": float(drawdown.min()),
        "turnover": gross / (2.0 * float(path.mean()) * years),
        "cost_erosion": zero_annual - annual_return,
        "unfilled_rate": remaining / requested if requested else 0.0,
        "target_deviation": (
            float(diagnostics.get_column("target_deviation_l1").mean())
            if diagnostics.height
            else None
        ),
        "total_cost_rate": total_cost / gross if gross else 0.0,
        "observations": nav.height,
        "start_date": start,
        "end_date": end,
    }
