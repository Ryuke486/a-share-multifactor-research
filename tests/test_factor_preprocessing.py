from dataclasses import replace
from datetime import date

import numpy as np
import polars as pl
import pytest
from polars.testing import assert_frame_equal

from ashare_multifactor.config import FactorResearchSettings
from ashare_multifactor.data.field_audit import FieldReadiness
from ashare_multifactor.factors import panel as panel_module
from ashare_multifactor.factors.definitions import FACTOR_DEFINITIONS
from ashare_multifactor.factors.panel import build_monthly_factor_panel
from ashare_multifactor.research.factor_preprocessing import preprocess_factor_panel


RAW_PANEL_SCHEMA = {
    "date": pl.Date,
    "symbol": pl.String,
    "factor_name": pl.String,
    "family": pl.String,
    "raw_value": pl.Float64,
}
PREPROCESSED_SCHEMA = {
    **RAW_PANEL_SCHEMA,
    "winsorized_value": pl.Float64,
    "score": pl.Float64,
    "score_size_neutral": pl.Float64,
    "score_industry_size_neutral": pl.Float64,
    "point_in_time_status": pl.String,
    "preprocessing_reason": pl.String,
}


def _settings(**overrides: object) -> FactorResearchSettings:
    settings = FactorResearchSettings(
        data_start=date(2003, 1, 1),
        analysis_start=date(2005, 1, 1),
        analysis_end=date(2005, 2, 28),
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
        minimum_valid_months=120,
        fdr_q_threshold=0.1,
        redundancy_threshold=0.7,
    )
    return replace(settings, **overrides)


def _daily_row(day: date, symbol: str, seed: float) -> dict[str, object]:
    close = 100.0 + seed
    return {
        "date": day,
        "symbol": symbol,
        "open_raw": close - 1.0,
        "high_raw": close + 1.0,
        "low_raw": close - 2.0,
        "close_raw": close,
        "close_adj": close,
        "prev_close_adj": close - 0.5,
        "volume": 1_000.0,
        "amount": 100_000.0 + seed,
        "turnover_rate": 0.01 + seed / 100_000.0,
        "total_market_cap": 1_000_000.0 + seed,
        "pe_ttm": 10.0 + seed / 100.0,
        "pb": 2.0 + seed / 1_000.0,
        "ps_ttm": 3.0 + seed / 1_000.0,
        "is_st": False,
        "industry": "synthetic-only",
        "forward_return_20": 999.0,
        "unrelated_column": "must-not-reach-factor-functions",
    }


def _daily_panel() -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    trading_days = (
        date(2004, 12, 30),
        date(2004, 12, 31),
        date(2005, 1, 28),
        date(2005, 1, 31),
    )
    for day_index, day in enumerate(trading_days):
        rows.append(_daily_row(day, "000001", float(day_index)))
        if day != date(2005, 1, 31):
            rows.append(_daily_row(day, "000002", float(day_index + 10)))
    return pl.DataFrame(rows)


def _readiness(panel: pl.DataFrame, *, industry_enabled: bool = False) -> FieldReadiness:
    names = panel.get_column("factor_name").unique().to_list()
    return FieldReadiness(
        factor_status={
            name: "unverified" if name in {"ep_ttm", "bp", "sp_ttm"} else "ready" for name in names
        },
        industry_neutralization_enabled=industry_enabled,
        field_metrics={},
    )


def _raw_rows(
    day: date,
    factor_name: str,
    values: list[float | None],
    *,
    symbols: list[str] | None = None,
) -> list[dict[str, object]]:
    definition = next(
        definition for definition in FACTOR_DEFINITIONS if definition.name == factor_name
    )
    symbols = symbols or [f"{index:06d}" for index in range(1, len(values) + 1)]
    return [
        {
            "date": day,
            "symbol": symbol,
            "factor_name": factor_name,
            "family": definition.family,
            "raw_value": value,
        }
        for symbol, value in zip(symbols, values, strict=True)
    ]


def _factor_with_size_panel(
    day: date,
    factor_name: str,
    factor_values: list[float | None],
    size_values: list[float | None],
    *,
    factor_symbols: list[str] | None = None,
    size_symbols: list[str] | None = None,
) -> pl.DataFrame:
    rows = _raw_rows(day, factor_name, factor_values, symbols=factor_symbols)
    rows.extend(_raw_rows(day, "log_market_cap", size_values, symbols=size_symbols))
    return pl.DataFrame(rows, schema=RAW_PANEL_SCHEMA)


