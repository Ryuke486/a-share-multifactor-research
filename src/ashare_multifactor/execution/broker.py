from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
import hashlib
import math

import polars as pl

from .fees import FeeSchedule
from .ledger import Ledger
from .orders import Order, OrderStatus, transition
from .order_rebalancer import build_target_order_specs
from .output_frames import build_output_frames
from .price_limits import limit_prices
from .sizing import proportional_buy_quantities
from .slippage import execution_price


@dataclass(frozen=True)
class BacktestSettings:
    initial_cash: float = 100_000_000.0
    maximum_participation: float = 0.10
    fixed_slippage_bps: float = 5.0
    reference_impact_bps: float = 10.0
    reference_participation: float = 0.01
    maximum_impact_bps: float = 50.0
    buy_lot_size: int = 100


def _order_id(signal_date: date, symbol: str, side: str, sequence: int) -> str:
    raw = f"{signal_date}|{symbol}|{side}|{sequence}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _run_backtest_once(
    execution_panel: pl.DataFrame,
    target_weights: pl.DataFrame,
    corporate_actions: pl.DataFrame,
    fees: FeeSchedule,
    settings: BacktestSettings,
    security_events: pl.DataFrame | None = None,
) -> dict[str, pl.DataFrame]:
    if not 0 < settings.maximum_participation <= 1:
        raise ValueError("maximum_participation must be inside (0, 1]")
    dates = execution_panel["date"].unique().sort().to_list()
    if not dates or max(dates) > date(2016, 12, 31):
        raise ValueError("execution panel outside research period")
    if target_weights.filter(pl.col("date") > date(2016, 12, 31)).height:
        raise ValueError("target weights outside research period")

    by_date: dict[date, dict[str, dict[str, object]]] = {}
    for row in execution_panel.sort("date", "symbol").iter_rows(named=True):
        by_date.setdefault(row["date"], {})[row["symbol"]] = row
    signals = {
        key[0]: {row["symbol"]: row["target_weight"] for row in group.to_dicts()}
        for key, group in target_weights.group_by("date", maintain_order=True)
    }
    execution_targets: dict[date, tuple[date, dict[str, float]]] = {}
    for signal_date, weights in signals.items():
        next_date = next((value for value in dates if value > signal_date), None)
        if next_date is not None:
            execution_targets[next_date] = (signal_date, weights)
    actions_by_ex_date = {
        key[0]: group.to_dicts()
        for key, group in corporate_actions.group_by("ex_date", maintain_order=True)
    }
    actions_by_payment_date = {
        key[0]: group.to_dicts()
        for key, group in corporate_actions.group_by(
            "effective_date", maintain_order=True
        )
    }
    security_events_by_date = (
        {
            key[0]: group.to_dicts()
            for key, group in security_events.group_by("effective_date", maintain_order=True)
        }
        if security_events is not None
        else {}
    )

    ledgers = {
        name: Ledger(initial_cash=settings.initial_cash)
        for name in ("full_cost", "explicit_fee_only", "zero_cost")
    }
    ledger = ledgers["full_cost"]
    pending: list[Order] = []
    all_orders: dict[str, Order] = {}
    last_close: dict[str, float] = {}
    last_price_date: dict[str, date] = {}
    events: list[dict[str, object]] = []
    trades: list[dict[str, object]] = []
    cash_rows: list[dict[str, object]] = []
    receivable_rows: list[dict[str, object]] = []
    position_rows: list[dict[str, object]] = []
    nav_rows: list[dict[str, object]] = []
    reconciliation_rows: list[dict[str, object]] = []
    scenario_reconciliation_rows: list[dict[str, object]] = []
    target_diagnostic_rows: list[dict[str, object]] = []
    order_sequence = 0
    trade_sequence = 0
    cash_sequence = 0
    previous_nav = settings.initial_cash
    peak_nav = settings.initial_cash
    event_sequences: dict[str, int] = {}
    recognized_dividends: set[str] = set()
    active_target_weights: dict[str, float] | None = None
    active_signal_date: date | None = None

    def record_event(order: Order, event_date: date, reason: str | None = None) -> None:
        event_sequences[order.order_id] = event_sequences.get(order.order_id, 0) + 1
        events.append(
            {
                "order_id": order.order_id,
                "event_seq": event_sequences[order.order_id],
                "date": event_date,
                "status": order.status.value,
                "remaining_quantity": order.remaining_quantity,
                "reason": reason,
            }
        )
        all_orders[order.order_id] = order

    for trade_date in dates:
        market = by_date[trade_date]
        diagnostic_target: dict[str, float] | None = None
        action_symbols: set[str] = set()

        for action in actions_by_ex_date.get(trade_date, []):
            action_symbols.add(action["symbol"])
            quantity = ledger.quantity(action["symbol"])
            if quantity:
                if action["cash_per_share"]:
                    dividend = ledger.recognize_dividend(
                        action["action_id"], action["symbol"], action["cash_per_share"]
                    )
                    for alternate in (
                        ledgers["explicit_fee_only"],
                        ledgers["zero_cost"],
                    ):
                        alternate.recognize_dividend(
                            action["action_id"],
                            action["symbol"],
                            action["cash_per_share"],
                        )
                    recognized_dividends.add(action["action_id"])
                    receivable_rows.append(
                        {
                            "action_id": action["action_id"],
                            "date": trade_date,
                            "event_type": "dividend_receivable",
                            "symbol": action["symbol"],
                            "amount": dividend,
                        }
                    )
                ledger.apply_action(
                    action["symbol"],
                    0.0,
                    action["share_ratio"],
                    trade_date,
                )
                for alternate in (
                    ledgers["explicit_fee_only"],
                    ledgers["zero_cost"],
                ):
                    alternate.apply_action(
                        action["symbol"], 0.0, action["share_ratio"], trade_date
                    )

        for action in actions_by_payment_date.get(trade_date, []):
            if action["action_id"] not in recognized_dividends:
                continue
            dividend = ledger.pay_dividend(action["action_id"])
            for alternate in (ledgers["explicit_fee_only"], ledgers["zero_cost"]):
                alternate.pay_dividend(action["action_id"])
            recognized_dividends.remove(action["action_id"])
            cash_sequence += 1
            cash_rows.append(
                {
                    "cash_event_id": f"c{cash_sequence:09d}",
                    "date": trade_date,
                    "event_type": "cash_dividend",
                    "symbol": action["symbol"],
                    "amount": dividend,
                    "order_id": None,
                }
            )
            receivable_rows.append(
                {
                    "action_id": action["action_id"],
                    "date": trade_date,
                    "event_type": "dividend_payment",
                    "symbol": action["symbol"],
                    "amount": -dividend,
                }
            )

        for event in security_events_by_date.get(trade_date, []):
            source = event["source_symbol"]
            quantity = ledger.quantity(source)
            if not quantity:
                continue
            for order in [item for item in pending if item.symbol == source]:
                cancelled = transition(order, OrderStatus.CANCELLED)
                record_event(cancelled, trade_date, "security_event")
            pending = [item for item in pending if item.symbol != source]
            cash_sequence += 1
            if event["event_type"] == "cash_exit":
                proceeds = ledger.cash_exit(source, event["cash_per_share"])
                for alternate in (
                    ledgers["explicit_fee_only"],
                    ledgers["zero_cost"],
                ):
                    alternate.cash_exit(source, event["cash_per_share"])
                cash_rows.append(
                    {
                        "cash_event_id": f"c{cash_sequence:09d}",
                        "date": trade_date,
                        "event_type": "cash_exit",
                        "symbol": source,
                        "amount": proceeds,
                        "order_id": None,
                    }
                )
            elif event["event_type"] == "stock_merger":
                converted = ledger.exchange_security(
                    source,
                    event["target_symbol"],
                    event["ratio"],
                    trade_date,
                )
                for alternate in (
                    ledgers["explicit_fee_only"],
                    ledgers["zero_cost"],
                ):
                    alternate.exchange_security(
                        source, event["target_symbol"], event["ratio"], trade_date
                    )
                cash_rows.append(
                    {
                        "cash_event_id": f"c{cash_sequence:09d}",
                        "date": trade_date,
                        "event_type": "stock_merger",
                        "symbol": event["target_symbol"],
                        "amount": 0.0,
                        "order_id": None,
                    }
                )
                if converted <= 0:
                    raise ValueError("stock merger produced no shares")
            else:
                raise ValueError(f"unknown security event: {event['event_type']}")

        affected_pending = [item for item in pending if item.symbol in action_symbols]
        if (
            action_symbols
            and trade_date not in execution_targets
            and active_target_weights is not None
            and active_signal_date is not None
            and affected_pending
        ):
            affected_symbols = {item.symbol for item in affected_pending}
            for order in affected_pending:
                cancelled = transition(order, OrderStatus.CANCELLED)
                record_event(cancelled, trade_date, "corporate_action_rebase")
            pending = [item for item in pending if item.symbol not in affected_symbols]
            order_specs = build_target_order_specs(
                ledger,
                active_target_weights,
                market,
                last_close,
                symbols=affected_symbols,
            )
            candidates: list[tuple[int, str, Order]] = []
            for priority, symbol, side, requested, _ in order_specs:
                order_sequence += 1
                order = Order(
                    _order_id(active_signal_date, symbol, side, order_sequence),
                    active_signal_date,
                    symbol,
                    side,
                    requested,
                    requested,
                )
                record_event(order, trade_date)
                order = transition(order, OrderStatus.PENDING)
                record_event(order, trade_date)
                candidates.append((priority, symbol, order))
            pending.extend(item[2] for item in sorted(candidates))

        if trade_date in execution_targets:
            for order in pending:
                cancelled = transition(order, OrderStatus.CANCELLED)
                record_event(cancelled, trade_date, "new_signal")
            pending = []
            signal_date, weights = execution_targets[trade_date]
            active_signal_date = signal_date
            active_target_weights = weights
            diagnostic_target = weights
            order_specs = build_target_order_specs(
                ledger, weights, market, last_close
            )
            candidates: list[tuple[int, str, Order]] = []
            for priority, symbol, side, requested, _ in order_specs:
                quantity = requested
                if quantity <= 0:
                    order_sequence += 1
                    rejected = Order(
                        _order_id(signal_date, symbol, side, order_sequence),
                        signal_date,
                        symbol,
                        side,
                        requested,
                        requested,
                    )
                    record_event(rejected, trade_date)
                    rejected = transition(rejected, OrderStatus.REJECTED)
                    record_event(rejected, trade_date, "insufficient_cash_after_fees")
                    continue
                order_sequence += 1
                order = Order(
                    _order_id(signal_date, symbol, side, order_sequence),
                    signal_date,
                    symbol,
                    side,
                    quantity,
                    quantity,
                )
                record_event(order, trade_date)
                order = transition(order, OrderStatus.PENDING)
                record_event(order, trade_date)
                candidates.append((priority, symbol, order))
            pending = [item[2] for item in sorted(candidates)]

        next_pending: list[Order] = []
        buy_allocations: dict[str, int] | None = None
        for order in sorted(
            pending,
            key=lambda item: (
                0 if item.side == "sell" else 1,
                -item.remaining_quantity,
                item.symbol,
            ),
        ):
            if order.side == "buy" and buy_allocations is None:
                executable: dict[str, tuple[int, float]] = {}
                for candidate in pending:
                    if candidate.side != "buy":
                        continue
                    candidate_row = market.get(candidate.symbol)
                    if (
                        not candidate_row
                        or candidate_row["is_suspended_proxy"]
                        or candidate_row["adv20"] is None
                        or candidate_row["adv20"] <= 0
                    ):
                        continue
                    lower, upper = limit_prices(
                        candidate_row["prev_close_raw"], candidate_row["limit_rate"]
                    )
                    candidate_open = candidate_row["open_raw"]
                    within_band = lower - 0.01 <= candidate_open <= upper + 0.01
                    if within_band and candidate_open >= upper:
                        continue
                    capacity = math.floor(
                        candidate_row["adv20"]
                        * settings.maximum_participation
                        / candidate_open
                    )
                    capacity = capacity // settings.buy_lot_size * settings.buy_lot_size
                    requested = min(candidate.remaining_quantity, capacity)
                    if requested > 0:
                        executable[candidate.symbol] = (requested, candidate_open)
                buy_allocations = proportional_buy_quantities(
                    executable,
                    available_cash=max(
                        0.0, ledger.cash - len(executable) * fees.minimum_commission
                    ),
                    lot_size=settings.buy_lot_size,
                )
            row = market.get(order.symbol)
            reason: str | None = None
            if not row or row["is_suspended_proxy"]:
                reason = "suspended_or_missing_open"
            elif row["adv20"] is None or row["adv20"] <= 0:
                reason = "missing_lagged_adv20"
            else:
                lower, upper = limit_prices(row["prev_close_raw"], row["limit_rate"])
                open_price = row["open_raw"]
                within_standard_band = lower - 0.01 <= open_price <= upper + 0.01
                if order.side == "buy" and within_standard_band and open_price >= upper:
                    reason = "limit_up_buy"
                elif order.side == "sell" and within_standard_band and open_price <= lower:
                    reason = "limit_down_sell"
            if reason:
                record_event(order, trade_date, reason)
                next_pending.append(order)
                continue

            maximum_amount = row["adv20"] * settings.maximum_participation
            maximum_quantity = math.floor(maximum_amount / row["open_raw"])
            if order.side == "buy":
                    maximum_quantity = (
                        maximum_quantity // settings.buy_lot_size * settings.buy_lot_size
                    )
            fill_quantity = min(order.remaining_quantity, maximum_quantity)
            if order.side == "sell":
                fill_quantity = min(
                    fill_quantity, ledger.available_quantity(order.symbol, trade_date)
                )
            if order.side == "buy":
                fill_quantity = min(
                    fill_quantity, (buy_allocations or {}).get(order.symbol, 0)
                )
                affordable = (
                    math.floor(ledger.cash / row["open_raw"] / settings.buy_lot_size)
                    * settings.buy_lot_size
                )
                fill_quantity = min(fill_quantity, affordable)
            if fill_quantity <= 0:
                zero_reason = (
                    "insufficient_cash_after_fees"
                    if order.side == "buy"
                    and (buy_allocations or {}).get(order.symbol, 0) == 0
                    else "capacity_cash_or_t1"
                )
                record_event(order, trade_date, zero_reason)
                next_pending.append(order)
                continue

            priced = execution_price(
                open_price=row["open_raw"],
                side=order.side,
                order_amount=fill_quantity * row["open_raw"],
                adv20=row["adv20"],
                fixed_bps=settings.fixed_slippage_bps,
                reference_impact_bps=settings.reference_impact_bps,
                reference_participation=settings.reference_participation,
                maximum_impact_bps=settings.maximum_impact_bps,
            )
            lower, upper = limit_prices(row["prev_close_raw"], row["limit_rate"])
            if lower <= row["open_raw"] <= upper and not lower <= priced.price <= upper:
                record_event(order, trade_date, "impact_crosses_price_limit")
                next_pending.append(order)
                continue
            market_code = "sh" if order.symbol.startswith("6") else "sz"
            fee = fees.calculate(
                trade_date=trade_date,
                market=market_code,
                side=order.side,
                price=priced.price,
                quantity=fill_quantity,
            )
            explicit = fee.total
            if order.side == "buy":
                while fill_quantity >= settings.buy_lot_size and (
                    fill_quantity * priced.price + explicit > ledger.cash + 1e-8
                ):
                    fill_quantity -= settings.buy_lot_size
                    if fill_quantity == 0:
                        break
                    fee = fees.calculate(
                        trade_date=trade_date,
                        market=market_code,
                        side=order.side,
                        price=priced.price,
                        quantity=fill_quantity,
                    )
                    explicit = fee.total
                if fill_quantity <= 0:
                    record_event(order, trade_date, "insufficient_cash_after_fees")
                    next_pending.append(order)
                    continue
                future = next((value for value in dates if value > trade_date), trade_date)
                ledger.buy(
                    order.symbol,
                    fill_quantity,
                    priced.price,
                    explicit,
                    trade_date,
                    future,
                )
                explicit_at_open = fees.calculate(
                    trade_date=trade_date,
                    market=market_code,
                    side=order.side,
                    price=row["open_raw"],
                    quantity=fill_quantity,
                ).total
                ledgers["explicit_fee_only"].buy(
                    order.symbol,
                    fill_quantity,
                    row["open_raw"],
                    explicit_at_open,
                    trade_date,
                    future,
                )
                ledgers["zero_cost"].buy(
                    order.symbol,
                    fill_quantity,
                    row["open_raw"],
                    0.0,
                    trade_date,
                    future,
                )
                cash_change = -(fill_quantity * priced.price + explicit)
            else:
                ledger.sell(
                    order.symbol, fill_quantity, priced.price, explicit, trade_date
                )
                explicit_at_open = fees.calculate(
                    trade_date=trade_date,
                    market=market_code,
                    side=order.side,
                    price=row["open_raw"],
                    quantity=fill_quantity,
                ).total
                ledgers["explicit_fee_only"].sell(
                    order.symbol,
                    fill_quantity,
                    row["open_raw"],
                    explicit_at_open,
                    trade_date,
                )
                ledgers["zero_cost"].sell(
                    order.symbol,
                    fill_quantity,
                    row["open_raw"],
                    0.0,
                    trade_date,
                )
                cash_change = fill_quantity * priced.price - explicit

            trade_sequence += 1
            cash_sequence += 1
            slip_cost = fill_quantity * row["open_raw"] * priced.fixed_slippage_bps / 10_000
            impact_cost = fill_quantity * row["open_raw"] * priced.impact_bps / 10_000
            trades.append(
                {
                    "trade_id": f"t{trade_sequence:09d}",
                    "order_id": order.order_id,
                    "date": trade_date,
                    "symbol": order.symbol,
                    "side": order.side,
                    "quantity": fill_quantity,
                    "open_price": row["open_raw"],
                    "execution_price": priced.price,
                    "amount": fill_quantity * priced.price,
                    "commission": fee.commission,
                    "stamp_duty": fee.stamp_duty,
                    "transfer_fee": fee.transfer_fee,
                    "slippage_cost": slip_cost,
                    "impact_cost": impact_cost,
                    "total_cost": explicit + slip_cost + impact_cost,
                    "adv20": row["adv20"],
                }
            )
            cash_rows.append(
                {
                    "cash_event_id": f"c{cash_sequence:09d}",
                    "date": trade_date,
                    "event_type": "trade_buy" if order.side == "buy" else "trade_sell",
                    "symbol": order.symbol,
                    "amount": cash_change,
                    "order_id": order.order_id,
                }
            )
            remaining = order.remaining_quantity - fill_quantity
            status = OrderStatus.FILLED if remaining == 0 else OrderStatus.PARTIALLY_FILLED
            updated = transition(order, status, remaining_quantity=remaining)
            record_event(updated, trade_date)
            if remaining:
                next_pending.append(updated)
        pending = next_pending

        for symbol, row in market.items():
            close = row.get("close_raw")
            if (
                close is not None
                and math.isfinite(close)
                and close > 0
                and not row["is_suspended_proxy"]
            ):
                last_close[symbol] = close
                last_price_date[symbol] = trade_date
        prices = {symbol: last_close[symbol] for symbol in ledger._lots if symbol in last_close}
        snapshot = ledger.value(trade_date, prices)
        scenario_snapshots = {
            name: scenario.value(trade_date, prices) for name, scenario in ledgers.items()
        }
        peak_nav = max(peak_nav, snapshot.nav)
        nav_rows.append(
            {
                "date": trade_date,
                "cash": snapshot.cash,
                "receivables": snapshot.receivables,
                "holdings_value": snapshot.holdings_value,
                "nav": snapshot.nav,
                "return": snapshot.nav / previous_nav - 1,
                "drawdown": snapshot.nav / peak_nav - 1,
                "explicit_cost_nav": scenario_snapshots["explicit_fee_only"].nav,
                "zero_cost_nav": scenario_snapshots["zero_cost"].nav,
            }
        )
        previous_nav = snapshot.nav
        reconciliation_rows.append(
            {
                "date": trade_date,
                "cash": snapshot.cash,
                "receivables": snapshot.receivables,
                "holdings_value": snapshot.holdings_value,
                "nav": snapshot.nav,
                "difference": snapshot.nav
                - snapshot.cash
                - snapshot.receivables
                - snapshot.holdings_value,
            }
        )
        for scenario, scenario_snapshot in sorted(scenario_snapshots.items()):
            scenario_reconciliation_rows.append(
                {
                    "date": trade_date,
                    "scenario": scenario,
                    "cash": scenario_snapshot.cash,
                    "receivables": scenario_snapshot.receivables,
                    "holdings_value": scenario_snapshot.holdings_value,
                    "nav": scenario_snapshot.nav,
                    "difference": scenario_snapshot.nav
                    - scenario_snapshot.cash
                    - scenario_snapshot.receivables
                    - scenario_snapshot.holdings_value,
                }
            )
        if diagnostic_target is not None:
            actual_weights = {
                symbol: ledger.quantity(symbol) * last_close[symbol] / snapshot.nav
                for symbol in ledger._lots
                if symbol in last_close and ledger.quantity(symbol)
            }
            deviation = sum(
                abs(actual_weights.get(symbol, 0.0) - diagnostic_target.get(symbol, 0.0))
                for symbol in set(actual_weights) | set(diagnostic_target)
            )
            target_diagnostic_rows.append(
                {
                    "date": trade_date,
                    "target_deviation_l1": deviation,
                    "cash_weight": snapshot.cash / snapshot.nav,
                }
            )
        for symbol in sorted(ledger._lots):
            quantity = ledger.quantity(symbol)
            if quantity:
                price = last_close[symbol]
                position_rows.append(
                    {
                        "date": trade_date,
                        "symbol": symbol,
                        "quantity": quantity,
                        "available_quantity": ledger.available_quantity(symbol, trade_date),
                        "close_price": price,
                        "market_value": quantity * price,
                        "is_stale": symbol not in market
                        or bool(market[symbol]["is_suspended_proxy"]),
                        "stale_days": (trade_date - last_price_date[symbol]).days,
                    }
                )

    for order in pending:
        expired = transition(order, OrderStatus.EXPIRED)
        record_event(expired, dates[-1], "end_of_backtest")

    orders_rows = [
        {
            "order_id": order.order_id,
            "signal_date": order.signal_date,
            "symbol": order.symbol,
            "side": order.side,
            "quantity": order.quantity,
            "remaining_quantity": order.remaining_quantity,
            "status": order.status.value,
        }
        for order in all_orders.values()
    ]
    result = build_output_frames(
        {
            "orders": orders_rows,
            "order_events": events,
            "trades": trades,
            "cash_ledger": cash_rows,
            "receivable_ledger": receivable_rows,
            "positions": position_rows,
            "nav": nav_rows,
            "reconciliation": reconciliation_rows,
            "scenario_reconciliation": scenario_reconciliation_rows,
            "target_diagnostics": target_diagnostic_rows,
        }
    )
    if result["reconciliation"]["difference"].abs().max() > 0.01:
        raise ValueError("daily ledger reconciliation failed")
    if result["scenario_reconciliation"]["difference"].abs().max() > 0.01:
        raise ValueError("scenario ledger reconciliation failed")
    return result


