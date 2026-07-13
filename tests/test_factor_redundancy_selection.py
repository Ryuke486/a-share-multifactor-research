from dataclasses import replace
from datetime import date
import polars as pl
import pytest
from scipy import stats

from ashare_multifactor.config import FactorResearchSettings
from ashare_multifactor.factors.definitions import FactorDefinition
from ashare_multifactor.research.factor_redundancy import analyze_factor_redundancy


NEUTRAL = FactorDefinition("neutral", "test", (), 0, 1)
PLAIN = FactorDefinition("plain", "test", (), 0, 1, size_neutralize=False)
MISSING = FactorDefinition("missing", "test", (), 0, 1)


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


def _factor_rows(
    day: date,
    definition: FactorDefinition,
    scores: list[float | None],
    *,
    neutral_scores: list[float | None] | None = None,
    symbols: list[str] | None = None,
) -> list[dict[str, object]]:
    neutral_scores = neutral_scores if neutral_scores is not None else scores
    symbols = symbols or [f"{index:06d}" for index in range(1, len(scores) + 1)]
    return [
        {
            "date": day,
            "symbol": symbol,
            "factor_name": definition.name,
            "family": definition.family,
            "score": score,
            "score_size_neutral": neutral_score,
        }
        for symbol, score, neutral_score in zip(symbols, scores, neutral_scores)
    ]


def _panel(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema={
            "date": pl.Date,
            "symbol": pl.String,
            "factor_name": pl.String,
            "family": pl.String,
            "score": pl.Float64,
            "score_size_neutral": pl.Float64,
        },
    )


def _correlation_row(
    correlations: pl.DataFrame,
    factor_a: str,
    factor_b: str,
) -> dict[str, object]:
    return correlations.filter(
        (pl.col("factor_a") == factor_a) & (pl.col("factor_b") == factor_b)
    ).row(0, named=True)


def test_redundancy_averages_monthly_cross_sections_instead_of_pooling_all_rows() -> None:
    ascending = [float(value) for value in range(20)]
    first_day = date(2005, 1, 31)
    second_day = date(2005, 2, 28)
    rows = _factor_rows(first_day, NEUTRAL, ascending)
    rows += _factor_rows(first_day, PLAIN, ascending)
    rows += _factor_rows(second_day, NEUTRAL, [value + 100.0 for value in ascending])
    rows += _factor_rows(second_day, PLAIN, [value + 100.0 for value in reversed(ascending)])
    panel = _panel(rows)

    result = analyze_factor_redundancy(panel, [NEUTRAL, PLAIN], _settings())
    pair = _correlation_row(result.factor_correlations, "neutral", "plain")
    pooled = stats.spearmanr(
        panel.filter(pl.col("factor_name") == "neutral")
        .sort("date", "symbol")
        .get_column("score_size_neutral"),
        panel.filter(pl.col("factor_name") == "plain").sort("date", "symbol").get_column("score"),
    ).statistic

    assert pair["primary_variant_a"] == "score_size_neutral"
    assert pair["primary_variant_b"] == "score"
    assert pair["common_months"] == 2
    assert pair["mean_correlation"] == pytest.approx(0.0, abs=1e-12)
    assert pooled != pytest.approx(pair["mean_correlation"], abs=1e-3)


def test_redundancy_outputs_complete_ordered_matrix_and_missing_pair_reasons() -> None:
    scores = [float(value) for value in range(20)]
    panel = _panel(
        _factor_rows(date(2005, 1, 31), NEUTRAL, scores)
        + _factor_rows(date(2005, 2, 28), PLAIN, scores)
    )

    correlations = analyze_factor_redundancy(
        panel,
        [NEUTRAL, PLAIN, MISSING],
        _settings(),
    ).factor_correlations

    assert correlations.height == 9
    assert correlations.select("factor_a", "factor_b").rows() == [
        (left, right)
        for left in ("neutral", "plain", "missing")
        for right in ("neutral", "plain", "missing")
    ]
    no_shared_date = _correlation_row(correlations, "neutral", "plain")
    no_data = _correlation_row(correlations, "neutral", "missing")
    assert no_shared_date["common_months"] == 0
    assert no_shared_date["reason"] == "no_common_dates"
    assert no_data["mean_correlation"] is None
    assert no_data["reason"] == "no_common_dates"


