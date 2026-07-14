from datetime import date
from pathlib import Path

import polars as pl

from ashare_multifactor.execution.broker import BacktestSettings, run_backtest
from ashare_multifactor.execution.corporate_actions import normalize_corporate_actions
from ashare_multifactor.execution.fees import load_market_rules


RULES = Path(__file__).parents[1] / "configs" / "market_rules.yaml"
SETTINGS = BacktestSettings(
    initial_cash=100_000.0,
    fixed_slippage_bps=0.0,
    reference_impact_bps=0.0,
)


def _panel(dates: list[date], prices: list[float], adv: list[float]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": dates,
            "symbol": ["000001"] * len(dates),
            "open_raw": prices,
            "close_raw": prices,
            "prev_close_raw": prices,
            "adv20": adv,
            "limit_rate": [0.10] * len(dates),
            "is_suspended_proxy": [False] * len(dates),
            "high_raw": [value * 1.01 for value in prices],
            "low_raw": [value * 0.99 for value in prices],
            "amount": [999_999_999.0] * len(dates),
        }
    )


def _actions(rows: list[dict[str, object]]) -> pl.DataFrame:
    return normalize_corporate_actions(pl.DataFrame(rows))


def _run(panel: pl.DataFrame, targets: pl.DataFrame, actions: pl.DataFrame) -> dict[str, pl.DataFrame]:
    return run_backtest(panel, targets, actions, load_market_rules(RULES), SETTINGS)


def test_pending_sell_and_partial_fill_are_rebuilt_after_share_bonus() -> None:
    dates = [date(2010, 1, 4), date(2010, 1, 5), date(2010, 1, 6)]
    targets = pl.DataFrame(
        {
            "date": [date(2009, 12, 31), date(2010, 1, 4)],
            "symbol": ["000001", "000001"],
            "target_weight": [1.0, 0.0],
        }
    )
    actions = _actions(
        [{
            "symbol": "000001", "ex_date": dates[2], "effective_date": dates[2],
            "cash_per_share": 0.0, "share_ratio": 1.0, "source": "fixture",
        }]
    )

    result = _run(_panel(dates, [10.0, 10.0, 5.0], [10_000_000.0, 10_000.0, 10_000_000.0]), targets, actions)

    rebase = result["order_events"].filter(pl.col("reason") == "corporate_action_rebase")
    assert rebase.height == 1
    old_order = rebase.item(0, "order_id")
    assert result["trades"].filter(pl.col("order_id") == old_order)["quantity"].sum() == 100
    replacement = result["orders"].filter(
        (pl.col("side") == "sell") & (pl.col("order_id") != old_order)
    )
    assert replacement.item(0, "quantity") == 19_600
    assert replacement.item(0, "quantity") % 100 == 0


def test_cash_dividend_receivable_is_included_when_pending_is_rebuilt() -> None:
    dates = [date(2010, 1, 4), date(2010, 1, 5)]
    targets = pl.DataFrame(
        {"date": [date(2009, 12, 31)], "symbol": ["000001"], "target_weight": [1.0]}
    )
    actions = _actions(
        [{
            "symbol": "000001", "ex_date": dates[1], "effective_date": date(2010, 1, 6),
            "cash_per_share": 10.0, "share_ratio": 0.0, "source": "fixture",
        }]
    )

    result = _run(_panel(dates, [10.0, 10.0], [10_000.0, 10_000_000.0]), targets, actions)

    old_order = result["order_events"].filter(
        pl.col("reason") == "corporate_action_rebase"
    ).item(0, "order_id")
    replacement = result["orders"].filter(pl.col("order_id") != old_order)
    assert replacement.tail(1).item(0, "quantity") == 9_900
    assert result["receivable_ledger"].item(0, "amount") == 1_000.0


def test_new_signal_wins_over_same_day_corporate_action() -> None:
    dates = [date(2010, 1, 4), date(2010, 1, 5)]
    targets = pl.DataFrame(
        {
            "date": [date(2009, 12, 31), date(2010, 1, 4)],
            "symbol": ["000001", "000001"],
            "target_weight": [1.0, 0.5],
        }
    )
    actions = _actions(
        [{
            "symbol": "000001", "ex_date": dates[1], "effective_date": dates[1],
            "cash_per_share": 0.0, "share_ratio": 1.0, "source": "fixture",
        }]
    )

    result = _run(_panel(dates, [10.0, 5.0], [10_000.0, 10_000_000.0]), targets, actions)

    reasons = result["order_events"]["reason"].drop_nulls().to_list()
    assert "corporate_action_rebase" not in reasons
    assert reasons.count("new_signal") == 1


def test_multiple_same_day_actions_trigger_only_one_rebuild() -> None:
    dates = [date(2010, 1, 4), date(2010, 1, 5)]
    targets = pl.DataFrame(
        {"date": [date(2009, 12, 31)], "symbol": ["000001"], "target_weight": [1.0]}
    )
    actions = _actions(
        [
            {"symbol": "000001", "ex_date": dates[1], "effective_date": dates[1], "cash_per_share": 0.0, "share_ratio": 0.5, "source": "fixture_a"},
            {"symbol": "000001", "ex_date": dates[1], "effective_date": date(2010, 1, 6), "cash_per_share": 1.0, "share_ratio": 0.0, "source": "fixture_b"},
        ]
    )

    result = _run(_panel(dates, [10.0, 7.0], [10_000.0, 10_000_000.0]), targets, actions)

    assert result["order_events"].filter(
        pl.col("reason") == "corporate_action_rebase"
    ).height == 1


