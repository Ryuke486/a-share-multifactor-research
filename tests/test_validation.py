from datetime import date

import polars as pl
import pytest

from ashare_multifactor.data.validation import (
    QualityIssue,
    raise_on_errors,
    validate_daily_panel,
)


def _clean_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [date(2014, 1, 2)],
            "symbol": ["000001"],
            "industry": ["Bank"],
            "open_raw": [10.0],
            "high_raw": [11.0],
            "low_raw": [9.0],
            "close_raw": [10.5],
            "prev_close_raw": [9.8],
            "open_adj": [20.0],
            "high_adj": [22.0],
            "low_adj": [18.0],
            "close_adj": [21.0],
            "prev_close_adj": [19.6],
            "volume": [100.0],
            "amount": [1_000.0],
            "pe_ttm": [12.0],
            "pb": [1.2],
            "ps_ttm": [2.0],
            "is_margin": [True],
        }
    )


def test_quality_issue_serializes_to_dict() -> None:
    issue = QualityIssue("error", "duplicate_key", 2, "duplicate primary key")

    assert issue.to_dict() == {
        "severity": "error",
        "code": "duplicate_key",
        "count": 2,
        "message": "duplicate primary key",
    }


def test_clean_panel_has_no_issues() -> None:
    assert validate_daily_panel(_clean_frame(), date(2014, 1, 2)) == []


@pytest.mark.parametrize(
    ("mutations", "expected_code"),
    [
        ({"date": [date(2014, 1, 3)]}, "date_mismatch"),
        ({"date": [None]}, "date_mismatch"),
        ({"open_raw": [0.0]}, "invalid_ohlc"),
        ({"high_raw": [9.0]}, "invalid_ohlc"),
        ({"low_raw": [10.6]}, "invalid_ohlc"),
        ({"open_adj": [0.0]}, "invalid_ohlc"),
        ({"high_adj": [19.0]}, "invalid_ohlc"),
        ({"low_adj": [22.0]}, "invalid_ohlc"),
        ({"close_raw": [None]}, "missing_price"),
        ({"close_adj": [None]}, "missing_price"),
        ({"volume": [-1.0]}, "negative_volume_amount"),
        ({"amount": [-1.0]}, "negative_volume_amount"),
    ],
)
def test_errors_are_identified(mutations: dict[str, list[object]], expected_code: str) -> None:
    frame = _clean_frame().with_columns(
        [pl.Series(name, values) for name, values in mutations.items()]
    )

    issues = validate_daily_panel(frame, date(2014, 1, 2))

    assert expected_code in {issue.code for issue in issues}
    assert all(issue.severity == "error" for issue in issues)


def test_duplicate_keys_are_identified() -> None:
    frame = pl.concat([_clean_frame(), _clean_frame()])

    issues = validate_daily_panel(frame, date(2014, 1, 2))

    assert [(issue.code, issue.count) for issue in issues] == [("duplicate_key", 2)]


def test_warnings_are_reported_in_stable_code_order() -> None:
    frame = _clean_frame().with_columns(
        pl.lit(None, dtype=pl.String).alias("industry"),
        pl.lit(None, dtype=pl.Float64).alias("pe_ttm"),
        pl.lit(None, dtype=pl.Float64).alias("pb"),
        pl.lit(None, dtype=pl.Float64).alias("ps_ttm"),
        pl.lit(None, dtype=pl.Boolean).alias("is_margin"),
        pl.lit(0.0).alias("volume"),
        pl.lit(0.0).alias("amount"),
    )

    issues = validate_daily_panel(frame, date(2014, 1, 2))

    assert [issue.code for issue in issues] == [
        "missing_industry",
        "missing_margin",
        "missing_valuation",
        "zero_volume_amount",
    ]
    assert all(issue.severity == "warning" for issue in issues)


@pytest.mark.parametrize("column", ["prev_close_raw", "prev_close_adj"])
def test_missing_previous_close_is_a_non_blocking_warning(column: str) -> None:
    frame = _clean_frame().with_columns(pl.lit(None, dtype=pl.Float64).alias(column))

    issues = validate_daily_panel(frame, date(2014, 1, 2))

    assert [(issue.severity, issue.code, issue.count) for issue in issues] == [
        ("warning", "missing_prev_close", 1)
    ]
    assert frame[column].item() is None
    raise_on_errors(issues)


def test_raise_on_errors_raises_with_stable_detail() -> None:
    issues = [
        QualityIssue("warning", "missing_industry", 1, "missing industry"),
        QualityIssue("error", "invalid_ohlc", 2, "invalid OHLC"),
        QualityIssue("error", "missing_price", 1, "missing price"),
    ]

    with pytest.raises(
        ValueError,
        match=r"^data quality errors: invalid_ohlc:2; missing_price:1$",
    ):
        raise_on_errors(issues)


def test_raise_on_errors_allows_warnings() -> None:
    raise_on_errors([QualityIssue("warning", "zero_volume_amount", 1, "zero volume")])
