from datetime import date
from pathlib import Path

import polars as pl

from ashare_multifactor.robustness.protocol import load_robustness_protocol
from ashare_multifactor.robustness.report import render_robustness_report
from ashare_multifactor.robustness.summary import (
    assess_test_protocol_gate,
    build_robustness_long_table,
)


def test_long_table_keeps_sensitive_failed_and_missing_experiments() -> None:
    protocol = load_robustness_protocol(Path("configs/robustness_protocol.yaml"))
    results = [
        {
            "experiment_id": "execution_full_cost",
            "status": "ready",
            "sample": "2005-2021",
            "observations": 4000,
            "metrics": {"annual_return": 0.05, "maximum_drawdown": -0.20},
        },
        {
            "experiment_id": "execution_slippage_high",
            "status": "ready",
            "sample": "2005-2021",
            "observations": 4000,
            "metrics": {"annual_return": 0.01, "maximum_drawdown": -0.30},
        },
        {
            "experiment_id": "portfolio_universe_high",
            "status": "unavailable",
            "sample": "2005-2021",
            "observations": 0,
            "reason": "requested universe exceeds frozen upstream",
            "metrics": {},
        },
    ]

    table = build_robustness_long_table(results, protocol)

    assert table.filter(
        (pl.col("experiment_id") == "execution_slippage_high")
        & (pl.col("interpretation") == "sensitive")
    ).height == 2
    failed = table.filter(pl.col("experiment_id") == "portfolio_universe_high")
    assert failed.item(0, "interpretation") == "failed"
    assert "requested universe" in failed.item(0, "reason")
    missing = table.filter(pl.col("experiment_id") == "execution_impact_low")
    assert missing.item(0, "status") == "not_run"
    assert table.get_column("experiment_id").n_unique() == len(protocol.experiments)


def test_fatal_state_blocks_test_protocol_and_report_discloses_failures() -> None:
    protocol = load_robustness_protocol(Path("configs/robustness_protocol.yaml"))
    table = build_robustness_long_table([], protocol)
    gate = assess_test_protocol_gate(
        table,
        fatal_events=[{"status": "accounting_failure", "reason": "ledger mismatch"}],
        protocol=protocol,
    )
    report = render_robustness_report(table, gate, protocol)

    assert gate["sealed_test_protocol_allowed"] is False
    assert gate["status"] == "fatal_defect"
    assert "ledger mismatch" in report
    assert "失败或未运行" in report


def test_long_table_infers_late_failure_reason_after_one_hundred_metric_rows() -> None:
    protocol = load_robustness_protocol(Path("configs/robustness_protocol.yaml"))
    metric_names = [
        "annual_return",
        "annual_volatility",
        "maximum_drawdown",
        "turnover",
        "cost_erosion",
        "unfilled_rate",
        "target_deviation",
        "total_cost_rate",
    ]
    results = [
        {
            "experiment_id": experiment.experiment_id,
            "status": "ready",
            "sample": "2005-2021",
            "start_date": date(2005, 1, 4),
            "end_date": date(2021, 12, 31),
            "observations": 4132,
            "metrics": {name: float(index) for index, name in enumerate(metric_names)},
        }
        for experiment in protocol.experiments
        if experiment.category == "execution"
    ]
    results.append(
        {
            "experiment_id": "portfolio_universe_high",
            "status": "unavailable",
            "sample": "2005-2021",
            "observations": 0,
            "reason": "requested universe exceeds frozen upstream",
            "metrics": {},
        }
    )

    table = build_robustness_long_table(results, protocol)

    assert (
        table.filter(pl.col("experiment_id") == "portfolio_universe_high").item(
            0, "reason"
        )
        == "requested universe exceeds frozen upstream"
    )


def test_fatal_status_in_result_table_blocks_sealing() -> None:
    protocol = load_robustness_protocol(Path("configs/robustness_protocol.yaml"))
    results = [
        {
            "experiment_id": "portfolio_size_low",
            "status": "implementation_bug",
            "sample": "2005-2021",
            "observations": 0,
            "reason": "unexpected schema",
            "metrics": {},
        }
    ]
    table = build_robustness_long_table(results, protocol)

    gate = assess_test_protocol_gate(table, fatal_events=[], protocol=protocol)

    assert gate["status"] == "fatal_defect"
    assert gate["sealed_test_protocol_allowed"] is False
