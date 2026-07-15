from dataclasses import replace
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from ashare_multifactor.execution.broker import BacktestSettings
from ashare_multifactor.execution.fees import FeeSchedule, _DatedRate, _TransferRule
from ashare_multifactor.robustness.execution_sensitivity import (
    build_execution_scenarios,
    summarize_execution_result,
)
from ashare_multifactor.robustness.protocol import load_robustness_protocol


def _fees() -> FeeSchedule:
    return FeeSchedule(
        (_DatedRate(date(2005, 1, 1), date(2021, 12, 31), 0.0, 0.001),),
        (
            _TransferRule(
                date(2005, 1, 1), date(2021, 12, 31), market, "amount", 0.0, 0.0
            )
            for market in ("sh", "sz")
        ),
        0.0003,
        5.0,
    )


def test_execution_scenarios_change_only_registered_assumption() -> None:
    protocol = load_robustness_protocol(Path("configs/robustness_protocol.yaml"))
    settings = BacktestSettings(analysis_end=date(2021, 12, 31))
    scenarios = {
        item.experiment_id: item
        for item in build_execution_scenarios(protocol, settings, _fees())
    }

    assert scenarios["execution_full_cost"].settings == settings
    assert scenarios["execution_slippage_low"].settings == replace(
        settings, fixed_slippage_bps=0.0
    )
    assert scenarios["execution_commission_high"].fees.commission_rate == 0.0005
    assert scenarios["execution_commission_high"].settings == settings
    assert scenarios["execution_zero_cost"].result_scenario == "zero_cost"
    assert scenarios["execution_explicit_only"].result_scenario == "explicit_fee_only"


def test_execution_summary_reports_joint_cost_and_fill_metrics() -> None:
    nav = pl.DataFrame(
        {
            "date": [date(2005, 1, 3), date(2021, 12, 31)],
            "nav": [100.0, 200.0],
            "zero_cost_nav": [100.0, 220.0],
        }
    )
    result = {
        "nav": nav,
        "trades": pl.DataFrame(
            {
                "amount": [40.0, 20.0],
                "total_cost": [1.0, 0.5],
            }
        ),
        "orders": pl.DataFrame(
            {"quantity": [100, 100], "remaining_quantity": [0, 50]}
        ),
        "target_diagnostics": pl.DataFrame(
            {"target_deviation_l1": [0.1, 0.3]}
        ),
    }

    row = summarize_execution_result("execution_full_cost", result)

    assert row["experiment_id"] == "execution_full_cost"
    assert row["annual_return"] > 0
    assert row["cost_erosion"] > 0
    assert row["turnover"] == pytest.approx(60.0 / (2 * 150.0 * 17.0))
    assert row["unfilled_rate"] == pytest.approx(0.25)
    assert row["target_deviation"] == pytest.approx(0.2)
    assert row["total_cost_rate"] == pytest.approx(1.5 / 60.0)
