"""Auditable MVP backtest with next-session open execution.

This deliberately models fractional holdings, one uniform transaction-cost
rate, and minimal missing-quote blocking. Full suspension rules, price limits,
board lots, historical fee schedules, and order lifecycle handling belong to
the later production backtest rather than this MVP.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any

import polars as pl

from ashare_multifactor.config import ResearchConfig
from ashare_multifactor.research.execution import execute_rebalance


@dataclass(frozen=True)
class BacktestResult:
    nav: pl.DataFrame
    trades: pl.DataFrame
    holdings: pl.DataFrame
    blocked_orders: pl.DataFrame
    summary: dict[str, float]


_NAV_SCHEMA = {
    "date": pl.Date,
    "cash": pl.Float64,
    "holdings_value": pl.Float64,
    "nav": pl.Float64,
    "daily_return": pl.Float64,
    "turnover": pl.Float64,
    "cost": pl.Float64,
    "drawdown": pl.Float64,
}
_TRADE_SCHEMA = {
    "signal_date": pl.Date,
    "execution_date": pl.Date,
    "symbol": pl.String,
    "side": pl.String,
    "price": pl.Float64,
    "trade_units": pl.Float64,
    "gross_amount": pl.Float64,
    "cost": pl.Float64,
    "target_weight": pl.Float64,
}
_HOLDING_SCHEMA = {
    "date": pl.Date,
    "symbol": pl.String,
    "units": pl.Float64,
    "close_price": pl.Float64,
    "market_value": pl.Float64,
    "weight": pl.Float64,
    "price_is_stale": pl.Boolean,
    "valuation_price_date": pl.Date,
}
_BLOCKED_ORDER_SCHEMA = {
    "signal_date": pl.Date,
    "execution_date": pl.Date,
    "symbol": pl.String,
    "side": pl.String,
    "reason": pl.String,
    "target_weight": pl.Float64,
    "units_held": pl.Float64,
}


def run_backtest(
    prices: pl.DataFrame,
    target_weights: pl.DataFrame,
    config: ResearchConfig,
) -> BacktestResult:
    """Execute frozen target weights at ``execution_date`` adjusted opens."""
    prices = prices.filter(
        pl.col("date").is_between(
            config.smoke_analysis.start,
            config.smoke_analysis.end,
            closed="both",
        )
    )
    _validate_inputs(prices, target_weights, config)
    price_by_date = _price_lookup(prices)
    targets_by_date = _target_lookup(target_weights)

    cash = float(config.mvp.initial_cash)
    cost_rate = float(config.mvp.transaction_cost_bps) / 10_000.0
    positions: dict[str, float] = {}
    last_closes: dict[str, tuple[Any, float]] = {}
    nav_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []
    holding_rows: list[dict[str, Any]] = []
    blocked_order_rows: list[dict[str, Any]] = []
    rebalance_turnovers: list[float] = []
    previous_nav = cash
    running_peak = cash

    for trading_date in sorted(price_by_date):
        daily_prices = price_by_date[trading_date]
        opening_values = {
            symbol: units * _opening_valuation_price(daily_prices, last_closes, symbol)
            for symbol, units in positions.items()
        }
        opening_equity = cash + sum(opening_values.values())
        daily_trades: list[dict[str, Any]] = []

        if trading_date in targets_by_date:
            target_rows = targets_by_date[trading_date]
            signal_date, weights = _normalized_weights(target_rows)
            execution_prices = {
                symbol: _optional_valid_price(daily_prices, symbol, "open_adj")
                for symbol in set(positions).union(weights)
            }
            execution = execute_rebalance(
                signal_date=signal_date,
                execution_date=trading_date,
                execution_prices=execution_prices,
                positions=positions,
                current_values=opening_values,
                weights=weights,
                opening_equity=opening_equity,
                cash=cash,
                cost_rate=cost_rate,
            )
            cash = execution.cash
            positions = execution.positions
            daily_trades = execution.trades
            gross_amount = sum(float(row["gross_amount"]) for row in daily_trades)
            daily_turnover = gross_amount / opening_equity if opening_equity else 0.0
            rebalance_turnovers.append(daily_turnover)
            trade_rows.extend(daily_trades)
            blocked_order_rows.extend(execution.blocked_orders)
        else:
            daily_turnover = 0.0

        daily_cost = sum(float(row["cost"]) for row in daily_trades)
        _record_current_closes(last_closes, daily_prices, trading_date)

        closing_values: dict[str, float] = {}
        valuation_prices: dict[str, tuple[Any, float]] = {}
        for symbol, units in positions.items():
            valuation_date, price = _closing_valuation_price(
                daily_prices,
                last_closes,
                symbol,
                trading_date,
            )
            closing_values[symbol] = units * price
            valuation_prices[symbol] = (valuation_date, price)
        holdings_value = sum(closing_values.values())
        nav = cash + holdings_value
        daily_return = nav / previous_nav - 1.0 if previous_nav else 0.0
        running_peak = max(running_peak, nav)
        drawdown = nav / running_peak - 1.0 if running_peak else 0.0
        nav_rows.append(
            {
                "date": trading_date,
                "cash": cash,
                "holdings_value": holdings_value,
                "nav": nav,
                "daily_return": daily_return,
                "turnover": daily_turnover,
                "cost": daily_cost,
                "drawdown": drawdown,
            }
        )
        holding_rows.extend(
            {
                "date": trading_date,
                "symbol": symbol,
                "units": positions[symbol],
                "close_price": closing_values[symbol] / positions[symbol],
                "market_value": closing_values[symbol],
                "weight": closing_values[symbol] / nav if nav else 0.0,
                "price_is_stale": valuation_prices[symbol][0] != trading_date,
                "valuation_price_date": valuation_prices[symbol][0],
            }
            for symbol in sorted(positions)
        )
        previous_nav = nav

    nav_frame = pl.DataFrame(nav_rows, schema=_NAV_SCHEMA)
    trades_frame = pl.DataFrame(trade_rows, schema=_TRADE_SCHEMA)
    holdings_frame = pl.DataFrame(holding_rows, schema=_HOLDING_SCHEMA)
    blocked_orders_frame = pl.DataFrame(blocked_order_rows, schema=_BLOCKED_ORDER_SCHEMA)
    summary = _summarize(
        nav_rows,
        rebalance_turnovers,
        trade_rows,
        holding_rows,
        blocked_order_rows,
        float(config.mvp.initial_cash),
    )
    return BacktestResult(
        nav=nav_frame,
        trades=trades_frame,
        holdings=holdings_frame,
        blocked_orders=blocked_orders_frame,
        summary=summary,
    )


def _validate_inputs(
    prices: pl.DataFrame,
    target_weights: pl.DataFrame,
    config: ResearchConfig,
) -> None:
    price_columns = {"date", "symbol", "open_adj", "close_adj"}
    target_columns = {"signal_date", "execution_date", "symbol", "target_weight"}
    if missing := price_columns.difference(prices.columns):
        raise ValueError(f"prices missing columns: {sorted(missing)}")
    if missing := target_columns.difference(target_weights.columns):
        raise ValueError(f"target_weights missing columns: {sorted(missing)}")
    if prices.select("date", "symbol").is_duplicated().any():
        raise ValueError("prices must be unique by date and symbol")
    if target_weights.select("execution_date", "symbol").is_duplicated().any():
        raise ValueError("target weights must be unique by execution date and symbol")
    if any(
        row["execution_date"] <= row["signal_date"]
        for row in target_weights.select("signal_date", "execution_date").to_dicts()
    ):
        raise ValueError("execution_date must be later than signal_date")
    execution_dates = set(target_weights.get_column("execution_date").to_list())
    price_dates = set(prices.get_column("date").to_list())
    if missing_dates := execution_dates.difference(price_dates):
        raise ValueError(f"execution dates missing from prices: {sorted(missing_dates)}")
    if not math.isfinite(config.mvp.initial_cash) or config.mvp.initial_cash <= 0.0:
        raise ValueError("initial_cash must be positive and finite")
    cost_rate = config.mvp.transaction_cost_bps / 10_000.0
    if not math.isfinite(cost_rate) or not 0.0 <= cost_rate < 1.0:
        raise ValueError("transaction cost rate must be finite and in [0, 1)")


def _price_lookup(prices: pl.DataFrame) -> dict[Any, dict[str, dict[str, Any]]]:
    lookup: dict[Any, dict[str, dict[str, Any]]] = {}
    for row in prices.sort("date", "symbol").to_dicts():
        lookup.setdefault(row["date"], {})[row["symbol"]] = row
    return lookup


def _target_lookup(targets: pl.DataFrame) -> dict[Any, list[dict[str, Any]]]:
    lookup: dict[Any, list[dict[str, Any]]] = {}
    for row in targets.sort("execution_date", "symbol").to_dicts():
        lookup.setdefault(row["execution_date"], []).append(row)
    return lookup


def _optional_valid_price(
    daily_prices: dict[str, dict[str, Any]],
    symbol: str,
    field: str,
) -> float | None:
    row = daily_prices.get(symbol)
    if row is None or row.get(field) is None:
        return None
    value = float(row[field])
    return value if math.isfinite(value) and value > 0.0 else None


def _latest_close(
    last_closes: dict[str, tuple[Any, float]],
    symbol: str,
) -> tuple[Any, float]:
    if symbol not in last_closes:
        raise ValueError(f"no historical close available to value {symbol}")
    return last_closes[symbol]


def _opening_valuation_price(
    daily_prices: dict[str, dict[str, Any]],
    last_closes: dict[str, tuple[Any, float]],
    symbol: str,
) -> float:
    current_open = _optional_valid_price(daily_prices, symbol, "open_adj")
    return current_open if current_open is not None else _latest_close(last_closes, symbol)[1]


def _record_current_closes(
    last_closes: dict[str, tuple[Any, float]],
    daily_prices: dict[str, dict[str, Any]],
    trading_date: Any,
) -> None:
    for symbol in daily_prices:
        close = _optional_valid_price(daily_prices, symbol, "close_adj")
        if close is not None:
            last_closes[symbol] = (trading_date, close)


def _closing_valuation_price(
    daily_prices: dict[str, dict[str, Any]],
    last_closes: dict[str, tuple[Any, float]],
    symbol: str,
    trading_date: Any,
) -> tuple[Any, float]:
    current_close = _optional_valid_price(daily_prices, symbol, "close_adj")
    if current_close is not None:
        return trading_date, current_close
    return _latest_close(last_closes, symbol)


def _normalized_weights(
    target_rows: list[dict[str, Any]],
) -> tuple[Any, dict[str, float]]:
    signal_dates = {row["signal_date"] for row in target_rows}
    if len(signal_dates) != 1:
        raise ValueError("one execution date must map to one signal date")
    weights = {row["symbol"]: float(row["target_weight"]) for row in target_rows}
    if any(not math.isfinite(weight) or weight < 0.0 for weight in weights.values()):
        raise ValueError("target weights must be non-negative and finite")
    total_weight = sum(weights.values())
    if not math.isclose(total_weight, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("target weights must sum to one on each execution date")
    return signal_dates.pop(), {symbol: weight / total_weight for symbol, weight in weights.items()}


def _summarize(
    nav_rows: list[dict[str, Any]],
    rebalance_turnovers: list[float],
    trade_rows: list[dict[str, Any]],
    holding_rows: list[dict[str, Any]],
    blocked_order_rows: list[dict[str, Any]],
    initial_cash: float,
) -> dict[str, float]:
    final_nav = float(nav_rows[-1]["nav"]) if nav_rows else initial_cash
    total_return = final_nav / initial_cash - 1.0
    observations = len(nav_rows)
    annualized_return = (
        (final_nav / initial_cash) ** (252.0 / observations) - 1.0
        if observations and final_nav > 0.0
        else 0.0
    )
    daily_returns = [float(row["daily_return"]) for row in nav_rows]
    daily_volatility = statistics.stdev(daily_returns) if len(daily_returns) >= 2 else 0.0
    annualized_volatility = daily_volatility * math.sqrt(252.0)
    sharpe = (
        statistics.fmean(daily_returns) / daily_volatility * math.sqrt(252.0)
        if daily_volatility > 0.0
        else 0.0
    )
    max_drawdown = -min((float(row["drawdown"]) for row in nav_rows), default=0.0)
    return {
        "initial_cash": initial_cash,
        "final_nav": final_nav,
        "total_return": total_return,
        "annualized_return": annualized_return,
        "annualized_volatility": annualized_volatility,
        "sharpe_zero_rf": sharpe,
        "max_drawdown": max_drawdown,
        "average_turnover": (statistics.fmean(rebalance_turnovers) if rebalance_turnovers else 0.0),
        "total_cost": sum(float(row["cost"]) for row in trade_rows),
        "blocked_order_count": float(len(blocked_order_rows)),
        "stale_valuation_count": float(sum(bool(row["price_is_stale"]) for row in holding_rows)),
        "uses_fractional_holdings": 1.0,
        "models_suspensions": 0.0,
        "models_price_limits": 0.0,
        "models_board_lots": 0.0,
        "models_historical_fees": 0.0,
        "models_unfilled_orders": 0.0,
    }