def _zero_fee_schedule(fees: FeeSchedule) -> FeeSchedule:
    return replace(
        fees,
        stamp_duty=tuple(
            replace(rule, buy_rate=0.0, sell_rate=0.0) for rule in fees.stamp_duty
        ),
        transfer_fees=tuple(
            replace(rule, rate=0.0, minimum=0.0) for rule in fees.transfer_fees
        ),
        commission_rate=0.0,
        minimum_commission=0.0,
    )


def run_backtest(
    execution_panel: pl.DataFrame,
    target_weights: pl.DataFrame,
    corporate_actions: pl.DataFrame,
    fees: FeeSchedule,
    settings: BacktestSettings,
    security_events: pl.DataFrame | None = None,
) -> dict[str, pl.DataFrame]:
    """Run three independently compounded execution and accounting scenarios."""
    no_market_cost = replace(
        settings,
        fixed_slippage_bps=0.0,
        reference_impact_bps=0.0,
        maximum_impact_bps=0.0,
    )
    runs = {
        "full_cost": _run_backtest_once(
            execution_panel,
            target_weights,
            corporate_actions,
            fees,
            settings,
            security_events,
        ),
        "explicit_fee_only": _run_backtest_once(
            execution_panel,
            target_weights,
            corporate_actions,
            fees,
            no_market_cost,
            security_events,
        ),
        "zero_cost": _run_backtest_once(
            execution_panel,
            target_weights,
            corporate_actions,
            _zero_fee_schedule(fees),
            no_market_cost,
            security_events,
        ),
    }
    result = dict(runs["full_cost"])
    scenario_outputs = (
        "orders",
        "order_events",
        "trades",
        "cash_ledger",
        "receivable_ledger",
        "positions",
        "nav",
        "reconciliation",
        "target_diagnostics",
    )
    for name in scenario_outputs:
        frames_with_scenario = [
                frame.with_columns(pl.lit(scenario).alias("scenario"))
                for scenario, frames in runs.items()
                if not (frame := frames[name]).is_empty()
            ]
        result[f"scenario_{name}"] = (
            pl.concat(frames_with_scenario, how="diagonal_relaxed")
            if frames_with_scenario
            else runs["full_cost"][name].with_columns(
                pl.lit(None, dtype=pl.String).alias("scenario")
            )
        )
    scenario_nav = result["scenario_nav"]
    comparison = (
        scenario_nav.select("date", "scenario", "nav")
        .pivot(on="scenario", index="date", values="nav")
        .rename(
            {
                "explicit_fee_only": "explicit_cost_nav",
                "zero_cost": "zero_cost_nav",
            }
        )
    )
    result["nav"] = (
        runs["full_cost"]["nav"]
        .drop("explicit_cost_nav", "zero_cost_nav")
        .join(comparison, on="date", how="left")
    )
    result["scenario_reconciliation"] = result["scenario_reconciliation"].drop(
        "scenario_right", strict=False
    ).sort("date", "scenario")
    return result