def test_monthly_panel_uses_the_market_month_end_and_retains_null_factors() -> None:
    actual = build_monthly_factor_panel(_daily_panel(), FACTOR_DEFINITIONS, _settings())

    assert actual.schema == pl.Schema(RAW_PANEL_SCHEMA)
    assert actual.get_column("date").unique().to_list() == [date(2005, 1, 31)]
    assert actual.get_column("symbol").unique().to_list() == ["000001"]
    assert actual.height == len(FACTOR_DEFINITIONS)
    assert actual.get_column("factor_name").to_list() == sorted(
        definition.name for definition in FACTOR_DEFINITIONS
    )
    assert actual.get_column("raw_value").null_count() > 0


def test_monthly_panel_has_a_unique_stably_sorted_primary_key() -> None:
    actual = build_monthly_factor_panel(_daily_panel(), FACTOR_DEFINITIONS, _settings())

    assert not actual.select("date", "symbol", "factor_name").is_duplicated().any()
    assert actual.equals(actual.sort("date", "symbol", "factor_name"))


def test_monthly_panel_rejects_rows_after_the_analysis_end_before_computation() -> None:
    frame = pl.concat(
        [_daily_panel(), pl.DataFrame([_daily_row(date(2005, 3, 1), "000001", 99.0)])],
        how="diagonal_relaxed",
    )

    with pytest.raises(ValueError, match="after factor research analysis_end"):
        build_monthly_factor_panel(frame, FACTOR_DEFINITIONS, _settings())


def test_each_family_receives_only_keys_and_its_registered_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = tuple(
        next(definition for definition in FACTOR_DEFINITIONS if definition.family == family)
        for family in ("value", "momentum", "reversal", "liquidity", "low_volatility", "size")
    )
    seen: dict[str, tuple[str, ...]] = {}

    for definition in selected:
        expected_columns = ("date", "symbol", *definition.source_columns)

        def spy(
            frame: pl.DataFrame,
            *,
            family: str = definition.family,
            factor_name: str = definition.name,
            expected: tuple[str, ...] = expected_columns,
        ) -> pl.DataFrame:
            seen[family] = tuple(frame.columns)
            assert tuple(frame.columns) == expected
            return frame.select("date", "symbol").with_columns(pl.lit(1.0).alias(factor_name))

        monkeypatch.setitem(panel_module._FAMILY_COMPUTERS, definition.family, spy)

    actual = build_monthly_factor_panel(
        pl.DataFrame([_daily_row(date(2005, 1, 31), "000001", 1.0)]),
        selected,
        _settings(),
    )

    assert set(seen) == {definition.family for definition in selected}
    assert actual.get_column("factor_name").to_list() == sorted(
        definition.name for definition in selected
    )
    assert not any("forward_return" in column for columns in seen.values() for column in columns)


def test_universe_builder_receives_no_factor_or_label_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = (
        "date",
        "symbol",
        "close_adj",
        "volume",
        "amount",
        "is_st",
        "open_raw",
        "high_raw",
        "low_raw",
        "close_raw",
    )
    original = panel_module.build_research_universe

    def spy(frame: pl.DataFrame, settings: FactorResearchSettings) -> pl.DataFrame:
        assert tuple(frame.columns) == expected
        return original(frame, settings)

    monkeypatch.setattr(panel_module, "build_research_universe", spy)

    build_monthly_factor_panel(_daily_panel(), FACTOR_DEFINITIONS, _settings())


def test_preprocessing_uses_linear_same_date_winsorization_only() -> None:
    first_day = date(2005, 1, 31)
    future_day = date(2005, 2, 28)
    first = _factor_with_size_panel(
        first_day,
        "ep_ttm",
        [0.0, 1.0, 2.0, 3.0, 100.0],
        [1.0, 2.0, 3.0, 4.0, 5.0],
    )
    future = _factor_with_size_panel(
        future_day,
        "ep_ttm",
        [0.0, 1.0, 2.0, 3.0, 1e12],
        [1.0, 2.0, 3.0, 4.0, 5.0],
    )

    baseline = preprocess_factor_panel(first, _readiness(first), _settings())
    combined_panel = pl.concat([first, future])
    combined = preprocess_factor_panel(
        combined_panel,
        _readiness(combined_panel),
        _settings(),
    ).filter(pl.col("date") == first_day)

    first_factor = baseline.filter(pl.col("factor_name") == "ep_ttm").sort("symbol")
    assert first_factor.get_column("winsorized_value").to_list() == pytest.approx(
        [0.04, 1.0, 2.0, 3.0, 96.12]
    )
    assert_frame_equal(combined, baseline)


