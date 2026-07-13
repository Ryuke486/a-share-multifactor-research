from dataclasses import replace
from datetime import date
import math

import polars as pl
import pytest

from ashare_multifactor.config import FactorResearchSettings
from ashare_multifactor.factors.definitions import FactorDefinition
from ashare_multifactor.research.factor_evaluation import evaluate_factors
from ashare_multifactor.research.factor_statistics import benjamini_hochberg


ALPHA = FactorDefinition("alpha", "test", (), 0, 1)
SIZE = FactorDefinition("size_control", "size", (), 0, -1, size_neutralize=False)
MISSING = FactorDefinition("missing_factor", "test", (), 0, 1)


def _settings(**overrides: object) -> FactorResearchSettings:
    settings = FactorResearchSettings(
        data_start=date(2003, 1, 1),
        analysis_start=date(2005, 1, 1),
        analysis_end=date(2016, 12, 31),
        universe_size=1_000,
        minimum_history=1,
        liquidity_lookback=1,
        require_valid_trade_observation=True,
        signal_frequency="month_end",
        forward_horizons=(5, 20, 60),
        primary_horizon=20,
        winsor_lower=0.01,
        winsor_upper=0.99,
        quantile_count=5,
        minimum_coverage=0.8,
        minimum_valid_months=2,
        fdr_q_threshold=0.1,
        redundancy_threshold=0.7,
    )
    return replace(settings, **overrides)


def _rows(
    day: date,
    definition: FactorDefinition,
    scores: list[float | None],
    returns_20: list[float | None],
    *,
    returns_5: list[float | None] | None = None,
    returns_60: list[float | None] | None = None,
    symbols: list[str] | None = None,
    neutral_scores: list[float | None] | None = None,
    point_in_time_status: str = "ready",
) -> list[dict[str, object]]:
    size = len(scores)
    symbols = symbols or [f"{index:06d}" for index in range(1, size + 1)]
    returns_5 = returns_5 or returns_20
    returns_60 = returns_60 or returns_20
    neutral_scores = neutral_scores or scores
    return [
        {
            "date": day,
            "symbol": symbol,
            "factor_name": definition.name,
            "family": definition.family,
            "score": score,
            "score_size_neutral": neutral,
            "point_in_time_status": point_in_time_status,
            "forward_return_5": forward_5,
            "forward_return_20": forward_20,
            "forward_return_60": forward_60,
        }
        for symbol, score, neutral, forward_5, forward_20, forward_60 in zip(
            symbols,
            scores,
            neutral_scores,
            returns_5,
            returns_20,
            returns_60,
        )
    ]


def _frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema={
            "date": pl.Date,
            "symbol": pl.String,
            "factor_name": pl.String,
            "family": pl.String,
            "score": pl.Float64,
            "score_size_neutral": pl.Float64,
            "point_in_time_status": pl.String,
            "forward_return_5": pl.Float64,
            "forward_return_20": pl.Float64,
            "forward_return_60": pl.Float64,
        },
    )


def _summary_row(
    summary: pl.DataFrame,
    factor_name: str,
    score_variant: str,
    horizon: int,
) -> dict[str, object]:
    return summary.filter(
        (pl.col("factor_name") == factor_name)
        & (pl.col("score_variant") == score_variant)
        & (pl.col("horizon") == horizon)
    ).row(0, named=True)


def test_evaluation_rejects_missing_labels_duplicate_keys_and_out_of_period_dates() -> None:
    rows = _rows(date(2005, 1, 31), ALPHA, list(range(20)), list(range(20)))
    panel = _frame(rows)

    with pytest.raises(ValueError, match="forward_return_60"):
        evaluate_factors(panel.drop("forward_return_60"), [ALPHA], _settings())
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_factors(pl.concat([panel, panel.head(1)]), [ALPHA], _settings())
    with pytest.raises(ValueError, match="analysis period"):
        evaluate_factors(
            panel.with_columns(pl.lit(date(2017, 1, 31)).alias("date")),
            [ALPHA],
            _settings(),
        )


