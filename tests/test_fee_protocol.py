from datetime import date
from pathlib import Path

import pytest

from ashare_multifactor.execution.fee_protocol import validate_fee_protocol
from ashare_multifactor.execution.fees import load_market_rules


def test_final_fee_protocol_covers_both_policy_switches() -> None:
    schedule = load_market_rules(Path("configs/market_rules.yaml"))

    evidence = validate_fee_protocol(
        schedule,
        date(2022, 1, 1),
        date(2025, 12, 31),
    )

    assert schedule.transfer_fee(
        date(2022, 4, 28), "sh", 1_000_000.0, 10_000
    ) == pytest.approx(20.0)
    assert schedule.transfer_fee(
        date(2022, 4, 29), "sh", 1_000_000.0, 10_000
    ) == pytest.approx(10.0)
    assert schedule.transfer_fee(
        date(2022, 4, 28), "sz", 1_000_000.0, 10_000
    ) == pytest.approx(20.0)
    assert schedule.transfer_fee(
        date(2022, 4, 29), "sz", 1_000_000.0, 10_000
    ) == pytest.approx(10.0)
    assert schedule.stamp_duty_rate(date(2023, 8, 27), "sell") == pytest.approx(
        0.001
    )
    assert schedule.stamp_duty_rate(date(2023, 8, 28), "sell") == pytest.approx(
        0.0005
    )
    assert evidence.coverage_start == date(2022, 1, 1)
    assert evidence.coverage_end == date(2025, 12, 31)
    assert evidence.markets == ("sh", "sz")
    assert evidence.checked_days == 1_461


def test_fee_protocol_rejects_incomplete_market_set() -> None:
    schedule = load_market_rules(Path("configs/market_rules.yaml"))

    with pytest.raises(ValueError, match="both sh and sz"):
        validate_fee_protocol(
            schedule,
            date(2022, 1, 1),
            date(2025, 12, 31),
            markets=("sh",),
        )
