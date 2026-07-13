from dataclasses import replace
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from ashare_multifactor.config import MvpSettings, Paths, Period, ResearchConfig
from ashare_multifactor.research.backtest import run_backtest


def _config(*, transaction_cost_bps: float = 10.0) -> ResearchConfig:
    return ResearchConfig(
        paths=Paths(
            raw_unadjusted=Path("raw"),
            raw_backward_adjusted=Path("adj"),
            processed=Path("processed"),
            artifacts=Path("artifacts"),
        ),
        research=Period(date(2005, 1, 1), date(2016, 12, 31)),
        validation=Period(date(2017, 1, 1), date(2021, 12, 31)),
        test=Period(date(2022, 1, 1), date(2025, 12, 31)),
        smoke_data=Period(date(2012, 1, 1), date(2015, 12, 31)),
        smoke_analysis=Period(date(2014, 1, 1), date(2015, 12, 31)),
        mvp=MvpSettings(25, 60, 20, 20, transaction_cost_bps, 1_000_000.0),
    )


def _single_stock_prices(
    *, execution_open: float = 10.0, execution_close: float | None = None
) -> pl.DataFrame:
    execution_close = execution_open if execution_close is None else execution_close
    return pl.DataFrame(
        {
            "date": [date(2014, 1, 2), date(2014, 1, 3)],
            "symbol": ["000001", "000001"],
            "open_adj": [10.0, execution_open],
            "close_adj": [10.0, execution_close],
        }
    )


def _single_stock_target() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "signal_date": [date(2014, 1, 2)],
            "execution_date": [date(2014, 1, 3)],
            "symbol": ["000001"],
            "target_weight": [1.0],
        }
    )


def test_signal_trades_only_on_next_observed_session_open() -> None:
    result = run_backtest(
        _single_stock_prices(execution_open=11.0, execution_close=12.0),
        _single_stock_target(),
        _config(),
    )

    signal_date = date(2014, 1, 2)
    execution_date = date(2014, 1, 3)
    assert result.trades.filter(pl.col("execution_date") == signal_date).is_empty()
    assert result.holdings.filter(pl.col("date") == signal_date).is_empty()
    assert result.trades.get_column("execution_date").min() == execution_date
    assert result.trades.get_column("price").item() == pytest.approx(11.0)
    assert result.holdings.get_column("close_price").item() == pytest.approx(12.0)


def test_initial_trade_uses_maximum_feasible_funding_base() -> None:
    config = _config()
    cost_result = run_backtest(
        _single_stock_prices(),
        _single_stock_target(),
        config,
    )
    zero_cost_result = run_backtest(
        _single_stock_prices(),
        _single_stock_target(),
        replace(config, mvp=replace(config.mvp, transaction_cost_bps=0.0)),
    )

    trade = cost_result.trades.row(0, named=True)
    execution_nav = cost_result.nav.filter(pl.col("date") == date(2014, 1, 3)).row(0, named=True)
    maximum_gross = config.mvp.initial_cash / (1.0 + config.mvp.transaction_cost_bps / 10_000)
    expected_cost = maximum_gross * config.mvp.transaction_cost_bps / 10_000
    assert trade["gross_amount"] == pytest.approx(maximum_gross, rel=1e-12)
    assert trade["cost"] == pytest.approx(expected_cost, rel=1e-12)
    assert execution_nav["cash"] == pytest.approx(0.0, abs=1e-8)
    assert execution_nav["holdings_value"] == pytest.approx(maximum_gross)
    assert execution_nav["nav"] == pytest.approx(maximum_gross)
    assert cost_result.summary["total_cost"] == pytest.approx(expected_cost)
    assert zero_cost_result.summary["final_nav"] >= cost_result.summary["final_nav"]

    required_summary = {
        "initial_cash",
        "final_nav",
        "total_return",
        "annualized_return",
        "annualized_volatility",
        "sharpe_zero_rf",
        "max_drawdown",
        "average_turnover",
        "total_cost",
    }
    assert required_summary <= cost_result.summary.keys()
    assert cost_result.summary["uses_fractional_holdings"] == 1.0
    assert cost_result.summary["models_suspensions"] == 0.0
    assert cost_result.summary["models_price_limits"] == 0.0
    assert cost_result.summary["models_board_lots"] == 0.0
    assert cost_result.summary["models_historical_fees"] == 0.0
    assert cost_result.summary["models_unfilled_orders"] == 0.0


def test_warmup_prices_are_excluded_from_performance_dates() -> None:
    prices = pl.concat(
        [
            pl.DataFrame(
                {
                    "date": [date(2013, 12, 31)],
                    "symbol": ["000001"],
                    "open_adj": [9.0],
                    "close_adj": [9.0],
                }
            ),
            _single_stock_prices(),
        ]
    )

    result = run_backtest(prices, _single_stock_target(), _config())

    assert result.nav.get_column("date").to_list() == [
        date(2014, 1, 2),
        date(2014, 1, 3),
    ]