def test_rank_ic_reports_exact_correlations_and_explicit_invalid_reasons() -> None:
    ascending = [float(value) for value in range(20)]
    rows = _rows(
        date(2005, 1, 31),
        ALPHA,
        ascending,
        list(reversed(ascending)),
        returns_5=ascending,
        returns_60=[1.0] * 20,
    )
    rows += _rows(
        date(2005, 2, 28),
        ALPHA,
        ascending[:19],
        ascending[:19],
    )
    rows += _rows(
        date(2005, 3, 31),
        ALPHA,
        [1.0] * 20,
        ascending,
    )
    result = evaluate_factors(_frame(rows), [ALPHA], _settings())

    ic = result.rank_ic.filter(pl.col("score_variant") == "score")
    assert ic.filter((pl.col("date") == date(2005, 1, 31)) & (pl.col("horizon") == 5)).item(
        0, "rank_ic"
    ) == pytest.approx(1.0)
    assert ic.filter((pl.col("date") == date(2005, 1, 31)) & (pl.col("horizon") == 20)).item(
        0, "rank_ic"
    ) == pytest.approx(-1.0)
    assert (
        ic.filter((pl.col("date") == date(2005, 1, 31)) & (pl.col("horizon") == 60)).item(
            0, "reason"
        )
        == "constant_forward_return"
    )
    assert (
        ic.filter(pl.col("date") == date(2005, 2, 28)).get_column("reason").to_list()
        == ["insufficient_observations"] * 3
    )
    assert (
        ic.filter(pl.col("date") == date(2005, 3, 31)).get_column("reason").to_list()
        == ["constant_score"] * 3
    )


def test_spearman_ties_use_average_ranks_and_only_common_finite_observations() -> None:
    tied = [0.0] * 10 + [1.0] * 10
    panel = _frame(
        _rows(
            date(2005, 1, 31),
            ALPHA,
            tied + [2.0],
            tied + [None],
        )
    )

    result = evaluate_factors(panel, [ALPHA], _settings())
    ic_row = result.rank_ic.filter(
        (pl.col("score_variant") == "score") & (pl.col("horizon") == 20)
    ).row(0, named=True)
    summary = _summary_row(result.factor_summary, ALPHA.name, "score", 20)

    assert ic_row["n_obs"] == 20
    assert ic_row["rank_ic"] == pytest.approx(1.0)
    assert summary["coverage"] == pytest.approx(1.0)


def test_quantiles_are_nearly_equal_and_symbol_stable_when_scores_tie() -> None:
    scores = [1.0] * 23
    labels = [float(value) for value in range(1, 24)]
    result = evaluate_factors(
        _frame(_rows(date(2005, 1, 31), ALPHA, scores, labels)),
        [ALPHA],
        _settings(),
    )
    quantiles = result.quantile_returns.filter(pl.col("score_variant") == "score").sort("quantile")

    assert quantiles.get_column("n_obs").to_list() == [5, 5, 4, 5, 4]
    assert quantiles.get_column("mean_forward_return_20").to_list() == pytest.approx(
        [3.0, 8.0, 12.5, 17.0, 21.5]
    )
    summary = _summary_row(result.factor_summary, ALPHA.name, "score", 20)
    assert summary["q5_q1"] == pytest.approx(18.5)
    assert summary["monotonicity"] == pytest.approx(1.0)


def test_top_twenty_percent_turnover_uses_equal_weight_member_sets() -> None:
    first_symbols = [f"{value:06d}" for value in range(1, 21)]
    second_symbols = [f"{value:06d}" for value in range(2, 22)]
    rows = _rows(
        date(2005, 1, 31),
        ALPHA,
        list(range(20)),
        list(range(20)),
        symbols=first_symbols,
    )
    rows += _rows(
        date(2005, 2, 28),
        ALPHA,
        list(range(20)),
        list(range(20)),
        symbols=second_symbols,
    )

    turnover = evaluate_factors(_frame(rows), [ALPHA], _settings()).factor_turnover.filter(
        pl.col("score_variant") == "score"
    )
    first, second = turnover.sort("date").iter_rows(named=True)

    assert first["previous_date"] is None
    assert first["turnover"] is None
    assert first["top_count"] == 4
    assert second["previous_date"] == date(2005, 1, 31)
    assert second["previous_top_count"] == 4
    assert second["turnover"] == pytest.approx(0.25)


