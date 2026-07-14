from datetime import date

import polars as pl
import pytest

from ashare_multifactor.execution.calendar import next_trading_dates
from ashare_multifactor.execution.corporate_actions import (
    apply_corporate_actions,
    normalize_corporate_actions,
)
from ashare_multifactor.execution.market_state import build_execution_panel


def test_next_trading_date_uses_market_calendar_not_natural_day() -> None:
    result = next_trading_dates(
        [date(2016, 12, 29), date(2016, 12, 30), date(2017, 1, 3)],
        [date(2016, 12, 30)],
        research_end=date(2016, 12, 31),
    )
    assert result.item(0, "execution_date") is None


def test_calendar_rejects_post_research_inputs() -> None:
    with pytest.raises(ValueError, match="outside research period"):
        next_trading_dates(
            [date(2016, 12, 30), date(2017, 1, 3)],
            [date(2017, 1, 1)],
            research_end=date(2016, 12, 31),
        )


def test_adv20_is_strictly_lagged() -> None:
    dates = [date(2010, 1, day) for day in range(1, 23)]
    frame = pl.DataFrame(
        {
            "date": dates,
            "symbol": ["000001"] * len(dates),
            "open_raw": [10.0] * len(dates),
            "close_raw": [10.0] * len(dates),
            "prev_close_raw": [10.0] * len(dates),
            "amount": list(map(float, range(1, 23))),
            "is_st": [False] * len(dates),
        }
    )
    panel = build_execution_panel(frame)
    assert panel.item(20, "adv20") == 10.5
    changed = frame.with_columns(
        pl.when(pl.col("date") == dates[20])
        .then(1_000_000.0)
        .otherwise(pl.col("amount"))
        .alias("amount")
    )
    assert build_execution_panel(changed).item(20, "adv20") == 10.5


def test_adv_window_uses_market_days_including_missing_security_rows() -> None:
    market_dates = [date(2010, 1, day) for day in range(1, 23)]
    frame = pl.DataFrame(
        {
            "date": market_dates + [market_dates[0], market_dates[-1]],
            "symbol": ["000001"] * len(market_dates) + ["000002", "000002"],
            "open_raw": [10.0] * (len(market_dates) + 2),
            "close_raw": [10.0] * (len(market_dates) + 2),
            "prev_close_raw": [10.0] * (len(market_dates) + 2),
            "amount": [1.0] * len(market_dates) + [1_000_000.0, 10.0],
            "is_st": [False] * (len(market_dates) + 2),
        }
    )

    panel = build_execution_panel(frame, adv_lookback=20)

    assert panel.filter(
        (pl.col("symbol") == "000002") & (pl.col("date") == market_dates[-1])
    ).item(0, "adv20") is None


def test_corporate_actions_apply_once_on_effective_date() -> None:
    actions = normalize_corporate_actions(
        pl.DataFrame(
            {
                "symbol": ["000001"],
                "effective_date": [date(2010, 6, 2)],
                "cash_per_share": [0.2],
                "share_ratio": [0.1],
                "source": ["fixture"],
            }
        )
    )
    positions, cash = apply_corporate_actions(
        {"000001": 1_000}, 10_000.0, actions, date(2010, 6, 2), applied_ids=set()
    )
    assert positions == {"000001": 1_100}
    assert cash == 10_200.0
    with pytest.raises(ValueError, match="already applied"):
        apply_corporate_actions(
            positions,
            cash,
            actions,
            date(2010, 6, 2),
            applied_ids=set(actions["action_id"]),
        )
