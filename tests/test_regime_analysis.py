from datetime import date, timedelta

import polars as pl

from ashare_multifactor.robustness.regime_analysis import (
    classify_market_regimes,
    industry_limitation,
    size_segments,
    summarize_segment_returns,
    time_segments,
)


def _market(values: list[float]) -> pl.DataFrame:
    start = date(2017, 1, 2)
    return pl.DataFrame(
        {
            "date": [start + timedelta(days=index) for index in range(len(values))],
            "market_return": values,
        }
    )


def test_market_regimes_use_only_contemporaneously_available_market_history() -> None:
    original = _market([0.01, -0.01, 0.02, -0.02] * 30)
    changed_future = original.with_columns(
        pl.when(pl.col("date") > original.item(79, "date"))
        .then(pl.lit(0.50))
        .otherwise(pl.col("market_return"))
        .alias("market_return")
    )

    first = classify_market_regimes(original)
    second = classify_market_regimes(changed_future)

    assert first.head(80).equals(second.head(80))
    assert set(first.get_column("market_trend")) <= {"up", "down", "insufficient_history"}
    assert set(first.get_column("volatility_regime")) <= {
        "high",
        "low",
        "insufficient_history",
    }


def test_segment_summary_reports_sample_and_uncertainty() -> None:
    returns = pl.DataFrame(
        {
            "date": [date(2017, 1, day) for day in range(1, 7)],
            "return": [0.01, -0.02, 0.03, 0.01, -0.01, 0.02],
            "segment_type": ["market_trend"] * 6,
            "segment": ["up", "down", "up", "up", "down", "up"],
        }
    )

    summary = summarize_segment_returns(returns)

    assert summary.get_column("observations").sum() == 6
    assert summary.get_column("start_date").min() == date(2017, 1, 1)
    assert summary.get_column("standard_error").null_count() == 0
    assert {"ci95_low", "ci95_high"} <= set(summary.columns)


def test_time_and_industry_boundaries_are_explicit() -> None:
    dates = pl.DataFrame(
        {"date": [date(2016, 12, 30), date(2017, 1, 3), date(2021, 12, 31)]}
    )
    segmented = time_segments(dates)

    assert segmented.get_column("segment").to_list() == [
        "research_2005_2016",
        "validation_2017_2021",
        "validation_2017_2021",
    ]
    assert industry_limitation() == {
        "status": "descriptive_only",
        "reason": "historical_industry_point_in_time_unverified",
        "eligible_for_primary_conclusion": False,
    }


def test_time_segments_use_the_canonical_concat_column_order() -> None:
    frame = pl.DataFrame(
        {"date": [date(2017, 1, 3)], "return": [0.01]}
    )

    assert time_segments(frame).columns == [
        "date",
        "return",
        "segment_type",
        "segment",
    ]


def test_monthly_size_summary_uses_monthly_rows_and_annualization() -> None:
    returns = pl.DataFrame(
        {
            "date": [date(2017, 1, 31), date(2017, 2, 28)],
            "return": [0.01, 0.03],
            "segment_type": ["market_size", "market_size"],
            "segment": ["small", "small"],
        }
    )

    summary = summarize_segment_returns(
        returns, annualization_by_segment_type={"market_size": 12}
    )

    assert summary.item(0, "observations") == 2
    assert summary.item(0, "annualized_return") == 0.24
    assert "mean_period_return" in summary.columns
    assert "mean_daily_return" not in summary.columns


def test_size_segments_preserve_canonical_concat_order_after_aggregation() -> None:
    panel = pl.DataFrame(
        {
            "date": [date(2017, 1, 31), date(2017, 1, 31)],
            "factor_name": ["log_market_cap", "log_market_cap"],
            "raw_value": [1.0, 2.0],
            "forward_return_20": [0.01, 0.03],
        }
    )

    result = size_segments(panel)

    assert result.columns == ["date", "return", "segment_type", "segment"]
