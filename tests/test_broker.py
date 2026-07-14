from datetime import date

import polars as pl

from ashare_multifactor.execution.broker import BacktestSettings, run_backtest
from ashare_multifactor.execution.corporate_actions import normalize_corporate_actions
from ashare_multifactor.execution.fees import load_market_rules
from ashare_multifactor.research.formal_backtest_metrics import backtest_summary
from pathlib import Path


RULES = Path(__file__).parents[1] / "configs" / "market_rules.yaml"


def _empty_actions() -> pl.DataFrame:
    return normalize_corporate_actions(
        pl.DataFrame(
            schema={
                "symbol": pl.String,
                "effective_date": pl.Date,
                "cash_per_share": pl.Float64,
                "share_ratio": pl.Float64,
                "source": pl.String,
            }
        )
    )


def test_broker_blocks_limit_buy_then_fills_pending_without_same_day_sell() -> None:
    dates = [date(2010, 1, 4), date(2010, 1, 5), date(2010, 1, 6)]
    panel = pl.DataFrame(
        {
            "date": dates,
            "symbol": ["000001"] * 3,
            "open_raw": [11.0, 10.5, 10.0],
            "close_raw": [11.0, 10.5, 10.0],
            "prev_close_raw": [10.0, 11.0, 10.5],
            "adv20": [1_000_000.0] * 3,
            "limit_rate": [0.10] * 3,
            "is_suspended_proxy": [False] * 3,
        }
    )
    targets = pl.DataFrame(
        {
            "date": [date(2009, 12, 31)],
            "symbol": ["000001"],
            "target_weight": [1.0],
        }
    )
    actions = normalize_corporate_actions(
        pl.DataFrame(
            schema={
                "symbol": pl.String,
                "effective_date": pl.Date,
                "cash_per_share": pl.Float64,
                "share_ratio": pl.Float64,
                "source": pl.String,
            }
        )
    )
    result = run_backtest(
        panel,
        targets,
        actions,
        load_market_rules(RULES),
        BacktestSettings(initial_cash=100_000.0, maximum_participation=0.10),
    )
    assert result["trades"].item(0, "date") == date(2010, 1, 5)
    assert "limit_up_buy" in result["order_events"]["reason"].drop_nulls().to_list()
    assert result["reconciliation"]["difference"].abs().max() < 1e-8


def test_corporate_action_changes_cash_and_shares_before_valuation() -> None:
    panel = pl.DataFrame(
        {
            "date": [date(2010, 1, 4), date(2010, 1, 5), date(2010, 1, 6)],
            "symbol": ["000001"] * 3,
            "open_raw": [10.0, 10.0, 9.0],
            "close_raw": [10.0, 10.0, 9.0],
            "prev_close_raw": [10.0, 10.0, 10.0],
            "adv20": [10_000_000.0] * 3,
            "limit_rate": [0.10] * 3,
            "is_suspended_proxy": [False] * 3,
        }
    )
    targets = pl.DataFrame(
        {"date": [date(2009, 12, 31)], "symbol": ["000001"], "target_weight": [0.5]}
    )
    actions = normalize_corporate_actions(
        pl.DataFrame(
            {
                "symbol": ["000001"],
                "effective_date": [date(2010, 1, 6)],
                "cash_per_share": [0.1],
                "share_ratio": [0.1],
                "source": ["fixture"],
            }
        )
    )
    result = run_backtest(
        panel,
        targets,
        actions,
        load_market_rules(RULES),
        BacktestSettings(initial_cash=100_000.0, maximum_participation=0.10),
    )
    before = result["positions"].filter(pl.col("date") == date(2010, 1, 5)).item(0, "quantity")
    after = result["positions"].filter(pl.col("date") == date(2010, 1, 6)).item(0, "quantity")
    assert after == round(before * 1.1)
    assert result["cash_ledger"].filter(pl.col("event_type") == "cash_dividend").height == 1


def test_cash_dividend_is_receivable_between_ex_and_payment_dates() -> None:
    panel = pl.DataFrame(
        {
            "date": [date(2010, 1, 4), date(2010, 1, 5), date(2010, 1, 6)],
            "symbol": ["000001"] * 3,
            "open_raw": [10.0, 10.0, 9.9],
            "close_raw": [10.0, 10.0, 9.9],
            "prev_close_raw": [10.0, 10.0, 10.0],
            "adv20": [10_000_000.0] * 3,
            "limit_rate": [0.10] * 3,
            "is_suspended_proxy": [False] * 3,
        }
    )
    targets = pl.DataFrame(
        {"date": [date(2009, 12, 31)], "symbol": ["000001"], "target_weight": [0.5]}
    )
    actions = normalize_corporate_actions(
        pl.DataFrame(
            {
                "symbol": ["000001"],
                "ex_date": [date(2010, 1, 5)],
                "effective_date": [date(2010, 1, 6)],
                "cash_per_share": [0.1],
                "share_ratio": [0.0],
                "source": ["fixture"],
            }
        )
    )

    result = run_backtest(
        panel,
        targets,
        actions,
        load_market_rules(RULES),
        BacktestSettings(initial_cash=100_000.0, maximum_participation=0.10),
    )

    ex_nav = result["nav"].filter(pl.col("date") == date(2010, 1, 5)).row(0, named=True)
    pay_nav = result["nav"].filter(pl.col("date") == date(2010, 1, 6)).row(0, named=True)
    assert ex_nav["receivables"] > 0
    assert pay_nav["receivables"] == 0
    assert result["receivable_ledger"]["event_type"].to_list() == [
        "dividend_receivable",
        "dividend_payment",
    ]


