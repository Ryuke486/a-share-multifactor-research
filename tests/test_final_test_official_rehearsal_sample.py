from __future__ import annotations

from datetime import date

import polars as pl
import pytest


def _panel() -> pl.DataFrame:
    rows = []
    for market, prefix in (("sh", "6"), ("sz", "0")):
        for index in range(12):
            rows.append(
                {
                    "date": date(2021, 12, 31),
                    "symbol": f"{prefix}{index:05d}",
                    "total_market_cap": float(index + 1),
                    "market": market,
                }
            )
    rows.extend(
        [
            {
                "date": date(2017, 12, 22),
                "symbol": "000916",
                "total_market_cap": 10.0,
                "market": "sz",
            },
            {
                "date": date(2018, 12, 27),
                "symbol": "000979",
                "total_market_cap": 20.0,
                "market": "sz",
            },
        ]
    )
    return pl.DataFrame(rows)


def test_rehearsal_sample_is_deterministic_stratified_and_keeps_known_events() -> None:
    from ashare_multifactor.final_test.official_rehearsal_sample import (
        select_rehearsal_sample,
    )

    first = select_rehearsal_sample(
        _panel(),
        sample_size=10,
        required_symbols=("000916", "000979"),
    )
    second = select_rehearsal_sample(
        _panel().reverse(),
        sample_size=10,
        required_symbols=("000916", "000979"),
    )

    assert first.equals(second)
    assert first.height == 10
    assert {"000916", "000979"} <= set(first.get_column("symbol"))
    assert set(first.get_column("market")) == {"sh", "sz"}
    assert set(first.get_column("size_bucket")) == {1, 2, 3, 4}
    assert first.get_column("symbol").n_unique() == first.height


def test_rehearsal_sample_rejects_post_2021_data_or_missing_required_symbol() -> None:
    from ashare_multifactor.final_test.official_rehearsal_sample import (
        select_rehearsal_sample,
    )

    with pytest.raises(ValueError, match="2021|validation"):
        select_rehearsal_sample(
            _panel().with_columns(pl.lit(date(2022, 1, 1)).alias("date")),
            sample_size=10,
            required_symbols=("000916",),
        )
    with pytest.raises(ValueError, match="required"):
        select_rehearsal_sample(
            _panel(),
            sample_size=10,
            required_symbols=("600999",),
        )