def test_decay_subperiods_and_empty_registered_combinations_are_retained() -> None:
    ascending = [float(value) for value in range(20)]
    rows: list[dict[str, object]] = []
    for day in (date(2005, 6, 30), date(2009, 6, 30), date(2013, 6, 30)):
        rows += _rows(day, ALPHA, ascending, ascending)

    result = evaluate_factors(_frame(rows), [ALPHA, MISSING], _settings())
    alpha_summary = result.factor_summary.filter(pl.col("factor_name") == ALPHA.name)
    missing_summary = result.factor_summary.filter(pl.col("factor_name") == MISSING.name)

    assert alpha_summary.select("score_variant", "horizon").rows() == [
        ("score", 5),
        ("score", 20),
        ("score", 60),
        ("score_size_neutral", 5),
        ("score_size_neutral", 20),
        ("score_size_neutral", 60),
    ]
    assert alpha_summary.get_column("mean_ic").to_list() == pytest.approx([1.0] * 6)
    subperiods = result.subperiod_metrics.filter(
        (pl.col("factor_name") == ALPHA.name) & (pl.col("score_variant") == "score_size_neutral")
    ).sort("start")
    assert subperiods.get_column("valid_months").to_list() == [1, 1, 1]
    assert subperiods.get_column("mean_ic").to_list() == pytest.approx([1.0, 1.0, 1.0])
    assert missing_summary.height == 6
    assert missing_summary.get_column("coverage").null_count() == 6
    assert missing_summary.get_column("summary_reason").unique().to_list() == ["no_eligible_rows"]


def test_bh_is_applied_only_to_valid_preregistered_primary_rows() -> None:
    ascending = [float(value) for value in range(20)]
    descending = list(reversed(ascending))
    rotated = ascending[1:] + ascending[:1]
    rows: list[dict[str, object]] = []
    for day, alpha_returns, size_returns in zip(
        (date(2005, 1, 31), date(2005, 2, 28), date(2005, 3, 31)),
        (ascending, descending, rotated),
        (ascending, ascending, descending),
    ):
        rows += _rows(day, ALPHA, ascending, alpha_returns)
        rows += _rows(day, SIZE, ascending, size_returns)

    summary = evaluate_factors(_frame(rows), [ALPHA, SIZE], _settings()).factor_summary
    primary = summary.filter(pl.col("bh_q").is_not_null()).sort("factor_name")

    assert primary.select("factor_name", "score_variant", "horizon").rows() == [
        (ALPHA.name, "score_size_neutral", 20),
        (SIZE.name, "score", 20),
    ]
    expected_q = benjamini_hochberg(primary.get_column("nw_p").to_list())
    assert primary.get_column("bh_q").to_list() == pytest.approx(expected_q)
    assert (
        summary.filter(
            ~(
                (
                    (pl.col("factor_name") == ALPHA.name)
                    & (pl.col("score_variant") == "score_size_neutral")
                    & (pl.col("horizon") == 20)
                )
                | (
                    (pl.col("factor_name") == SIZE.name)
                    & (pl.col("score_variant") == "score")
                    & (pl.col("horizon") == 20)
                )
            )
        )
        .get_column("bh_q")
        .null_count()
        == 7
    )

    alpha = _summary_row(summary, ALPHA.name, "score_size_neutral", 20)
    ic_values = [1.0, -1.0, 0.7142857142857143]
    expected_mean = sum(ic_values) / 3.0
    expected_std = math.sqrt(sum((value - expected_mean) ** 2 for value in ic_values) / 2.0)
    assert alpha["mean_ic"] == pytest.approx(expected_mean)
    assert alpha["icir"] == pytest.approx(expected_mean / expected_std * math.sqrt(12.0))
