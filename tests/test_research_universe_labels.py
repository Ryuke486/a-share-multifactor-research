from dataclasses import replace
from datetime import date, timedelta

import polars as pl
import pytest

from ashare_multifactor.config import FactorResearchSettings


def _settings(**overrides: object) -> FactorResearchSettings:
    settings = FactorResearchSettings(
        data_start=date(2003, 1, 1),
        analysis_start=date(2005, 1, 1),
        analysis_end=date(2016, 12, 31),
        universe_size=1_000,
        minimum_history=252,
        liquidity_lookback=20,
        require_valid_trade_observation=True,
        signal_frequency="month_end",
        forward_horizons=(5, 20, 60),
        primary_horizon=20,
        winsor_lower=0.01,
        winsor_upper=0.99,
        quantile_count=5,
        minimum_coverage=0.8,
        minimum_valid_months=120,
        fdr_q_threshold=0.1,
        redundancy_threshold=0.7,
    )
    return replace(settings, **overrides)


def _universe_row(day: date, symbol: str, amount: float = 1_000.0) -> dict[str, object]:
    return {
        "date": day,
        "symbol": symbol,
        "close_adj": 20.0,
        "volume": 100.0,
        "amount": amount,
        "is_st": False,
        "open_raw": 10.0,
        "high_raw": 11.0,
        "low_raw": 9.0,
        "close_raw": 10.5,
    }


@pytest.mark.parametrize(
    ("column", "invalid_value"),
    [
        ("close_adj", 0.0),
        ("close_adj", float("nan")),
        ("close_adj", float("inf")),
        ("volume", 0.0),
        ("volume", float("nan")),
        ("volume", float("inf")),
        ("amount", 0.0),
        ("amount", float("nan")),
        ("amount", float("inf")),
    ],
)
def test_valid_trade_observation_requires_positive_finite_adjusted_close_and_trades(
    column: str,
    invalid_value: float,
) -> None:
    from ashare_multifactor.research.trade_observations import (
        is_valid_trade_observation,
    )

    frame = pl.DataFrame([_universe_row(date(2004, 1, 5), "000001")]).with_columns(
        pl.lit(invalid_value).alias(column)
    )

    assert frame.select(is_valid_trade_observation()).item() is False


def test_valid_trade_observation_does_not_replace_raw_ohlc_checks() -> None:
    from ashare_multifactor.research.research_universe import build_research_universe
    from ashare_multifactor.research.trade_observations import (
        is_valid_trade_observation,
    )

    panel = pl.DataFrame([_universe_row(date(2004, 1, 5), "000001")]).with_columns(
        pl.lit(0.0).alias("open_raw")
    )

    assert panel.select(is_valid_trade_observation()).item() is True
    assert (
        build_research_universe(
            panel,
            _settings(minimum_history=1, liquidity_lookback=1),
        )
        .get_column("is_research_eligible")
        .item()
        is False
    )


def test_research_universe_uses_no_information_after_target_date() -> None:
    from ashare_multifactor.research.research_universe import build_research_universe

    days = [date(2004, 1, 5) + timedelta(days=offset) for offset in range(3)]
    panel = pl.DataFrame(
        [
            _universe_row(day, symbol, amount)
            for symbol, amount in (("000001", 1_000.0), ("000002", 500.0))
            for day in days
        ]
    )
    settings = _settings(minimum_history=2, liquidity_lookback=2, universe_size=1)
    baseline = build_research_universe(panel, settings)
    changed_future = build_research_universe(
        panel.with_columns(
            pl.when((pl.col("date") == days[2]) & (pl.col("symbol") == "000002"))
            .then(1_000_000_000.0)
            .otherwise(pl.col("amount"))
            .alias("amount"),
            pl.when((pl.col("date") == days[2]) & (pl.col("symbol") == "000001"))
            .then(True)
            .otherwise(pl.col("is_st"))
            .alias("is_st"),
            pl.when((pl.col("date") == days[2]) & (pl.col("symbol") == "000001"))
            .then(-1.0)
            .otherwise(pl.col("close_raw"))
            .alias("close_raw"),
            pl.when((pl.col("date") == days[2]) & (pl.col("symbol") == "000002"))
            .then(0.0)
            .otherwise(pl.col("volume"))
            .alias("volume"),
            pl.when((pl.col("date") == days[2]) & (pl.col("symbol") == "000002"))
            .then(float("nan"))
            .otherwise(pl.col("close_adj"))
            .alias("close_adj"),
        ),
        settings,
    )

    target_columns = ["symbol", "liquidity_rank", "is_research_eligible"]
    target = baseline.filter(pl.col("date") == days[1]).select(target_columns).sort("symbol")
    changed_target = (
        changed_future.filter(pl.col("date") == days[1])
        .select(target_columns)
        .sort("symbol")
    )

    assert target.rows() == [("000001", 1, True), ("000002", 2, False)]
    assert changed_target.equals(target)


