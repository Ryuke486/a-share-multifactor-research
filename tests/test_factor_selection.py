from dataclasses import replace
from datetime import date

import polars as pl
import pytest

from ashare_multifactor.config import FactorResearchSettings
from ashare_multifactor.research.factor_selection import classify_factors


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


def _selection_summary(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema={
            "factor_name": pl.String,
            "family": pl.String,
            "score_variant": pl.String,
            "horizon": pl.Int64,
            "coverage": pl.Float64,
            "valid_months": pl.Int64,
            "mean_ic": pl.Float64,
            "bh_q": pl.Float64,
            "q5_q1": pl.Float64,
            "point_in_time_status": pl.String,
        },
    )


def _selection_row(
    factor_name: str,
    *,
    score_variant: str = "score_size_neutral",
    coverage: float | None = 0.9,
    valid_months: int = 120,
    mean_ic: float | None = 0.03,
    bh_q: float | None = 0.05,
    q5_q1: float | None = 0.01,
    point_in_time_status: str = "ready",
) -> dict[str, object]:
    return {
        "factor_name": factor_name,
        "family": "test",
        "score_variant": score_variant,
        "horizon": 20,
        "coverage": coverage,
        "valid_months": valid_months,
        "mean_ic": mean_ic,
        "bh_q": bh_q,
        "q5_q1": q5_q1,
        "point_in_time_status": point_in_time_status,
    }


def _subperiods(
    factor_name: str,
    score_variant: str,
    means: tuple[float | None, float | None, float | None] = (0.02, 0.01, 0.03),
) -> list[dict[str, object]]:
    periods = (
        ("2005-2008", date(2005, 1, 1), date(2008, 12, 31)),
        ("2009-2012", date(2009, 1, 1), date(2012, 12, 31)),
        ("2013-2016", date(2013, 1, 1), date(2016, 12, 31)),
    )
    return [
        {
            "factor_name": factor_name,
            "family": "test",
            "score_variant": score_variant,
            "subperiod": name,
            "start": start,
            "end": end,
            "valid_months": 48 if mean is not None else 0,
            "mean_ic": mean,
        }
        for (name, start, end), mean in zip(periods, means)
    ]


def test_classification_selects_neutral_primary_when_available_and_has_no_candidate_quota() -> None:
    summary = _selection_summary(
        [
            _selection_row(
                "neutral_factor",
                score_variant="score",
                mean_ic=-0.2,
                bh_q=None,
                q5_q1=-0.1,
            ),
            _selection_row("neutral_factor"),
            _selection_row("plain_factor", score_variant="score"),
        ]
    )
    subperiods = pl.DataFrame(
        _subperiods("neutral_factor", "score_size_neutral") + _subperiods("plain_factor", "score")
    )

    selected = classify_factors(summary, subperiods, _settings()).sort("factor_name")

    assert selected.select("factor_name", "primary_score_variant", "classification").rows() == [
        ("neutral_factor", "score_size_neutral", "candidate"),
        ("plain_factor", "score", "candidate"),
    ]
    assert selected.get_column("reason").to_list() == [
        "candidate:all_thresholds_passed",
        "candidate:all_thresholds_passed",
    ]
    assert (
        selected.select(
            "coverage_pass",
            "valid_months_pass",
            "mean_ic_positive",
            "fdr_pass",
            "q5_q1_positive",
            "subperiod_stability_pass",
            "point_in_time_verified",
        )
        .to_numpy()
        .all()
    )


@pytest.mark.parametrize(
    ("overrides", "subperiod_means", "failed_column", "expected_reason"),
    [
        ({"bh_q": 0.2}, (0.02, 0.01, 0.03), "fdr_pass", "watch:fdr_threshold_not_met"),
        (
            {"q5_q1": 0.0},
            (0.02, 0.01, 0.03),
            "q5_q1_positive",
            "watch:non_positive_or_missing_q5_q1",
        ),
        (
            {},
            (0.02, -0.01, -0.03),
            "subperiod_stability_pass",
            "watch:subperiod_stability_not_met",
        ),
        (
            {"point_in_time_status": "unverified"},
            (0.02, 0.01, 0.03),
            "point_in_time_verified",
            "watch:point_in_time_unverified",
        ),
    ],
)
def test_classification_watch_reasons_are_explicit(
    overrides: dict[str, object],
    subperiod_means: tuple[float, float, float],
    failed_column: str,
    expected_reason: str,
) -> None:
    summary = _selection_summary([_selection_row("factor", **overrides)])
    subperiods = pl.DataFrame(_subperiods("factor", "score_size_neutral", subperiod_means))

    selected = classify_factors(summary, subperiods, _settings()).row(0, named=True)

    assert selected["classification"] == "watch"
    assert selected[failed_column] is False
    assert selected["reason"] == expected_reason


@pytest.mark.parametrize(
    ("overrides", "failed_column", "expected_reason"),
    [
        (
            {"coverage": 0.79},
            "coverage_pass",
            "reject:coverage_below_or_missing_threshold",
        ),
        (
            {"valid_months": 1},
            "valid_months_pass",
            "reject:insufficient_valid_months",
        ),
        (
            {"mean_ic": 0.0},
            "mean_ic_positive",
            "reject:non_positive_or_missing_mean_ic",
        ),
        (
            {"mean_ic": None},
            "mean_ic_positive",
            "reject:non_positive_or_missing_mean_ic",
        ),
    ],
)
def test_classification_rejects_each_failed_core_gate(
    overrides: dict[str, object],
    failed_column: str,
    expected_reason: str,
) -> None:
    summary = _selection_summary([_selection_row("factor", **overrides)])
    subperiods = pl.DataFrame(_subperiods("factor", "score_size_neutral"))

    selected = classify_factors(summary, subperiods, _settings()).row(0, named=True)

    assert selected["classification"] == "reject"
    assert selected[failed_column] is False
    assert selected["reason"] == expected_reason


def test_classification_uses_status_not_factor_name_for_unverified_cap() -> None:
    summary = _selection_summary(
        [
            _selection_row("ep_ttm", point_in_time_status="ready"),
            _selection_row("ordinary", point_in_time_status="unverified"),
        ]
    )
    subperiods = pl.DataFrame(
        _subperiods("ep_ttm", "score_size_neutral") + _subperiods("ordinary", "score_size_neutral")
    )

    selected = classify_factors(summary, subperiods, _settings()).sort("factor_name")

    assert selected.select("factor_name", "classification").rows() == [
        ("ep_ttm", "candidate"),
        ("ordinary", "watch"),
    ]


def test_classification_ignores_non_preregistered_subperiod_rows() -> None:
    summary = _selection_summary([_selection_row("factor")])
    subperiods = pl.DataFrame(
        [
            {
                "factor_name": "factor",
                "score_variant": "score_size_neutral",
                "subperiod": name,
                "mean_ic": 0.5,
            }
            for name in ("extra-period-a", "extra-period-b")
        ]
    )

    selected = classify_factors(summary, subperiods, _settings()).row(0, named=True)

    assert selected["positive_subperiods"] == 0
    assert selected["subperiod_stability_pass"] is False
    assert selected["classification"] == "watch"
    assert selected["reason"] == "watch:subperiod_stability_not_met"


def test_classification_rejects_a_summary_that_omits_a_factor_primary_row() -> None:
    summary = _selection_summary([_selection_row("factor")]).with_columns(
        pl.lit(5).alias("horizon")
    )
    subperiods = pl.DataFrame(_subperiods("factor", "score_size_neutral"))

    with pytest.raises(ValueError, match="exactly one.*primary summary row"):
        classify_factors(summary, subperiods, _settings())