def test_high_turnover_rebalance_scales_targets_without_negative_cash() -> None:
    trading_dates = [date(2014, 1, 2), date(2014, 1, 3), date(2014, 1, 6)]
    prices = pl.DataFrame(
        {
            "date": [day for day in trading_dates for _ in range(3)],
            "symbol": ["000001", "000002", "000003"] * len(trading_dates),
            "open_adj": [10.0, 20.0, 25.0] * len(trading_dates),
            "close_adj": [10.0, 20.0, 25.0] * len(trading_dates),
        }
    )
    targets = pl.DataFrame(
        {
            "signal_date": [
                date(2014, 1, 2),
                date(2014, 1, 3),
                date(2014, 1, 3),
            ],
            "execution_date": [
                date(2014, 1, 3),
                date(2014, 1, 6),
                date(2014, 1, 6),
            ],
            "symbol": ["000001", "000002", "000003"],
            "target_weight": [1.0, 0.6, 0.4],
        }
    )

    result = run_backtest(prices, targets, _config())

    for row in result.nav.iter_rows(named=True):
        assert row["cash"] >= -1e-8
        assert row["cash"] + row["holdings_value"] == pytest.approx(row["nav"])

    final_holdings = result.holdings.filter(pl.col("date") == date(2014, 1, 6))
    values = dict(final_holdings.select("symbol", "market_value").iter_rows())
    assert set(values) == {"000002", "000003"}
    assert values["000002"] / values["000003"] == pytest.approx(0.6 / 0.4)

    rebalance_trades = result.trades.filter(pl.col("execution_date") == date(2014, 1, 6))
    trades_by_symbol = {row["symbol"]: row for row in rebalance_trades.iter_rows(named=True)}
    assert trades_by_symbol["000001"]["side"] == "sell"
    assert trades_by_symbol["000002"]["side"] == "buy"
    assert trades_by_symbol["000003"]["side"] == "buy"
    assert trades_by_symbol["000002"]["gross_amount"] / trades_by_symbol["000003"][
        "gross_amount"
    ] == pytest.approx(0.6 / 0.4)


def test_cash_constrained_rebalance_preserves_target_value_ratios() -> None:
    prices = pl.DataFrame(
        {
            "date": [
                date(2014, 1, 2),
                date(2014, 1, 2),
                date(2014, 1, 2),
                date(2014, 1, 3),
                date(2014, 1, 3),
                date(2014, 1, 3),
                date(2014, 1, 6),
                date(2014, 1, 6),
                date(2014, 1, 6),
                date(2014, 1, 6),
            ],
            "symbol": [
                "000001",
                "000002",
                "000005",
                "000001",
                "000002",
                "000005",
                "000002",
                "000003",
                "000004",
                "000005",
            ],
            "open_adj": [10.0] * 10,
            "close_adj": [10.0] * 10,
        }
    )
    targets = pl.DataFrame(
        {
            "signal_date": [
                date(2014, 1, 2),
                date(2014, 1, 2),
                date(2014, 1, 2),
                date(2014, 1, 3),
                date(2014, 1, 3),
                date(2014, 1, 3),
            ],
            "execution_date": [
                date(2014, 1, 3),
                date(2014, 1, 3),
                date(2014, 1, 3),
                date(2014, 1, 6),
                date(2014, 1, 6),
                date(2014, 1, 6),
            ],
            "symbol": ["000001", "000002", "000005", "000002", "000003", "000004"],
            "target_weight": [0.4, 0.1, 0.5, 0.3, 0.35, 0.35],
        }
    )

    result = run_backtest(prices, targets, _config())

    execution_date = date(2014, 1, 6)
    final_rows = result.holdings.filter(pl.col("date") == execution_date)
    final_values = dict(final_rows.select("symbol", "market_value").iter_rows())
    assert set(final_values) == {"000001", "000002", "000003", "000004"}
    assert final_values["000002"] / final_values["000003"] == pytest.approx(0.3 / 0.35)
    assert final_values["000003"] == pytest.approx(final_values["000004"])

    stale_a = final_rows.filter(pl.col("symbol") == "000001").row(0, named=True)
    original_a = result.holdings.filter(
        (pl.col("date") == date(2014, 1, 3)) & (pl.col("symbol") == "000001")
    ).row(0, named=True)
    assert stale_a["units"] == pytest.approx(original_a["units"])
    assert stale_a["price_is_stale"] is True
    assert (
        result.blocked_orders.filter(pl.col("execution_date") == execution_date).row(0, named=True)[
            "symbol"
        ]
        == "000001"
    )

    rebalance_trades = result.trades.filter(pl.col("execution_date") == execution_date)
    sides = rebalance_trades.get_column("side").to_list()
    first_buy = sides.index("buy")
    assert all(side == "sell" for side in sides[:first_buy])
    assert all(side == "buy" for side in sides[first_buy:])
    final_nav = result.nav.filter(pl.col("date") == execution_date).row(0, named=True)
    assert final_nav["cash"] >= -1e-8
    assert final_nav["cash"] + final_nav["holdings_value"] == pytest.approx(final_nav["nav"])


