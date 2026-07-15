from datetime import date
from pathlib import Path

import polars as pl
import pytest

from ashare_multifactor.execution.broker import BacktestSettings
from ashare_multifactor.execution.corporate_actions import normalize_corporate_actions
from ashare_multifactor.execution.fees import load_market_rules
from ashare_multifactor.validation.backtest_extension import run_validation_backtest
from ashare_multifactor.validation.backtest_inputs import (
    build_continuous_targets,
    extend_execution_panel,
)
from ashare_multifactor.validation.corporate_actions import (
    assert_baostock_query_scope,
    load_validation_corporate_actions,
)
from ashare_multifactor.validation.portfolio_extension import build_validation_targets


RULES = Path(__file__).parents[1] / "configs/market_rules.yaml"


def test_validation_actions_keep_ex_date_and_payment_date_separate(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "baostock_dividends.parquet"
    pl.DataFrame(
        {
            "code": ["sz.000001"],
            "query_year": [2018],
            "query_year_type": ["operate"],
            "dividPlanAnnounceDate": ["2018-06-01"],
            "dividRegistDate": ["2018-06-07"],
            "dividOperateDate": ["2018-06-08"],
            "dividPayDate": ["2018-06-12"],
            "dividStockMarketDate": [""],
            "dividCashPsBeforeTax": ["0.2"],
            "dividStocksPs": ["0"],
            "dividReserveToStockPs": ["0"],
        }
    ).write_parquet(raw)
    research = normalize_corporate_actions(
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

    actions = load_validation_corporate_actions(
        research, raw, symbols=["000001"]
    )

    row = actions.row(0, named=True)
    assert row["ex_date"] == date(2018, 6, 8)
    assert row["effective_date"] == date(2018, 6, 12)
    assert row["cash_per_share"] == 0.2


def test_validation_actions_fill_missing_payment_date_only_from_official_evidence(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "baostock_dividends.parquet"
    pl.DataFrame(
        {
            "code": ["sz.000819"],
            "query_year": [2021],
            "query_year_type": ["operate"],
            "dividPlanAnnounceDate": ["2021-03-30"],
            "dividRegistDate": ["2021-06-24"],
            "dividOperateDate": ["2021-06-25"],
            "dividPayDate": [""],
            "dividStockMarketDate": [""],
            "dividCashPsBeforeTax": ["0.01"],
            "dividStocksPs": ["0"],
            "dividReserveToStockPs": ["0"],
        }
    ).write_parquet(raw)
    research = normalize_corporate_actions(
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
    overrides = pl.DataFrame(
        {
            "symbol": ["000819"],
            "ex_date": [date(2021, 6, 25)],
            "payment_date": [date(2021, 6, 25)],
            "source_url": ["https://static.cninfo.com.cn/notice.pdf"],
        }
    )

    actions = load_validation_corporate_actions(
        research,
        raw,
        symbols=["000819"],
        payment_date_overrides=overrides,
    )

    assert actions.item(0, "effective_date") == date(2021, 6, 25)
    assert actions.item(0, "source").endswith("+cninfo_payment_date")


def test_validation_action_query_scope_rejects_unsealed_requests() -> None:
    assert_baostock_query_scope(2017, "operate")
    assert_baostock_query_scope(2021, "operate")

    with pytest.raises(ValueError, match="sealed final test"):
        assert_baostock_query_scope(2022, "operate")
    with pytest.raises(ValueError, match="operate"):
        assert_baostock_query_scope(2021, "report")


def test_validation_actions_reject_any_date_after_2021(tmp_path: Path) -> None:
    raw = tmp_path / "baostock_dividends.parquet"
    pl.DataFrame(
        {
            "code": ["sz.000001"],
            "query_year": [2021],
            "query_year_type": ["operate"],
            "dividPlanAnnounceDate": ["2021-12-20"],
            "dividRegistDate": ["2021-12-30"],
            "dividOperateDate": ["2021-12-31"],
            "dividPayDate": ["2022-01-04"],
            "dividStockMarketDate": [""],
            "dividCashPsBeforeTax": ["0.2"],
            "dividStocksPs": ["0"],
            "dividReserveToStockPs": ["0"],
        }
    ).write_parquet(raw)
    research = normalize_corporate_actions(
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

    with pytest.raises(ValueError, match="sealed final test"):
        load_validation_corporate_actions(research, raw, symbols=["000001"])


def test_buffered_validation_target_uses_research_period_holdings() -> None:
    signal_date = date(2017, 1, 31)
    scores = pl.DataFrame(
        {
            "date": [signal_date] * 4,
            "symbol": ["000001", "000002", "000003", "000004"],
            "method": ["family_equal"] * 4,
            "score": [4.0, 3.0, 2.0, 1.0],
        }
    )
    log_size = pl.DataFrame(
        {
            "date": [signal_date] * 4,
            "symbol": ["000001", "000002", "000003", "000004"],
            "log_market_cap": [1.0, 2.0, 3.0, 4.0],
        }
    )
    previous = pl.DataFrame(
        {"symbol": ["000002", "000004"], "target_weight": [0.5, 0.5]}
    )

    result = build_validation_targets(
        scores,
        log_size,
        method="family_equal",
        portfolio_name="size_stratified_buffered",
        previous_targets=previous,
        portfolio_size=2,
        size_groups=2,
        per_group=1,
        buffer_rank=2,
    )

    assert set(result.targets.get_column("symbol")) == {"000002", "000004"}
    assert result.diagnostics.item(0, "continued_from_research") is True


def test_validation_backtest_runs_research_history_before_validation() -> None:
    dates = [date(2016, 12, 29), date(2016, 12, 30), date(2017, 1, 3)]
    panel = pl.DataFrame(
        {
            "date": dates,
            "symbol": ["000001"] * 3,
            "open_raw": [10.0, 10.0, 11.0],
            "close_raw": [10.0, 10.5, 11.0],
            "prev_close_raw": [10.0, 10.0, 10.5],
            "adv20": [10_000_000.0] * 3,
            "limit_rate": [0.10] * 3,
            "is_suspended_proxy": [False] * 3,
        }
    )
    targets = pl.DataFrame(
        {
            "date": [date(2016, 12, 29)],
            "symbol": ["000001"],
            "target_weight": [0.5],
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

    result = run_validation_backtest(
        panel,
        targets,
        actions,
        load_market_rules(RULES),
        BacktestSettings(initial_cash=100_000.0, analysis_end=date(2021, 12, 31)),
    )

    first_validation = result["nav"].filter(pl.col("date") == date(2017, 1, 3)).row(
        0, named=True
    )
    assert first_validation["holdings_value"] > 0
    assert first_validation["nav"] != 100_000.0


def test_validation_backtest_rejects_sealed_dates_in_every_input() -> None:
    panel = pl.DataFrame(
        {
            "date": [date(2016, 12, 30), date(2017, 1, 3)],
            "symbol": ["000001", "000001"],
            "open_raw": [10.0, 10.0],
            "close_raw": [10.0, 10.0],
            "prev_close_raw": [10.0, 10.0],
            "adv20": [10_000_000.0, 10_000_000.0],
            "limit_rate": [0.10, 0.10],
            "is_suspended_proxy": [False, False],
        }
    )
    targets = pl.DataFrame(
        {"date": [date(2016, 12, 29)], "symbol": ["000001"], "target_weight": [1.0]}
    )
    empty_actions = normalize_corporate_actions(
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
    sealed_actions = normalize_corporate_actions(
        pl.DataFrame(
            {
                "symbol": ["000001"],
                "ex_date": [date(2022, 1, 4)],
                "effective_date": [date(2022, 1, 4)],
                "cash_per_share": [0.1],
                "share_ratio": [0.0],
                "source": ["fixture"],
            }
        ),
        maximum_date=date(2022, 1, 4),
    )
    sealed_events = pl.DataFrame(
        {
            "effective_date": [date(2022, 1, 4)],
            "source_symbol": ["000001"],
            "event_type": ["write_off"],
            "target_symbol": [None],
            "ratio": [0.0],
            "cash_per_share": [0.0],
            "source": ["fixture"],
        }
    )
    settings = BacktestSettings(initial_cash=100_000.0, analysis_end=date(2021, 12, 31))
    fees = load_market_rules(RULES)

    with pytest.raises(ValueError, match="sealed final test.*corporate_actions"):
        run_validation_backtest(panel, targets, sealed_actions, fees, settings)
    with pytest.raises(ValueError, match="sealed final test.*security_events"):
        run_validation_backtest(
            panel,
            targets,
            empty_actions,
            fees,
            settings,
            sealed_events,
        )
    sealed_targets = pl.concat(
        [
            targets,
            pl.DataFrame(
                {
                    "date": [date(2022, 1, 4)],
                    "symbol": ["000001"],
                    "target_weight": [1.0],
                }
            ),
        ]
    )
    with pytest.raises(ValueError, match="sealed final test.*target_weights"):
        run_validation_backtest(panel, sealed_targets, empty_actions, fees, settings)


def test_rolling_candidate_deploys_from_frozen_research_baseline() -> None:
    research = pl.DataFrame(
        {
            "date": [date(2016, 12, 30)],
            "portfolio_name": ["size_stratified_buffered"],
            "symbol": ["000001"],
            "target_weight": [1.0],
        }
    )
    validation = pl.DataFrame(
        {
            "date": [date(2017, 1, 26)],
            "candidate": ["rolling_ic_family_size_stratified_buffered"],
            "symbol": ["000002"],
            "target_weight": [1.0],
        }
    )

    targets = build_continuous_targets(
        research,
        validation,
        candidate="rolling_ic_family_size_stratified_buffered",
    )

    assert targets.get_column("date").to_list() == [
        date(2016, 12, 30),
        date(2017, 1, 26),
    ]
    assert targets.get_column("symbol").to_list() == ["000001", "000002"]


def test_top100_candidate_also_deploys_from_audited_stage_six_baseline() -> None:
    research = pl.DataFrame(
        {
            "date": [date(2016, 12, 30), date(2016, 12, 30)],
            "portfolio_name": ["size_stratified_buffered", "top100_equal"],
            "symbol": ["000001", "000099"],
            "target_weight": [1.0, 1.0],
        }
    )
    validation = pl.DataFrame(
        {
            "date": [date(2017, 1, 26)],
            "candidate": ["family_equal_top100_equal"],
            "symbol": ["000002"],
            "target_weight": [1.0],
        }
    )

    targets = build_continuous_targets(
        research,
        validation,
        candidate="family_equal_top100_equal",
    )

    assert targets.get_column("symbol").to_list() == ["000001", "000002"]


def test_validation_execution_extension_uses_research_warmup_for_adv() -> None:
    raw = pl.DataFrame(
        {
            "date": [date(2016, 12, 29), date(2016, 12, 30), date(2017, 1, 3)],
            "symbol": ["000001"] * 3,
            "open_raw": [10.0] * 3,
            "close_raw": [10.0] * 3,
            "prev_close_raw": [10.0] * 3,
            "close_adj": [10.0] * 3,
            "prev_close_adj": [10.0] * 3,
            "amount": [100.0, 200.0, 300.0],
            "is_st": [False] * 3,
        }
    )
    research = pl.DataFrame(
        {
            "date": [date(2016, 12, 30)],
            "symbol": ["000001"],
            "open_raw": [10.0],
            "close_raw": [10.0],
            "prev_close_raw": [10.0],
            "close_adj": [10.0],
            "prev_close_adj": [10.0],
            "adv20": [100.0],
            "limit_rate": [0.10],
            "is_suspended_proxy": [False],
        }
    )

    result = extend_execution_panel(research, raw, adv_lookback=2)

    assert result.filter(pl.col("date") == date(2017, 1, 3)).item(0, "adv20") == 150.0
