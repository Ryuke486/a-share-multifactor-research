from datetime import date
from pathlib import Path

import pytest

from ashare_multifactor.execution.fees import FeeSchedule, load_market_rules
from ashare_multifactor.execution.price_limits import limit_prices


RULES = Path(__file__).parents[1] / "configs" / "market_rules.yaml"


@pytest.fixture(scope="module")
def schedule() -> FeeSchedule:
    return load_market_rules(RULES)


@pytest.mark.parametrize(
    ("trade_date", "side", "expected"),
    [
        (date(2005, 1, 23), "buy", 0.002),
        (date(2005, 1, 24), "buy", 0.001),
        (date(2007, 5, 29), "sell", 0.001),
        (date(2007, 5, 30), "sell", 0.003),
        (date(2008, 4, 23), "buy", 0.003),
        (date(2008, 4, 24), "buy", 0.001),
        (date(2008, 9, 18), "buy", 0.001),
        (date(2008, 9, 19), "buy", 0.0),
        (date(2008, 9, 19), "sell", 0.001),
        (date(2016, 12, 30), "sell", 0.001),
    ],
)
def test_stamp_duty_boundaries(
    schedule: FeeSchedule, trade_date: date, side: str, expected: float
) -> None:
    assert schedule.stamp_duty_rate(trade_date, side) == expected


def test_transfer_fee_historical_boundaries(schedule: FeeSchedule) -> None:
    assert schedule.transfer_fee(date(2012, 5, 31), "sh", 10_000.0, 1_000) == 1.0
    assert schedule.transfer_fee(date(2012, 6, 1), "sh", 10_000.0, 1_000) == 0.375
    assert schedule.transfer_fee(date(2012, 9, 1), "sh", 10_000.0, 1_000) == 0.3
    assert schedule.transfer_fee(date(2015, 7, 31), "sz", 10_000.0, 1_000) == 0.255
    assert schedule.transfer_fee(date(2015, 8, 1), "sz", 10_000.0, 1_000) == 0.2


def test_explicit_fee_components_are_hand_calculable(schedule: FeeSchedule) -> None:
    fees = schedule.calculate(
        trade_date=date(2010, 1, 4),
        market="sh",
        side="sell",
        price=10.0,
        quantity=1_000,
    )
    assert fees.commission == 5.0
    assert fees.stamp_duty == 10.0
    assert fees.transfer_fee == 1.0
    assert fees.total == 16.0


def test_market_rules_reject_unknown_market(schedule: FeeSchedule) -> None:
    with pytest.raises(ValueError, match="unknown market"):
        schedule.transfer_fee(date(2010, 1, 4), "bj", 10_000.0, 1_000)


def test_limit_prices_use_decimal_half_up_rounding() -> None:
    assert limit_prices(10.05, 0.10) == (9.05, 11.06)
    assert limit_prices(10.05, 0.05) == (9.55, 10.55)