def test_cash_shortage_scales_executable_buy_orders_proportionally() -> None:
    panel = pl.DataFrame(
        {
            "date": [date(2010, 1, 4)] * 2,
            "symbol": ["000001", "000002"],
            "open_raw": [10.0, 10.0],
            "close_raw": [10.0, 10.0],
            "prev_close_raw": [10.0, 10.0],
            "adv20": [1_000_000.0, 1_000_000.0],
            "limit_rate": [0.10, 0.10],
            "is_suspended_proxy": [False, False],
        }
    )
    targets = pl.DataFrame(
        {
            "date": [date(2009, 12, 31)] * 2,
            "symbol": ["000001", "000002"],
            "target_weight": [0.6, 0.6],
        }
    )

    result = run_backtest(
        panel,
        targets,
        _empty_actions(),
        load_market_rules(RULES),
        BacktestSettings(initial_cash=18_010.0, fixed_slippage_bps=0.0, reference_impact_bps=0.0),
    )

    quantities = result["trades"].sort("symbol")["quantity"].to_list()
    assert quantities[0] == quantities[1]


def test_three_cost_scenarios_have_independent_daily_reconciliation() -> None:
    panel = pl.DataFrame(
        {
            "date": [date(2010, 1, 4)],
            "symbol": ["000001"],
            "open_raw": [10.0],
            "close_raw": [10.0],
            "prev_close_raw": [10.0],
            "adv20": [1_000_000.0],
            "limit_rate": [0.10],
            "is_suspended_proxy": [False],
        }
    )
    targets = pl.DataFrame(
        {"date": [date(2009, 12, 31)], "symbol": ["000001"], "target_weight": [0.5]}
    )

    result = run_backtest(
        panel,
        targets,
        _empty_actions(),
        load_market_rules(RULES),
        BacktestSettings(initial_cash=100_000.0),
    )

    assert result["scenario_reconciliation"]["scenario"].to_list() == [
        "explicit_fee_only",
        "full_cost",
        "zero_cost",
    ]
    assert result["scenario_reconciliation"]["difference"].abs().max() < 0.01
    row = result["nav"].row(0, named=True)
    assert row["zero_cost_nav"] > row["explicit_cost_nav"] > row["nav"]
    summary = backtest_summary(result, initial_cash=100_000.0)
    assert summary["partially_filled_order_rate"] == 0.0
    assert summary["implementation_shortfall_rate"] > 0
    assert summary["average_target_deviation_l1"] >= 0


def test_buy_rounds_to_zero_when_slippage_and_minimum_fee_exceed_cash() -> None:
    panel = pl.DataFrame(
        {
            "date": [date(2010, 1, 4)],
            "symbol": ["000001"],
            "open_raw": [10.0],
            "close_raw": [10.0],
            "prev_close_raw": [10.0],
            "adv20": [1_000_000.0],
            "limit_rate": [0.10],
            "is_suspended_proxy": [False],
        }
    )
    targets = pl.DataFrame(
        {"date": [date(2009, 12, 31)], "symbol": ["000001"], "target_weight": [1.0]}
    )
    actions = normalize_corporate_actions(
        pl.DataFrame(
            schema={
                "symbol": pl.String,
                "effective_date": pl.Date,
                "cash_per_share": pl.Float64,
                "share_ratio": pl.Float64,
                "source": pl.String,
            }
        )
    )
    result = run_backtest(
        panel,
        targets,
        actions,
        load_market_rules(RULES),
        BacktestSettings(initial_cash=1_000.0),
    )
    assert result["trades"].is_empty()
    assert "insufficient_cash_after_fees" in result["order_events"]["reason"].drop_nulls().to_list()


def test_cost_scenarios_generate_independent_trades_and_positions() -> None:
    panel = pl.DataFrame(
        {
            "date": [date(2010, 1, 4)],
            "symbol": ["000001"],
            "open_raw": [10.0],
            "close_raw": [10.0],
            "prev_close_raw": [10.0],
            "adv20": [1_000_000.0],
            "limit_rate": [0.10],
            "is_suspended_proxy": [False],
        }
    )
    targets = pl.DataFrame(
        {"date": [date(2009, 12, 31)], "symbol": ["000001"], "target_weight": [1.0]}
    )

    result = run_backtest(
        panel,
        targets,
        _empty_actions(),
        load_market_rules(RULES),
        BacktestSettings(initial_cash=1_000.0),
    )

    trades = result["scenario_trades"]
    assert trades.filter(pl.col("scenario") == "full_cost").is_empty()
    assert trades.filter(pl.col("scenario") == "zero_cost").item(0, "quantity") == 100
    positions = result["scenario_positions"]
    assert positions.filter(pl.col("scenario") == "full_cost").is_empty()
    assert positions.filter(pl.col("scenario") == "zero_cost").item(0, "quantity") == 100
