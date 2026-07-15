from __future__ import annotations

from datetime import date

import polars as pl

from ashare_multifactor.execution.broker import BacktestSettings, run_backtest
from ashare_multifactor.execution.fees import FeeSchedule
from ashare_multifactor.validation.protocol import assert_validation_read_allowed


SEALED_TEST_START = date(2022, 1, 1)


def run_validation_backtest(
    execution_panel: pl.DataFrame,
    target_weights: pl.DataFrame,
    corporate_actions: pl.DataFrame,
    fees: FeeSchedule,
    settings: BacktestSettings,
    security_events: pl.DataFrame | None = None,
) -> dict[str, pl.DataFrame]:
    """Run research history and validation as one ledger to preserve continuity."""
    maximum = execution_panel.get_column("date").max()
    if maximum is None:
        raise ValueError("validation execution panel is empty")
    assert_validation_read_allowed(date(2003, 1, 1), maximum)
    if maximum < date(2017, 1, 1):
        raise ValueError("validation execution panel does not reach validation period")
    _reject_sealed_dates(target_weights, ("date",), "target_weights")
    _reject_sealed_dates(
        corporate_actions,
        ("ex_date", "effective_date"),
        "corporate_actions",
    )
    if security_events is not None:
        _reject_sealed_dates(
            security_events,
            ("effective_date",),
            "security_events",
        )
    if target_weights.filter(pl.col("date") < date(2005, 1, 1)).height:
        raise ValueError("validation targets predate the research period")
    return run_backtest(
        execution_panel,
        target_weights,
        corporate_actions,
        fees,
        settings,
        security_events,
    )


def _reject_sealed_dates(
    frame: pl.DataFrame,
    columns: tuple[str, ...],
    name: str,
) -> None:
    for column in columns:
        if column in frame.columns and frame.filter(
            pl.col(column) >= SEALED_TEST_START
        ).height:
            raise ValueError(f"sealed final test date in {name}.{column}")