def test_research_universe_breaks_liquidity_ties_by_symbol_ascending() -> None:
    from ashare_multifactor.research.research_universe import build_research_universe

    day = date(2004, 1, 5)
    panel = pl.DataFrame(
        [_universe_row(day, symbol) for symbol in ("000003", "000001", "000002")]
    )

    rows = build_research_universe(
        panel,
        _settings(minimum_history=1, liquidity_lookback=1, universe_size=2),
    ).sort("symbol")

    assert rows.select("symbol", "liquidity_rank", "is_research_eligible").rows() == [
        ("000001", 1, True),
        ("000002", 2, True),
        ("000003", 3, False),
    ]


def test_research_universe_requires_exactly_252_valid_history_observations() -> None:
    from ashare_multifactor.research.research_universe import build_research_universe

    days = [date(2003, 1, 1) + timedelta(days=offset) for offset in range(253)]
    panel = pl.DataFrame([_universe_row(day, "000001") for day in days]).with_columns(
        pl.when(pl.col("date") == days[1])
        .then(0.0)
        .otherwise(pl.col("amount"))
        .alias("amount")
    )

    rows = build_research_universe(panel, _settings()).filter(
        pl.col("date").is_in([days[-2], days[-1]])
    )

    assert rows.select("history_observations", "is_research_eligible").rows() == [
        (251, False),
        (252, True),
    ]


def test_research_universe_liquidity_uses_last_20_valid_observations() -> None:
    from ashare_multifactor.research.research_universe import build_research_universe

    days = [date(2003, 1, 1) + timedelta(days=offset) for offset in range(21)]
    panel = pl.DataFrame(
        [
            _universe_row(day, "000001", 1_000.0 if index == 0 else 100.0)
            for index, day in enumerate(days)
        ]
    ).with_columns(
        pl.when(pl.col("date") == days[5])
        .then(0.0)
        .otherwise(pl.col("amount"))
        .alias("amount")
    )

    row = build_research_universe(
        panel,
        _settings(minimum_history=20, liquidity_lookback=20),
    ).filter(pl.col("date") == days[-1])

    assert row.select(
        "history_observations",
        "liquidity_amount_mean",
        "is_research_eligible",
    ).row(0) == (20, 145.0, True)


@pytest.mark.parametrize(
    ("column", "invalid_value"),
    [
        ("is_st", True),
        ("open_raw", None),
        ("high_raw", float("nan")),
        ("low_raw", float("inf")),
        ("close_raw", 0.0),
        ("close_adj", 0.0),
        ("close_adj", float("nan")),
        ("close_adj", float("inf")),
        ("volume", 0.0),
        ("volume", float("nan")),
        ("volume", float("inf")),
        ("amount", None),
        ("amount", 0.0),
        ("amount", float("nan")),
        ("amount", float("inf")),
    ],
)
def test_research_universe_rejects_invalid_current_trade_state(
    column: str,
    invalid_value: object,
) -> None:
    from ashare_multifactor.research.research_universe import build_research_universe

    panel = pl.DataFrame([_universe_row(date(2004, 1, 5), "000001")]).with_columns(
        pl.lit(invalid_value).alias(column)
    )

    row = build_research_universe(
        panel,
        _settings(minimum_history=1, liquidity_lookback=1),
    )

    assert row.get_column("is_research_eligible").item() is False
    assert row.get_column("liquidity_rank").item() is None


def test_research_universe_is_not_capped_by_mvp_top_200() -> None:
    from ashare_multifactor.research.research_universe import build_research_universe

    day = date(2004, 1, 5)
    panel = pl.DataFrame(
        [_universe_row(day, f"{symbol:06d}") for symbol in range(1, 202)]
    )

    rows = build_research_universe(
        panel,
        _settings(minimum_history=1, liquidity_lookback=1),
    )

    assert rows.get_column("is_research_eligible").sum() == 201


