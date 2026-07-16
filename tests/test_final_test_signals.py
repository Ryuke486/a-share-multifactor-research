from dataclasses import replace
from datetime import date
import inspect
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from ashare_multifactor.config import load_config
from ashare_multifactor.final_test.gate import FinalTestAuthorization
from ashare_multifactor.final_test.signals import (
    _build_rolling_targets,
    _realization_dates,
    _resolve_authorized_data,
    build_final_test_signals,
)


FINAL_START = date(2022, 1, 1)
FINAL_END = date(2025, 12, 31)


def _authorization() -> FinalTestAuthorization:
    return FinalTestAuthorization(
        attempt_id="attempt-001",
        approval_id="approved-stage9",
        registered_at="2026-07-16T00:00:00+00:00",
        git_commit="a" * 40,
        git_tree="b" * 40,
        sealed_protocol_sha256="c" * 64,
        robustness_release="stage8-release",
        test_period=(FINAL_START, FINAL_END),
    )


def test_signals_require_authorization_before_resolving_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def forbidden(_root: Path) -> None:
        nonlocal called
        called = True
        raise AssertionError("final-test data must not be resolved")

    monkeypatch.setattr(
        "ashare_multifactor.final_test.signals.resolve_final_test_data_panel",
        forbidden,
    )
    with pytest.raises(TypeError, match="FinalTestAuthorization"):
        build_final_test_signals(
            None,
            code_root=tmp_path / "code",
            final_root=tmp_path,
        )

    assert called is False


def test_signals_revalidate_execution_identity_before_resolving_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved = False
    frozen = load_config(Path("configs/research_protocol.yaml"))
    frozen = replace(
        frozen,
        paths=replace(frozen.paths, processed=tmp_path / "processed"),
    )

    def reject_identity(*_args: object, **_kwargs: object) -> None:
        raise ValueError("current Git commit/tree differs from final-test authorization")

    def forbidden(_root: Path) -> None:
        nonlocal resolved
        resolved = True
        raise AssertionError("data resolution must wait for execution identity")

    monkeypatch.setattr(
        "ashare_multifactor.final_test.signals._verify_data_authorization",
        reject_identity,
        raising=False,
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.signals._load_frozen_config",
        lambda _code_root, _data_root: frozen,
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.signals.resolve_final_test_data_panel",
        forbidden,
    )

    with pytest.raises(ValueError, match="current Git commit/tree"):
        build_final_test_signals(
            _authorization(),
            code_root=tmp_path / "code",
            final_root=tmp_path / "processed/final_test",
        )

    assert resolved is False


def test_signals_load_frozen_config_from_authorized_code_root() -> None:
    parameters = inspect.signature(build_final_test_signals).parameters

    assert "code_root" in parameters
    assert "config" not in parameters
    for forbidden in (
        "historical_daily",
        "readiness",
        "classifications",
        "historical_rank_ic",
        "historical_forward_returns",
        "previous_targets",
    ):
        assert forbidden not in parameters


def test_authoritative_history_failure_precedes_signal_math(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen = load_config(Path("configs/research_protocol.yaml"))
    frozen = replace(
        frozen,
        paths=replace(frozen.paths, processed=tmp_path / "processed"),
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.signals._load_frozen_config",
        lambda _code_root, _data_root: frozen,
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.signals._verify_data_authorization",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.signals._resolve_authorized_data",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.signals.resolve_final_test_signal_inputs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("authoritative Stage7 history was tampered")
        ),
    )

    def forbidden_math(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("signal math must wait for authoritative history")

    monkeypatch.setattr(
        "ashare_multifactor.final_test.signals.build_monthly_factor_panel",
        forbidden_math,
    )

    with pytest.raises(ValueError, match="Stage7 history was tampered"):
        build_final_test_signals(
            _authorization(),
            code_root=tmp_path / "code",
            final_root=tmp_path / "processed/final_test",
        )


def test_signals_bind_verified_data_claim_to_authorization_before_parquet_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "data-build-claim.json").write_text(
        '{"status":"published","attempt_id":"different-attempt"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.signals.resolve_final_test_data_panel",
        lambda _root: SimpleNamespace(
            root=tmp_path / "daily_panel",
            claim_status="published",
            requires_recovery=False,
        ),
    )

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Parquet validation must wait for matching authorization")

    monkeypatch.setattr(
        "ashare_multifactor.final_test.signals.validate_panel_source", forbidden
    )

    with pytest.raises(ValueError, match="differs from authorization"):
        _resolve_authorized_data(
            tmp_path,
            _authorization(),
            load_config(Path("configs/research_protocol.yaml")).test,
        )


def test_realization_dates_keep_unrealized_test_end_missing_without_2026() -> None:
    returns = pl.DataFrame(
        {
            "date": [date(2025, 11, 28), date(2025, 12, 31)],
            "forward_calendar_days_20": [28, None],
        },
        schema={"date": pl.Date, "forward_calendar_days_20": pl.Int64},
    )

    result = _realization_dates(returns, final_end=FINAL_END)

    assert result.rows() == [
        (date(2025, 11, 28), date(2025, 12, 26)),
        (date(2025, 12, 31), None),
    ]


