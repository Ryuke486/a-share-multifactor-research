from __future__ import annotations

import polars as pl


SCHEMAS: dict[str, dict[str, pl.DataType]] = {
    "orders": {
        "order_id": pl.String,
        "signal_date": pl.Date,
        "symbol": pl.String,
        "side": pl.String,
        "quantity": pl.Int64,
        "remaining_quantity": pl.Int64,
        "status": pl.String,
    },
    "order_events": {
        "order_id": pl.String,
        "event_seq": pl.Int64,
        "date": pl.Date,
        "status": pl.String,
        "remaining_quantity": pl.Int64,
        "reason": pl.String,
    },
    "trades": {
        "trade_id": pl.String,
        "order_id": pl.String,
        "date": pl.Date,
        "symbol": pl.String,
        "side": pl.String,
        "quantity": pl.Int64,
        "open_price": pl.Float64,
        "execution_price": pl.Float64,
        "amount": pl.Float64,
        "commission": pl.Float64,
        "stamp_duty": pl.Float64,
        "transfer_fee": pl.Float64,
        "slippage_cost": pl.Float64,
        "impact_cost": pl.Float64,
        "total_cost": pl.Float64,
        "adv20": pl.Float64,
    },
    "cash_ledger": {
        "cash_event_id": pl.String,
        "date": pl.Date,
        "event_type": pl.String,
        "symbol": pl.String,
        "amount": pl.Float64,
        "order_id": pl.String,
    },
    "receivable_ledger": {
        "action_id": pl.String,
        "date": pl.Date,
        "event_type": pl.String,
        "symbol": pl.String,
        "amount": pl.Float64,
    },
    "positions": {
        "date": pl.Date,
        "symbol": pl.String,
        "quantity": pl.Int64,
        "available_quantity": pl.Int64,
        "close_price": pl.Float64,
        "market_value": pl.Float64,
        "is_stale": pl.Boolean,
        "stale_days": pl.Int64,
    },
    "nav": {
        "date": pl.Date,
        "cash": pl.Float64,
        "receivables": pl.Float64,
        "holdings_value": pl.Float64,
        "nav": pl.Float64,
        "return": pl.Float64,
        "drawdown": pl.Float64,
        "explicit_cost_nav": pl.Float64,
        "zero_cost_nav": pl.Float64,
    },
    "reconciliation": {
        "date": pl.Date,
        "cash": pl.Float64,
        "receivables": pl.Float64,
        "holdings_value": pl.Float64,
        "nav": pl.Float64,
        "difference": pl.Float64,
    },
    "scenario_reconciliation": {
        "date": pl.Date,
        "scenario": pl.String,
        "cash": pl.Float64,
        "receivables": pl.Float64,
        "holdings_value": pl.Float64,
        "nav": pl.Float64,
        "difference": pl.Float64,
    },
    "target_diagnostics": {
        "date": pl.Date,
        "target_deviation_l1": pl.Float64,
        "cash_weight": pl.Float64,
    },
}


def build_output_frames(
    row_sets: dict[str, list[dict[str, object]]],
) -> dict[str, pl.DataFrame]:
    if set(row_sets) != set(SCHEMAS):
        raise ValueError("backtest output row sets do not match frozen schemas")
    return {
        name: pl.DataFrame(rows, schema=SCHEMAS[name])
        if rows
        else pl.DataFrame(schema=SCHEMAS[name])
        for name, rows in row_sets.items()
    }