def test_rebuild_ignores_future_close_high_low_and_current_amount() -> None:
    dates = [date(2010, 1, 4), date(2010, 1, 5)]
    targets = pl.DataFrame(
        {"date": [date(2009, 12, 31)], "symbol": ["000001"], "target_weight": [1.0]}
    )
    actions = _actions(
        [{
            "symbol": "000001", "ex_date": dates[1], "effective_date": dates[1],
            "cash_per_share": 0.0, "share_ratio": 1.0, "source": "fixture",
        }]
    )
    base = _panel(dates, [10.0, 5.0], [10_000.0, 10_000_000.0])
    changed = base.with_columns(
        pl.when(pl.col("date") == dates[1]).then(500.0).otherwise(pl.col("close_raw")).alias("close_raw"),
        pl.when(pl.col("date") == dates[1]).then(900.0).otherwise(pl.col("high_raw")).alias("high_raw"),
        pl.when(pl.col("date") == dates[1]).then(0.01).otherwise(pl.col("low_raw")).alias("low_raw"),
        pl.when(pl.col("date") == dates[1]).then(1.0).otherwise(pl.col("amount")).alias("amount"),
    )

    left = _run(base, targets, actions)["orders"].select("side", "quantity")
    right = _run(changed, targets, actions)["orders"].select("side", "quantity")
    assert left.equals(right)


def _mixed_panel(dates: list[date], *, reverse: bool = False) -> pl.DataFrame:
    rows = []
    for value in dates:
        daily = [
            {
                "date": value, "symbol": "000001", "open_raw": 10.0,
                "close_raw": 10.0, "prev_close_raw": 10.0,
                "adv20": 10_000.0 if value == dates[0] else 10_000_000.0,
                "limit_rate": 0.10, "is_suspended_proxy": False,
            },
            {
                "date": value, "symbol": "000002", "open_raw": 10.0,
                "close_raw": 10.0, "prev_close_raw": 10.0,
                "adv20": 10_000_000.0, "limit_rate": 0.10,
                "is_suspended_proxy": False,
            },
        ]
        rows.extend(reversed(daily) if reverse else daily)
    return pl.DataFrame(rows)


def test_mixed_symbol_actions_rebuild_only_symbol_with_pending_buy() -> None:
    dates = [date(2010, 1, 4), date(2010, 1, 5)]
    targets = pl.DataFrame(
        {
            "date": [date(2009, 12, 31)] * 2,
            "symbol": ["000001", "000002"],
            "target_weight": [0.5, 0.5],
        }
    )
    actions = _actions(
        [
            {"symbol": "000001", "ex_date": dates[1], "effective_date": dates[1], "cash_per_share": 0.0, "share_ratio": 1.0, "source": "fixture_a"},
            {"symbol": "000002", "ex_date": dates[1], "effective_date": date(2010, 1, 6), "cash_per_share": 10.0, "share_ratio": 0.0, "source": "fixture_b"},
        ]
    )

    normal = _run(_mixed_panel(dates), targets, actions)
    reversed_result = _run(_mixed_panel(dates, reverse=True), targets, actions)

    for result in (normal, reversed_result):
        rebase_symbols = (
            result["order_events"]
            .filter(pl.col("reason") == "corporate_action_rebase")
            .join(result["orders"].select("order_id", "symbol"), on="order_id")
            ["symbol"]
            .to_list()
        )
        assert rebase_symbols == ["000001"]
        symbol_b_orders = result["orders"].filter(pl.col("symbol") == "000002")
        assert symbol_b_orders.height == 1
        assert result["trades"].filter(
            (pl.col("symbol") == "000002") & (pl.col("date") == dates[1])
        ).is_empty()
    assert normal["orders"].sort("order_id").equals(
        reversed_result["orders"].sort("order_id")
    )


def test_mixed_symbol_actions_rebuild_only_symbol_with_pending_sell() -> None:
    dates = [date(2010, 1, 4), date(2010, 1, 5), date(2010, 1, 6)]
    rows = []
    for value in dates:
        for symbol in ("000001", "000002"):
            rows.append(
                {
                    "date": value, "symbol": symbol, "open_raw": 10.0,
                    "close_raw": 10.0, "prev_close_raw": 10.0,
                    "adv20": 10_000.0 if value == dates[1] and symbol == "000001" else 10_000_000.0,
                    "limit_rate": 0.10, "is_suspended_proxy": False,
                }
            )
    targets = pl.DataFrame(
        {
            "date": [date(2009, 12, 31)] * 2 + [dates[0]] * 2,
            "symbol": ["000001", "000002"] * 2,
            "target_weight": [0.5, 0.5, 0.0, 0.5],
        }
    )
    actions = _actions(
        [
            {"symbol": "000001", "ex_date": dates[2], "effective_date": dates[2], "cash_per_share": 0.0, "share_ratio": 1.0, "source": "fixture_a"},
            {"symbol": "000002", "ex_date": dates[2], "effective_date": date(2010, 1, 7), "cash_per_share": 10.0, "share_ratio": 0.0, "source": "fixture_b"},
        ]
    )

    result = _run(pl.DataFrame(rows), targets, actions)

    rebased = (
        result["order_events"]
        .filter(pl.col("reason") == "corporate_action_rebase")
        .join(result["orders"].select("order_id", "symbol", "side"), on="order_id")
    )
    assert rebased.select("symbol", "side").row(0) == ("000001", "sell")
    assert result["orders"].filter(pl.col("symbol") == "000002").height == 1
    assert result["trades"].filter(
        (pl.col("symbol") == "000002") & (pl.col("date") == dates[2])
    ).is_empty()
