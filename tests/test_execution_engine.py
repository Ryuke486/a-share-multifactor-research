from datetime import date

import pytest

from ashare_multifactor.execution.orders import Order, OrderStatus, transition
from ashare_multifactor.execution.sizing import (
    proportional_buy_quantities,
    target_quantity,
)
from ashare_multifactor.execution.slippage import execution_price
from ashare_multifactor.execution.ledger import Ledger


def test_buy_sizing_rounds_down_to_board_lot_and_full_exit_sells_odd_lot() -> None:
    assert target_quantity(10_000.0, 10.0, current_quantity=0) == 1_000
    assert target_quantity(10_999.0, 10.0, current_quantity=0) == 1_000
    assert target_quantity(0.0, 10.0, current_quantity=1_037) == 0


def test_cash_shortage_scales_buys_proportionally_then_assigns_residual_stably() -> None:
    result = proportional_buy_quantities(
        {"000001": (1_000, 10.0), "000002": (2_000, 10.0)},
        available_cash=15_000.0,
        lot_size=100,
    )

    assert result == {"000001": 500, "000002": 1_000}


def test_cash_scaling_residual_prefers_larger_unfilled_notional_then_symbol() -> None:
    result = proportional_buy_quantities(
        {"000002": (200, 10.0), "000001": (200, 10.0)},
        available_cash=3_000.0,
        lot_size=100,
    )

    assert result == {"000001": 200, "000002": 100}


def test_order_state_machine_rejects_illegal_transition() -> None:
    order = Order("o1", date(2010, 1, 4), "000001", "buy", 100, 100)
    pending = transition(order, OrderStatus.PENDING)
    assert transition(pending, OrderStatus.FILLED).status is OrderStatus.FILLED
    with pytest.raises(ValueError, match="illegal order transition"):
        transition(order, OrderStatus.FILLED)


def test_slippage_and_impact_are_adverse_and_capacity_uses_lagged_adv() -> None:
    buy = execution_price(
        open_price=10.0,
        side="buy",
        order_amount=10_000.0,
        adv20=1_000_000.0,
        fixed_bps=5.0,
        reference_impact_bps=10.0,
        reference_participation=0.01,
        maximum_impact_bps=50.0,
    )
    sell = execution_price(
        open_price=10.0,
        side="sell",
        order_amount=10_000.0,
        adv20=1_000_000.0,
        fixed_bps=5.0,
        reference_impact_bps=10.0,
        reference_participation=0.01,
        maximum_impact_bps=50.0,
    )
    assert buy.price == 10.02
    assert sell.price == 9.99


def test_ledger_enforces_t_plus_one_and_reconciles() -> None:
    ledger = Ledger(initial_cash=100_000.0)
    ledger.buy("000001", 1_000, 10.0, 5.0, date(2010, 1, 4), date(2010, 1, 5))
    assert ledger.available_quantity("000001", date(2010, 1, 4)) == 0
    with pytest.raises(ValueError, match="exceeds available"):
        ledger.sell("000001", 100, 10.0, 5.0, date(2010, 1, 4))
    ledger.sell("000001", 100, 10.0, 5.0, date(2010, 1, 5))
    snapshot = ledger.value(date(2010, 1, 5), {"000001": 10.0})
    assert snapshot.nav == snapshot.cash + snapshot.holdings_value
    assert ledger.quantity("000001") == 900


def test_ledger_handles_cash_exit_and_stock_merger() -> None:
    ledger = Ledger(initial_cash=100_000.0)
    ledger.buy("000515", 1_000, 10.0, 5.0, date(2009, 1, 1), date(2009, 1, 2))
    ledger.exchange_security("000515", "000629", 1.78, date(2009, 5, 6))
    assert ledger.quantity("000515") == 0
    assert ledger.quantity("000629") == 1_780
    ledger.cash_exit("000629", 9.0)
    assert ledger.quantity("000629") == 0
    assert ledger.cash == 106_015.0


def test_dividend_is_receivable_on_ex_date_and_cash_only_on_payment_date() -> None:
    ledger = Ledger(initial_cash=100_000.0)
    ledger.buy("000001", 1_000, 10.0, 5.0, date(2010, 6, 1), date(2010, 6, 2))

    amount = ledger.recognize_dividend("d1", "000001", 0.2)

    assert amount == 200.0
    assert ledger.cash == 89_995.0
    before_payment = ledger.value(date(2010, 6, 2), {"000001": 9.8})
    assert before_payment.receivables == 200.0
    assert before_payment.nav == 99_995.0

    ledger.pay_dividend("d1")
    after_payment = ledger.value(date(2010, 6, 8), {"000001": 9.8})
    assert after_payment.receivables == 0.0
    assert after_payment.cash == 90_195.0
    assert after_payment.nav == before_payment.nav