def test_missing_held_quote_uses_only_prior_close_and_balances_nav() -> None:
    prices = pl.DataFrame(
        {
            "date": [
                date(2014, 1, 2),
                date(2014, 1, 3),
                date(2014, 1, 6),
                date(2014, 1, 7),
            ],
            "symbol": ["000001", "000001", "000999", "000001"],
            "open_adj": [10.0, 10.0, 20.0, 1_000.0],
            "close_adj": [10.0, 11.0, 20.0, 1_000.0],
        }
    )

    config = _config()
    result = run_backtest(prices, _single_stock_target(), config)

    stale_holding = result.holdings.filter(pl.col("date") == date(2014, 1, 6)).row(0, named=True)
    stale_nav = result.nav.filter(pl.col("date") == date(2014, 1, 6)).row(0, named=True)
    maximum_gross = config.mvp.initial_cash / (1.0 + config.mvp.transaction_cost_bps / 10_000)
    assert stale_holding["units"] == pytest.approx(maximum_gross / 10.0)
    assert stale_holding["close_price"] == pytest.approx(11.0)
    assert stale_holding["price_is_stale"] is True
    assert stale_holding["valuation_price_date"] == date(2014, 1, 3)
    assert stale_nav["holdings_value"] == pytest.approx(maximum_gross / 10.0 * 11.0)
    assert stale_nav["cash"] + stale_nav["holdings_value"] == pytest.approx(stale_nav["nav"])
    assert result.summary["stale_valuation_count"] == 1.0


def test_missing_rebalance_quotes_create_blocked_orders_and_retain_positions() -> None:
    prices = pl.DataFrame(
        {
            "date": [
                date(2014, 1, 2),
                date(2014, 1, 2),
                date(2014, 1, 3),
                date(2014, 1, 3),
                date(2014, 1, 6),
            ],
            "symbol": ["000001", "000004", "000001", "000004", "000999"],
            "open_adj": [10.0, 10.0, 10.0, 10.0, 20.0],
            "close_adj": [10.0, 10.0, 10.0, 10.0, 20.0],
        }
    )
    targets = pl.DataFrame(
        {
            "signal_date": [
                date(2014, 1, 2),
                date(2014, 1, 2),
                date(2014, 1, 3),
                date(2014, 1, 3),
            ],
            "execution_date": [
                date(2014, 1, 3),
                date(2014, 1, 3),
                date(2014, 1, 6),
                date(2014, 1, 6),
            ],
            "symbol": ["000001", "000004", "000002", "000004"],
            "target_weight": [0.5, 0.5, 0.6, 0.4],
        }
    )

    result = run_backtest(prices, targets, _config())

    execution_date = date(2014, 1, 6)
    blocked = result.blocked_orders.filter(pl.col("execution_date") == execution_date)
    blocked_by_symbol = {row["symbol"]: row for row in blocked.iter_rows(named=True)}
    assert set(blocked_by_symbol) == {"000001", "000002", "000004"}
    assert blocked_by_symbol["000001"]["side"] == "sell"
    assert blocked_by_symbol["000001"]["target_weight"] == 0.0
    assert blocked_by_symbol["000002"]["side"] == "buy"
    assert blocked_by_symbol["000002"]["units_held"] == 0.0
    assert blocked_by_symbol["000004"]["side"] == "rebalance"
    assert all(row["reason"] == "missing_open_price" for row in blocked_by_symbol.values())
    assert result.trades.filter(pl.col("execution_date") == execution_date).is_empty()
    retained = result.holdings.filter(pl.col("date") == execution_date)
    assert set(retained.get_column("symbol")) == {"000001", "000004"}
    assert retained.get_column("price_is_stale").to_list() == [True, True]
    assert result.summary["blocked_order_count"] == 3.0


def test_blocked_buy_is_not_retried_when_quote_reappears() -> None:
    prices = pl.DataFrame(
        {
            "date": [
                date(2014, 1, 2),
                date(2014, 1, 3),
                date(2014, 1, 6),
                date(2014, 1, 7),
                date(2014, 1, 7),
            ],
            "symbol": ["000001", "000001", "000001", "000001", "000002"],
            "open_adj": [10.0, 10.0, 10.0, 10.0, 20.0],
            "close_adj": [10.0, 10.0, 10.0, 10.0, 20.0],
        }
    )
    targets = pl.DataFrame(
        {
            "signal_date": [date(2014, 1, 2), date(2014, 1, 3)],
            "execution_date": [date(2014, 1, 3), date(2014, 1, 6)],
            "symbol": ["000001", "000002"],
            "target_weight": [1.0, 1.0],
        }
    )

    result = run_backtest(prices, targets, _config())

    blocked_buy = result.blocked_orders.row(0, named=True)
    assert blocked_buy["execution_date"] == date(2014, 1, 6)
    assert blocked_buy["symbol"] == "000002"
    assert blocked_buy["side"] == "buy"
    assert result.trades.filter(pl.col("execution_date") == date(2014, 1, 7)).is_empty()
    assert result.holdings.filter(pl.col("date") == date(2014, 1, 7)).is_empty()