def test_negative_direction_reverses_cross_sectional_score_order() -> None:
    panel = _factor_with_size_panel(
        date(2005, 1, 31),
        "reversal_5",
        [1.0, 2.0, 3.0],
        [1.0, 3.0, 2.0],
    )

    actual = preprocess_factor_panel(panel, _readiness(panel), _settings()).filter(
        pl.col("factor_name") == "reversal_5"
    )

    scores = actual.sort("raw_value").get_column("score").to_list()
    assert scores[0] > scores[1] > scores[2]


def test_non_finite_raw_values_are_nulled_with_stable_reasons() -> None:
    panel = _factor_with_size_panel(
        date(2005, 1, 31),
        "ep_ttm",
        [1.0, None, float("nan"), float("inf"), -float("inf"), 2.0, 4.0],
        [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0],
    )

    actual = preprocess_factor_panel(panel, _readiness(panel), _settings()).filter(
        pl.col("factor_name") == "ep_ttm"
    )

    assert actual.get_column("raw_value").to_list() == [
        1.0,
        None,
        None,
        None,
        None,
        2.0,
        4.0,
    ]
    assert actual.get_column("preprocessing_reason").to_list() == [
        None,
        "missing_raw_value",
        "non_finite_raw_value",
        "non_finite_raw_value",
        "non_finite_raw_value",
        None,
        None,
    ]
    assert actual.get_column("score").to_list()[1:5] == [None, None, None, None]


def test_small_and_constant_cross_sections_return_null_scores_with_reasons() -> None:
    first_day = date(2005, 1, 31)
    second_day = date(2005, 2, 28)
    rows = _raw_rows(first_day, "ep_ttm", [1.0, None])
    rows.extend(_raw_rows(second_day, "bp", [2.0, 2.0, 2.0]))
    panel = pl.DataFrame(rows, schema=RAW_PANEL_SCHEMA)

    actual = preprocess_factor_panel(panel, _readiness(panel), _settings())

    first = actual.filter(pl.col("date") == first_day).sort("symbol")
    second = actual.filter(pl.col("date") == second_day)
    assert first.get_column("score").to_list() == [None, None]
    assert first.get_column("preprocessing_reason").to_list() == [
        "insufficient_valid_values",
        "missing_raw_value",
    ]
    assert second.get_column("score").to_list() == [None, None, None]
    assert second.get_column("preprocessing_reason").to_list() == [
        "zero_score_variance",
        "zero_score_variance",
        "zero_score_variance",
    ]


def test_small_scale_nonconstant_values_are_standardized() -> None:
    panel = _factor_with_size_panel(
        date(2005, 1, 31),
        "amihud_20",
        [1e-15, 2e-15, 4e-15],
        [1.0, 3.0, 2.0],
    )

    actual = preprocess_factor_panel(panel, _readiness(panel), _settings()).filter(
        pl.col("factor_name") == "amihud_20"
    )

    assert actual.get_column("score").null_count() == 0


def test_size_neutral_residual_is_standardized_and_orthogonal_to_size() -> None:
    panel = _factor_with_size_panel(
        date(2005, 1, 31),
        "ep_ttm",
        [1.0, 2.0, 4.0, 8.0, 16.0],
        [1.0, 2.0, 3.0, 4.0, 5.0],
    )

    actual = preprocess_factor_panel(panel, _readiness(panel), _settings())
    factor = actual.filter(pl.col("factor_name") == "ep_ttm").sort("symbol")
    size = actual.filter(pl.col("factor_name") == "log_market_cap").sort("symbol")
    residual = np.asarray(factor.get_column("score_size_neutral"), dtype=float)
    size_score = np.asarray(size.get_column("score"), dtype=float)

    assert residual.mean() == pytest.approx(0.0, abs=1e-12)
    assert residual.std(ddof=0) == pytest.approx(1.0, abs=1e-12)
    assert np.corrcoef(residual, size_score)[0, 1] == pytest.approx(0.0, abs=1e-12)


def test_missing_size_score_is_a_row_level_failure() -> None:
    symbols = ["000001", "000002", "000003", "000004"]
    panel = _factor_with_size_panel(
        date(2005, 1, 31),
        "ep_ttm",
        [1.0, 2.0, 4.0, 8.0],
        [1.0, 2.0, 3.0],
        factor_symbols=symbols,
        size_symbols=symbols[:3],
    )

    row = (
        preprocess_factor_panel(panel, _readiness(panel), _settings())
        .filter((pl.col("factor_name") == "ep_ttm") & (pl.col("symbol") == "000004"))
        .row(0, named=True)
    )

    assert row["score_size_neutral"] is None
    assert row["preprocessing_reason"] == "missing_size_score"