def _label_row(day: date, symbol: str, day_index: int) -> dict[str, object]:
    return {
        "date": day,
        "symbol": symbol,
        "close_adj": 100.0 + day_index,
        "volume": 100.0,
        "amount": 1_000.0,
    }


def test_forward_returns_use_each_symbols_valid_observations_and_audit_gaps() -> None:
    from ashare_multifactor.research.labels import add_forward_returns

    days = [date(2005, 1, 1) + timedelta(days=offset) for offset in range(66)]
    rows = [_label_row(day, "000001", index) for index, day in enumerate(days)]
    rows.extend(
        _label_row(day, "000002", index)
        for index, day in enumerate(days)
        if index != 2
    )
    panel = pl.DataFrame(rows).with_columns(
        pl.when((pl.col("symbol") == "000001") & pl.col("date").is_in([days[2], days[4]]))
        .then(0.0)
        .otherwise(pl.col("amount"))
        .alias("amount"),
        pl.when((pl.col("symbol") == "000001") & (pl.col("date") == days[6]))
        .then(float("nan"))
        .otherwise(pl.col("close_adj"))
        .alias("close_adj"),
        pl.when((pl.col("symbol") == "000002") & (pl.col("date") == days[4]))
        .then(0.0)
        .otherwise(pl.col("volume"))
        .alias("volume"),
    )

    target_rows = (
        add_forward_returns(panel, (5, 20, 60))
        .filter(pl.col("date") == days[0])
        .sort("symbol")
    )

    assert target_rows.select(
        "symbol",
        "forward_return_5",
        "forward_return_20",
        "forward_return_60",
    ).rows() == pytest.approx(
        [
            ("000001", 108.0 / 100.0 - 1.0, 123.0 / 100.0 - 1.0, 163.0 / 100.0 - 1.0),
            ("000002", 107.0 / 100.0 - 1.0, 122.0 / 100.0 - 1.0, 162.0 / 100.0 - 1.0),
        ]
    )
    assert target_rows.select(
        "symbol",
        "forward_calendar_days_5",
        "forward_calendar_days_20",
        "forward_calendar_days_60",
        "forward_skipped_observations_5",
        "forward_skipped_observations_20",
        "forward_skipped_observations_60",
    ).rows() == [
        ("000001", 8, 23, 63, 3, 3, 3),
        ("000002", 7, 22, 62, 1, 1, 1),
    ]


@pytest.mark.parametrize(
    ("column", "invalid_value"),
    [
        ("close_adj", 0.0),
        ("volume", 0.0),
        ("amount", 0.0),
        ("close_adj", float("nan")),
        ("volume", float("inf")),
        ("amount", float("nan")),
    ],
)
def test_invalid_signal_observation_has_no_labels_or_diagnostics(
    column: str,
    invalid_value: float,
) -> None:
    from ashare_multifactor.research.labels import add_forward_returns

    days = [date(2005, 1, 1) + timedelta(days=offset) for offset in range(7)]
    panel = pl.DataFrame(
        [_label_row(day, "000001", index) for index, day in enumerate(days)]
    ).with_columns(
        pl.when(pl.col("date") == days[0])
        .then(invalid_value)
        .otherwise(pl.col(column))
        .alias(column)
    )

    row = add_forward_returns(panel, (5,)).filter(pl.col("date") == days[0])

    assert row.select(
        "is_valid_trade_observation",
        "forward_return_5",
        "forward_calendar_days_5",
        "forward_skipped_observations_5",
    ).row(0) == (False, None, None, None)


def test_forward_target_and_diagnostics_are_null_when_horizon_is_unavailable() -> None:
    from ashare_multifactor.research.labels import add_forward_returns

    days = [date(2005, 1, 1) + timedelta(days=offset) for offset in range(6)]
    panel = pl.DataFrame(
        [_label_row(day, "000001", index) for index, day in enumerate(days)]
    )

    row = add_forward_returns(panel, (5, 20)).filter(pl.col("date") == days[0])

    assert row.select(
        "forward_return_5",
        "forward_calendar_days_5",
        "forward_skipped_observations_5",
    ).row(0) == pytest.approx((105.0 / 100.0 - 1.0, 5, 0))
    assert row.select(
        "forward_return_20",
        "forward_calendar_days_20",
        "forward_skipped_observations_20",
    ).row(0) == (None, None, None)
