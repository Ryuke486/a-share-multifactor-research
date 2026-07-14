from __future__ import annotations

import math

import numpy as np
import polars as pl


def backtest_summary(
    result: dict[str, pl.DataFrame], *, initial_cash: float
) -> dict[str, float | int]:
    nav = result["nav"]
    trades = result["trades"]
    daily = np.asarray(nav["return"].to_list(), dtype=float)
    daily = daily[np.isfinite(daily)]
    initial = initial_cash
    ending = float(nav["nav"][-1])
    years = max((nav["date"][-1] - nav["date"][0]).days / 365.25, 1 / 252)
    annual_return = (ending / initial) ** (1 / years) - 1
    annual_volatility = float(np.std(daily, ddof=1) * math.sqrt(252)) if len(daily) > 1 else 0.0
    sharpe = (
        float(np.mean(daily) / np.std(daily, ddof=1) * math.sqrt(252))
        if len(daily) > 1 and np.std(daily, ddof=1)
        else 0.0
    )
    explicit_end = float(nav["explicit_cost_nav"][-1])
    zero_end = float(nav["zero_cost_nav"][-1])
    month_ends = nav.sort("date").group_by_dynamic("date", every="1mo").agg(
        pl.col("nav").last()
    ).with_columns(pl.col("nav").pct_change().alias("monthly_return"))
    monthly = month_ends["monthly_return"].drop_nulls()
    events = result["order_events"]
    waits = events.group_by("order_id").agg(
        (pl.col("date").max() - pl.col("date").min()).dt.total_days().alias("wait_days"),
        pl.col("status").eq("partially_filled").any().alias("was_partial"),
    )
    order_count = max(result["orders"].height, 1)
    traded_amount = float(trades["amount"].sum())
    total_cost = float(trades["total_cost"].sum())
    target_diagnostics = result["target_diagnostics"]
    return {
        "start_nav": initial,
        "end_nav": ending,
        "total_return": ending / initial - 1,
        "explicit_fee_only_total_return": explicit_end / initial - 1,
        "zero_cost_total_return": zero_end / initial - 1,
        "cost_erosion_vs_zero_cost": (zero_end - ending) / initial,
        "annual_return": annual_return,
        "annual_volatility": annual_volatility,
        "sharpe_zero_rate": sharpe,
        "maximum_drawdown": float(nav["drawdown"].min()),
        "trade_count": trades.height,
        "order_count": result["orders"].height,
        "filled_order_rate": result["orders"].filter(pl.col("status") == "filled").height
        / order_count,
        "orders_ever_partially_filled": waits.filter(pl.col("was_partial")).height,
        "partially_filled_order_rate": waits.filter(pl.col("was_partial")).height
        / order_count,
        "average_order_wait_days": float(waits["wait_days"].mean()),
        "monthly_win_rate": float((monthly > 0).mean() or 0.0),
        "average_cash_weight": float((nav["cash"] / nav["nav"]).mean()),
        "realized_turnover": traded_amount / float(nav["nav"].mean()),
        "annualized_traded_value_ratio": traded_amount / float(nav["nav"].mean()) / years,
        "implementation_shortfall_rate": total_cost / traded_amount if traded_amount else 0.0,
        "average_target_deviation_l1": float(
            target_diagnostics["target_deviation_l1"].mean() or 0.0
        ),
        "commission": float(trades["commission"].sum()),
        "stamp_duty": float(trades["stamp_duty"].sum()),
        "transfer_fee": float(trades["transfer_fee"].sum()),
        "slippage_cost": float(trades["slippage_cost"].sum()),
        "impact_cost": float(trades["impact_cost"].sum()),
        "total_cost": total_cost,
        "maximum_reconciliation_difference": float(
            result["reconciliation"]["difference"].abs().max()
        ),
        "maximum_scenario_reconciliation_difference": float(
            result["scenario_reconciliation"]["difference"].abs().max()
        ),
        "stale_position_rows": result["positions"].filter(pl.col("is_stale")).height,
        "maximum_stale_days": int(result["positions"]["stale_days"].max()),
    }