def test_size_neutralization_requires_three_common_observations() -> None:
    panel = _factor_with_size_panel(
        date(2005, 1, 31),
        "ep_ttm",
        [1.0, 2.0],
        [1.0, 2.0],
    )

    actual = preprocess_factor_panel(panel, _readiness(panel), _settings()).filter(
        pl.col("factor_name") == "ep_ttm"
    )

    assert actual.get_column("score_size_neutral").to_list() == [None, None]
    assert actual.get_column("preprocessing_reason").to_list() == [
        "insufficient_size_samples",
        "insufficient_size_samples",
    ]


def test_singular_size_control_is_recorded_instead_of_fitted() -> None:
    factor_symbols = ["000001", "000002", "000003"]
    size_symbols = ["000001", "000002", "000003", "000004", "000005"]
    panel = _factor_with_size_panel(
        date(2005, 1, 31),
        "ep_ttm",
        [1.0, 2.0, 4.0],
        [1.0, 1.0, 1.0, 2.0, 3.0],
        factor_symbols=factor_symbols,
        size_symbols=size_symbols,
    )

    actual = preprocess_factor_panel(panel, _readiness(panel), _settings()).filter(
        pl.col("factor_name") == "ep_ttm"
    )

    assert actual.get_column("score_size_neutral").to_list() == [None, None, None]
    assert actual.get_column("preprocessing_reason").to_list() == [
        "singular_size_control",
        "singular_size_control",
        "singular_size_control",
    ]


def test_constant_size_residual_is_recorded_instead_of_standardized() -> None:
    panel = _factor_with_size_panel(
        date(2005, 1, 31),
        "ep_ttm",
        [1.0, 2.0, 3.0, 4.0],
        [1.0, 2.0, 3.0, 4.0],
    )

    actual = preprocess_factor_panel(panel, _readiness(panel), _settings()).filter(
        pl.col("factor_name") == "ep_ttm"
    )

    assert actual.get_column("score_size_neutral").to_list() == [None, None, None, None]
    assert actual.get_column("preprocessing_reason").to_list() == [
        "constant_size_residual",
        "constant_size_residual",
        "constant_size_residual",
        "constant_size_residual",
    ]


def test_size_factor_uses_score_and_is_not_neutralized_against_itself() -> None:
    panel = pl.DataFrame(
        _raw_rows(date(2005, 1, 31), "log_market_cap", [1.0, 2.0, 3.0]),
        schema=RAW_PANEL_SCHEMA,
    )

    actual = preprocess_factor_panel(panel, _readiness(panel), _settings())

    assert actual.get_column("score").null_count() == 0
    assert actual.get_column("score_size_neutral").to_list() == [None, None, None]
    assert actual.get_column("preprocessing_reason").to_list() == [None, None, None]


def test_disabled_industry_gate_returns_typed_nulls_and_exact_schema() -> None:
    panel = _factor_with_size_panel(
        date(2005, 1, 31),
        "ep_ttm",
        [1.0, 2.0, 4.0, 8.0, 16.0],
        [1.0, 2.0, 3.0, 4.0, 5.0],
    )

    actual = preprocess_factor_panel(panel, _readiness(panel), _settings())

    assert actual.schema == pl.Schema(PREPROCESSED_SCHEMA)
    assert actual.get_column("score_industry_size_neutral").null_count() == actual.height
    assert actual.filter(pl.col("factor_name") == "ep_ttm").get_column(
        "point_in_time_status"
    ).unique().to_list() == ["unverified"]
    assert (
        actual.filter(pl.col("factor_name") == "ep_ttm")
        .get_column("preprocessing_reason")
        .null_count()
        == 5
    )


def test_enabled_industry_gate_is_rejected_without_approved_historical_input() -> None:
    panel = _factor_with_size_panel(
        date(2005, 1, 31),
        "ep_ttm",
        [1.0, 2.0, 4.0],
        [1.0, 2.0, 3.0],
    )

    with pytest.raises(ValueError, match="approved historical industry input"):
        preprocess_factor_panel(
            panel,
            _readiness(panel, industry_enabled=True),
            _settings(),
        )


def test_missing_point_in_time_mapping_is_rejected_immediately() -> None:
    panel = pl.DataFrame(
        _raw_rows(date(2005, 1, 31), "ep_ttm", [1.0, 2.0]),
        schema=RAW_PANEL_SCHEMA,
    )
    readiness = FieldReadiness(
        factor_status={},
        industry_neutralization_enabled=False,
        field_metrics={},
    )

    with pytest.raises(ValueError, match="missing point-in-time readiness"):
        preprocess_factor_panel(panel, readiness, _settings())