def test_realization_dates_reject_any_label_that_requires_2026() -> None:
    returns = pl.DataFrame(
        {
            "date": [date(2025, 12, 20)],
            "forward_calendar_days_20": [20],
        },
        schema={"date": pl.Date, "forward_calendar_days_20": pl.Int64},
    )

    with pytest.raises(ValueError, match="beyond the sealed final-test end"):
        _realization_dates(returns, final_end=FINAL_END)


def test_rolling_targets_use_realized_ic_and_continue_2021_holdings() -> None:
    classifications = _classifications()
    dates = [date(2022, 1, 31), date(2022, 2, 28)]
    factors = classifications.filter(pl.col("classification") == "candidate").select(
        "factor_name", "family"
    )
    panel = pl.DataFrame(
        [
            {
                "date": signal_date,
                "symbol": f"00000{symbol}",
                "factor_name": factor_name,
                "family": family,
                "score_size_neutral": float(symbol + factor_index),
            }
            for signal_date in dates
            for symbol in range(1, 5)
            for factor_index, (factor_name, family) in enumerate(factors.iter_rows())
        ]
    )
    historical = _rank_ic(date(2021, 11, 30), value=0.1)
    final = pl.concat(
        (_rank_ic(dates[0], value=99.0), _rank_ic(dates[1], value=-99.0))
    )
    realization = pl.DataFrame(
        {
            "date": [date(2021, 11, 30), *dates],
            "realization_date": [date(2021, 12, 30), date(2022, 3, 1), None],
        },
        schema={"date": pl.Date, "realization_date": pl.Date},
    )
    log_size = pl.DataFrame(
        [
            {
                "date": signal_date,
                "symbol": f"00000{symbol}",
                "log_market_cap": float(symbol),
            }
            for signal_date in dates
            for symbol in range(1, 5)
        ]
    )
    previous = pl.DataFrame(
        {
            "date": [date(2021, 12, 31)] * 3,
            "candidate": [
                "rolling_ic_family_size_stratified_buffered",
                "rolling_ic_family_size_stratified_buffered",
                "family_equal_size_stratified_buffered",
            ],
            "symbol": ["000002", "000004", "000001"],
            "target_weight": [0.5, 0.5, 1.0],
        }
    )
    config = load_config(Path("configs/research_protocol.yaml"))
    combination = replace(
        config.factor_combination,
        ic_minimum_months=1,
    )
    portfolio = replace(
        config.portfolio_construction,
        portfolio_size=2,
        size_groups=2,
        per_group=1,
        buffer_rank=2,
    )

    scores, weights, targets = _build_rolling_targets(
        panel,
        classifications,
        pl.concat((historical, final)),
        realization,
        log_size,
        previous,
        combination=combination,
        portfolio=portfolio,
    )

    january_weights = weights.filter(pl.col("date") == dates[0])
    february_weights = weights.filter(pl.col("date") == dates[1])
    assert january_weights.get_column("history_end").unique().item() == date(2021, 11, 30)
    assert february_weights.get_column("history_end").unique().item() == date(2021, 11, 30)
    assert set(weights.get_column("factor_name")) == set(
        classifications.get_column("factor_name")
    )
    assert set(targets.filter(pl.col("date") == dates[0])["symbol"]) == {
        "000002",
        "000004",
    }
    assert scores.get_column("method").unique().to_list() == ["rolling_ic_family"]
    assert targets.get_column("candidate").unique().to_list() == [
        "rolling_ic_family_size_stratified_buffered"
    ]

    stale = previous.with_columns(pl.lit(date(2021, 1, 29)).alias("date"))
    with pytest.raises(ValueError, match="final 2021 rebalance"):
        _build_rolling_targets(
            panel,
            classifications,
            pl.concat((historical, final)),
            realization,
            log_size,
            stale,
            combination=combination,
            portfolio=portfolio,
        )


def _classifications() -> pl.DataFrame:
    pairs = [
        ("reversal_5", "reversal"),
        ("reversal_20", "reversal"),
        ("turnover_20", "liquidity"),
        ("amihud_20", "liquidity"),
        ("volatility_20", "low_volatility"),
        ("volatility_60", "low_volatility"),
    ]
    return pl.DataFrame(
        {
            "factor_name": [name for name, _ in pairs],
            "family": [family for _, family in pairs],
            "classification": ["candidate"] * len(pairs),
            "primary_score_variant": ["score_size_neutral"] * len(pairs),
        }
    )


def _rank_ic(signal_date: date, *, value: float) -> pl.DataFrame:
    classifications = _classifications()
    return classifications.select("factor_name", "family").with_columns(
        pl.lit(signal_date).cast(pl.Date).alias("date"),
        pl.lit("score_size_neutral").alias("score_variant"),
        pl.lit(20).alias("horizon"),
        (pl.lit(value) + pl.int_range(0, pl.len()) / 100).alias("rank_ic"),
    ).select(
        "date",
        "factor_name",
        "family",
        "score_variant",
        "horizon",
        "rank_ic",
    )
