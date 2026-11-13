from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl
import pytest

from ashare_multifactor.supplements.fama_macbeth import (
    fama_macbeth_summary,
    monthly_cross_sectional_regressions,
)


def _panel(
    months: int = 36,
    stocks: int = 300,
    *,
    betas: tuple[float, float] = (0.02, 0.0),
    redundancy: float = 0.0,
    seed: int = 7,
) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for month in range(months):
        day = date(2006 + month // 12, month % 12 + 1, 28)
        first = rng.standard_normal(stocks)
        second = redundancy * first + (1.0 - redundancy) * rng.standard_normal(stocks)
        label = betas[0] * first + betas[1] * second + 0.05 * rng.standard_normal(stocks)
        rows.append(
            pl.DataFrame(
                {
                    "date": [day] * stocks,
                    "symbol": [f"{index:06d}" for index in range(stocks)],
                    "first": first,
                    "second": second,
                    "label": label + 0.01 * month,
                }
            )
        )
    return pl.concat(rows)


def test_regression_recovers_the_per_standard_deviation_premium() -> None:
    monthly = monthly_cross_sectional_regressions(
        _panel(), factors=("first", "second"), label="label", minimum=50
    )
    summary = fama_macbeth_summary(monthly, published_lag=0)
    by_factor = {row["factor_name"]: row for row in summary.iter_rows(named=True)}

    assert monthly.height == 36 * 2
    assert by_factor["first"]["months"] == 36
    assert by_factor["first"]["mean_coefficient"] == pytest.approx(0.02, abs=0.002)
    assert by_factor["first"]["t_published_lag"] > 10
    assert abs(by_factor["second"]["t_published_lag"]) < 3
    assert by_factor["first"]["automatic_lag"] == 3
    assert 0.0 < by_factor["first"]["mean_r_squared"] < 1.0


def test_a_redundant_factor_has_no_incremental_premium_once_its_source_is_controlled() -> None:
    panel = _panel(betas=(0.02, 0.0), redundancy=0.8)

    alone = fama_macbeth_summary(
        monthly_cross_sectional_regressions(panel, factors=("second",), label="label", minimum=50),
        published_lag=0,
    ).row(0, named=True)
    controlled = {
        row["factor_name"]: row
        for row in fama_macbeth_summary(
            monthly_cross_sectional_regressions(
                panel, factors=("first", "second"), label="label", minimum=50
            ),
            published_lag=0,
        ).iter_rows(named=True)
    }

    assert alone["t_published_lag"] > 10
    assert abs(controlled["second"]["t_published_lag"]) < 3
    assert controlled["first"]["t_published_lag"] > 5


def test_coefficients_are_invariant_to_the_scale_of_a_regressor() -> None:
    panel = _panel(months=6)
    scaled = panel.with_columns(pl.col("first") * 1000.0 + 5.0)

    base = monthly_cross_sectional_regressions(
        panel, factors=("first", "second"), label="label", minimum=50
    )
    other = monthly_cross_sectional_regressions(
        scaled, factors=("first", "second"), label="label", minimum=50
    )

    assert other.get_column("coefficient").to_numpy() == pytest.approx(
        base.get_column("coefficient").to_numpy(), abs=1e-12
    )


def test_incomplete_rows_are_dropped_and_thin_or_degenerate_months_are_skipped() -> None:
    panel = _panel(months=4, stocks=80)
    days = panel.get_column("date").unique().sort().to_list()
    panel = panel.with_columns(
        pl.when((pl.col("date") == days[0]) & (pl.col("symbol") < "000040"))
        .then(None)
        .otherwise(pl.col("second"))
        .alias("second"),
        pl.when(pl.col("date") == days[1]).then(1.0).otherwise(pl.col("first")).alias("first"),
        pl.when((pl.col("date") == days[2]) & (pl.col("symbol") == "000000"))
        .then(float("nan"))
        .otherwise(pl.col("label"))
        .alias("label"),
    )

    monthly = monthly_cross_sectional_regressions(
        panel, factors=("first", "second"), label="label", minimum=50
    )

    counts = dict(monthly.select("date", "n_obs").unique().iter_rows())
    assert days[0] not in counts  # only 40 complete rows, below the minimum
    assert days[1] not in counts  # a constant regressor cannot be standardised
    assert counts[days[2]] == 79
    assert counts[days[3]] == 80


def test_duplicate_keys_and_a_label_used_as_regressor_are_rejected() -> None:
    panel = _panel(months=2, stocks=60)

    with pytest.raises(ValueError, match="duplicate"):
        monthly_cross_sectional_regressions(
            pl.concat([panel, panel.head(1)]), factors=("first",), label="label", minimum=20
        )
    with pytest.raises(ValueError, match="label"):
        monthly_cross_sectional_regressions(
            panel, factors=("first", "label"), label="label", minimum=20
        )


def test_table_runs_every_specification_in_both_periods_from_long_form_scores() -> None:
    from ashare_multifactor.supplements.fama_macbeth_pipeline import fama_macbeth_table

    wide = _panel(months=24, stocks=150)
    periods = {2006: "research_2005_2016", 2007: "validation_2017_2021"}
    long = pl.concat(
        [
            wide.select(
                pl.col("date").dt.year().replace_strict(periods).alias("period"),
                pl.lit(name).alias("object_name"),
                "date",
                "symbol",
                pl.col(name).alias("score_value"),
                pl.col("label").alias("forward_return_20"),
            )
            for name in ("first", "second")
        ]
    )

    table = fama_macbeth_table(
        long,
        specifications={"one": ["first"], "both": ["first", "second"]},
        label="forward_return_20",
        published_lag=0,
    )

    assert table.height == 2 * (1 + 2)
    assert set(table.get_column("period")) == set(periods.values())
    assert (
        table.filter(pl.col("specification") == "both").get_column("regressors").unique().item()
        == 2
    )
    assert table.get_column("months").unique().item() == 12
    with pytest.raises(ValueError, match="missing"):
        fama_macbeth_table(
            long, specifications={"bad": ["third"]}, label="forward_return_20", published_lag=0
        )