@pytest.mark.parametrize(
    ("plain_scores", "expected_reason"),
    [
        ([float(value) for value in range(19)] + [None], "insufficient_common_observations"),
        ([1.0] * 20, "constant_score"),
    ],
)
def test_redundancy_preserves_explicit_invalid_common_month_reason(
    plain_scores: list[float | None],
    expected_reason: str,
) -> None:
    scores = [float(value) for value in range(20)]
    panel = _panel(
        _factor_rows(date(2005, 1, 31), NEUTRAL, scores)
        + _factor_rows(date(2005, 1, 31), PLAIN, plain_scores)
    )

    pair = _correlation_row(
        analyze_factor_redundancy(panel, [NEUTRAL, PLAIN], _settings()).factor_correlations,
        "neutral",
        "plain",
    )

    assert pair["common_months"] == 0
    assert pair["mean_correlation"] is None
    assert pair["reason"] == f"no_valid_common_months:{expected_reason}"


def test_exact_redundancy_threshold_is_flagged_without_deleting_either_factor() -> None:
    scores = [float(value) for value in range(21)]
    permutation = list(scores)
    for left, right in ((0, 15), (1, 3), (4, 5), (6, 7)):
        permutation[left], permutation[right] = permutation[right], permutation[left]
    panel = _panel(
        _factor_rows(date(2005, 1, 31), NEUTRAL, scores)
        + _factor_rows(date(2005, 1, 31), PLAIN, permutation)
    )

    result = analyze_factor_redundancy(panel, [NEUTRAL, PLAIN], _settings())

    assert _correlation_row(result.factor_correlations, "neutral", "plain")[
        "mean_correlation"
    ] == pytest.approx(0.7)
    assert result.redundancy_flags.select(
        "factor_a",
        "factor_b",
        "mean_correlation",
        "correlation_direction",
        "common_months",
    ).rows() == [("neutral", "plain", pytest.approx(0.7), "positive", 1)]
    assert set(result.factor_correlations.get_column("factor_a")) == {"neutral", "plain"}


def test_redundancy_rejects_duplicate_keys_unknown_names_labels_and_out_of_period_rows() -> None:
    scores = [float(value) for value in range(20)]
    panel = _panel(_factor_rows(date(2005, 1, 31), NEUTRAL, scores))

    with pytest.raises(ValueError, match="duplicate"):
        analyze_factor_redundancy(pl.concat([panel, panel.head(1)]), [NEUTRAL], _settings())
    with pytest.raises(ValueError, match="unknown factor"):
        analyze_factor_redundancy(
            panel.with_columns(pl.lit("unknown").alias("factor_name")),
            [NEUTRAL],
            _settings(),
        )
    with pytest.raises(ValueError, match="forward-return labels"):
        analyze_factor_redundancy(
            panel.with_columns(pl.lit(0.1).alias("forward_return_20")),
            [NEUTRAL],
            _settings(),
        )
    with pytest.raises(ValueError, match="analysis period"):
        analyze_factor_redundancy(
            panel.with_columns(pl.lit(date(2017, 1, 31)).alias("date")),
            [NEUTRAL],
            _settings(),
        )


def test_redundancy_rejects_two_signal_dates_for_one_factor_in_the_same_month() -> None:
    scores = [float(value) for value in range(20)]
    panel = _panel(
        _factor_rows(date(2005, 1, 28), NEUTRAL, scores)
        + _factor_rows(date(2005, 1, 31), NEUTRAL, scores)
    )

    with pytest.raises(ValueError, match="one signal date per natural month"):
        analyze_factor_redundancy(panel, [NEUTRAL], _settings())


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("date", None, "keys must be non-null"),
        ("symbol", None, "keys must be non-null"),
        ("factor_name", None, "keys must be non-null"),
        ("symbol", "   ", "symbol must be non-blank"),
        ("factor_name", "", "factor_name must be non-blank"),
    ],
)
def test_redundancy_rejects_null_or_blank_keys(
    column: str,
    value: object,
    message: str,
) -> None:
    panel = _panel(
        _factor_rows(date(2005, 1, 31), NEUTRAL, [float(value) for value in range(20)])
    ).with_columns(pl.lit(value).cast(_panel_dtype(column)).alias(column))

    with pytest.raises(ValueError, match=message):
        analyze_factor_redundancy(panel, [NEUTRAL], _settings())


def _panel_dtype(column: str) -> pl.DataType:
    return {"date": pl.Date, "symbol": pl.String, "factor_name": pl.String}[column]
