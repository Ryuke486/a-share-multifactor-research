from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
import json
from pathlib import Path

import polars as pl

from ashare_multifactor.combination.definitions import CANDIDATE_FACTORS
from ashare_multifactor.combination.panel import build_rolling_composite_scores
from ashare_multifactor.combination.rolling_ic import rolling_ic_weights
from ashare_multifactor.config import (
    FactorCombinationSettings,
    Period,
    PortfolioConstructionSettings,
    load_config,
)
from ashare_multifactor.data.manifest import validate_panel_source
from ashare_multifactor.factors.definitions import FACTOR_DEFINITIONS
from ashare_multifactor.factors.panel import build_monthly_factor_panel
from ashare_multifactor.final_test.data_publication import (
    read_data_claim,
    resolve_final_test_data_panel,
)
from ashare_multifactor.final_test.data_extension import (
    _verify_authorization as _verify_data_authorization,
)
from ashare_multifactor.final_test.gate import (
    FINAL_TEST_END,
    FINAL_TEST_START,
    FinalTestAuthorization,
)
from ashare_multifactor.final_test.panel_binding import FrozenPanelSnapshot
from ashare_multifactor.final_test.signal_inputs import (
    resolve_final_test_signal_inputs,
)
from ashare_multifactor.portfolio.size_stratified import size_stratified_buffered
from ashare_multifactor.research.factor_evaluation import evaluate_factors
from ashare_multifactor.research.factor_preprocessing import preprocess_factor_panel
from ashare_multifactor.research.labels import add_forward_returns


MAIN_CANDIDATE = "rolling_ic_family_size_stratified_buffered"


@dataclass(frozen=True)
class FinalTestSignals:
    factor_panel: pl.DataFrame
    composite_scores: pl.DataFrame
    composite_weights: pl.DataFrame
    target_weights: pl.DataFrame


def build_final_test_signals(
    authorization: FinalTestAuthorization,
    *,
    code_root: Path,
    final_root: Path,
    panel_snapshot: FrozenPanelSnapshot | None = None,
) -> FinalTestSignals:
    """Extend the single sealed Stage-8 candidate through the final-test period."""
    if not isinstance(authorization, FinalTestAuthorization):
        raise TypeError("authorization must be a FinalTestAuthorization")
    if authorization.test_period != (FINAL_TEST_START, FINAL_TEST_END):
        raise ValueError("authorization period differs from the sealed final-test period")
    code_root = code_root.resolve()
    final_root = final_root.resolve()
    config = _load_frozen_config(code_root, final_root.parent.parent)
    if final_root != config.paths.processed / "final_test":
        raise ValueError("final-test data root is not canonical")
    _verify_data_authorization(config, authorization, code_root)
    if (config.test.start, config.test.end) != (FINAL_TEST_START, FINAL_TEST_END):
        raise ValueError("configured test period differs from the sealed final-test period")
    factor = config.factor_research
    combination = config.factor_combination
    portfolio = config.portfolio_construction
    if factor is None or combination is None or portfolio is None:
        raise ValueError("final-test signals require frozen factor and portfolio settings")

    source = _resolve_authorized_data(
        final_root,
        authorization,
        config.test,
        panel_snapshot=panel_snapshot,
    )
    inputs = resolve_final_test_signal_inputs(
        config,
        authorization,
        data_root=final_root.parent.parent,
    )
    final_daily = pl.scan_parquet(source.files).collect()
    _assert_date_bounds(final_daily, config.test, "final-test daily panel")
    daily = pl.concat((inputs.historical_daily, final_daily), how="vertical_relaxed").sort(
        "date", "symbol"
    )
    factor_settings = replace(
        factor,
        analysis_start=FINAL_TEST_START,
        analysis_end=FINAL_TEST_END,
    )
    raw = build_monthly_factor_panel(daily, FACTOR_DEFINITIONS, factor_settings)
    features = preprocess_factor_panel(raw, inputs.readiness, factor_settings)
    labelled = add_forward_returns(
        daily.select("date", "symbol", "close_adj", "volume", "amount"),
        factor_settings.forward_horizons,
    )
    label_columns = [
        column
        for column in labelled.columns
        if column.startswith("forward_return_")
        or column.startswith("forward_calendar_days_")
        or column.startswith("forward_skipped_observations_")
    ]
    forward_returns = (
        features.select("date", "symbol")
        .unique()
        .join(
            labelled.select("date", "symbol", *label_columns),
            on=["date", "symbol"],
            how="left",
            validate="1:1",
        )
        .sort("date", "symbol")
    )
    return_columns = [f"forward_return_{horizon}" for horizon in factor_settings.forward_horizons]
    factor_panel = features.join(
        forward_returns.select("date", "symbol", *return_columns),
        on=["date", "symbol"],
        how="left",
        validate="m:1",
    ).sort("date", "symbol", "factor_name")
    _assert_date_bounds(factor_panel, config.test, "final-test factor panel")

    final_rank_ic = evaluate_factors(
        factor_panel,
        FACTOR_DEFINITIONS,
        factor_settings,
    ).rank_ic
    all_rank_ic = pl.concat((inputs.historical_rank_ic, final_rank_ic), how="diagonal_relaxed")
    realization_dates = _realization_dates(
        pl.concat(
            (inputs.historical_forward_returns, forward_returns),
            how="diagonal_relaxed",
        ),
        final_end=FINAL_TEST_END,
    )
    log_size = features.filter(pl.col("factor_name") == "log_market_cap").select(
        "date",
        "symbol",
        pl.col("raw_value").alias("log_market_cap"),
    )
    scores, weights, targets = _build_rolling_targets(
        factor_panel,
        inputs.classifications,
        all_rank_ic,
        realization_dates,
        log_size,
        inputs.previous_targets,
        combination=combination,
        portfolio=portfolio,
    )
    return FinalTestSignals(factor_panel, scores, weights, targets)


def _load_frozen_config(code_root: Path, data_root: Path):
    config = load_config(code_root / "configs/research_protocol.yaml")
    paths = replace(
        config.paths,
        **{
            name: path if path.is_absolute() else data_root / path
            for name, path in vars(config.paths).items()
        },
    )
    return replace(config, paths=paths)


def _resolve_authorized_data(
    final_root: Path,
    authorization: FinalTestAuthorization,
    period: Period,
    *,
    panel_snapshot: FrozenPanelSnapshot | None = None,
):
    if panel_snapshot is not None:
        panel_snapshot.assert_bound()
        if not panel_snapshot.anonymous_frozen:
            raise ValueError("attempt-bound final-test data is not anonymously frozen")
        if panel_snapshot.source is None:
            raise ValueError("attempt-bound final-test data manifest is missing")
        return panel_snapshot.source
    resolution = resolve_final_test_data_panel(final_root)
    if resolution.requires_recovery or resolution.claim_status != "published":
        raise ValueError("final-test data publication requires recovery before signals")
    claim = read_data_claim(final_root)
    expected = {
        "attempt_id": authorization.attempt_id,
        "approval_id": authorization.approval_id,
        "git_commit": authorization.git_commit,
        "git_tree": authorization.git_tree,
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "robustness_release": authorization.robustness_release,
    }
    if not isinstance(claim, dict) or any(
        claim.get(key) != value for key, value in expected.items()
    ):
        reuse_path = final_root / "data-reuse" / f"{authorization.attempt_id}.json"
        try:
            reuse = json.loads(reuse_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as error:
            raise ValueError("final-test data claim differs from authorization") from error
        reuse_expected = {
            **expected,
            "data_manifest_sha256": resolution.data_manifest_sha256,
            "status": "reused_verified_immutable_panel",
        }
        if reuse_path.is_symlink() or any(
            reuse.get(key) != value for key, value in reuse_expected.items()
        ):
            raise ValueError("final-test data reuse differs from authorization")
    return validate_panel_source(resolution.root, period)


def _assert_date_bounds(frame: pl.DataFrame, period: Period, label: str) -> None:
    if frame.is_empty():
        raise ValueError(f"{label} is empty")
    minimum = frame.get_column("date").min()
    maximum = frame.get_column("date").max()
    if minimum < period.start or maximum > period.end:
        raise ValueError(f"{label} dates differ from the sealed final-test period")


def _realization_dates(
    returns: pl.DataFrame,
    *,
    final_end: date,
) -> pl.DataFrame:
    result = (
        returns.group_by("date")
        .agg(
            (
                pl.col("date").first() + pl.duration(days=pl.col("forward_calendar_days_20").max())
            ).alias("realization_date")
        )
        .sort("date")
    )
    if result.filter(pl.col("realization_date") > final_end).height:
        raise ValueError("forward label realization extends beyond the sealed final-test end")
    return result


def _build_rolling_targets(
    factor_panel: pl.DataFrame,
    classifications: pl.DataFrame,
    rank_ic: pl.DataFrame,
    realization_dates: pl.DataFrame,
    log_size: pl.DataFrame,
    previous_targets: pl.DataFrame,
    *,
    combination: FactorCombinationSettings,
    portfolio: PortfolioConstructionSettings,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    weight_dates = factor_panel.get_column("date").unique().sort().to_list()
    realized_ic = (
        rank_ic.join(
            realization_dates,
            on="date",
            how="left",
            validate="m:1",
        )
        .filter(pl.col("realization_date").is_not_null())
        .drop("realization_date")
    )
    realized_dates = realization_dates.filter(pl.col("realization_date").is_not_null())
    weights = rolling_ic_weights(
        realized_ic,
        weight_dates=weight_dates,
        factors=CANDIDATE_FACTORS,
        window_months=combination.ic_window_months,
        minimum_months=combination.ic_minimum_months,
        shrinkage=combination.ic_shrinkage,
        realization_dates=realized_dates,
    )
    scores = build_rolling_composite_scores(
        factor_panel,
        classifications,
        weights,
        minimum_families=combination.minimum_families,
    ).sort("date", "symbol")
    selected = scores.join(
        log_size,
        on=["date", "symbol"],
        how="left",
        validate="1:1",
    )
    frozen_previous = (
        previous_targets.filter(pl.col("candidate") == MAIN_CANDIDATE)
        if "candidate" in previous_targets.columns
        else previous_targets
    )
    if frozen_previous.is_empty():
        raise ValueError("previous targets lack the frozen main candidate")
    latest = frozen_previous.get_column("date").max()
    if latest != date(2021, 12, 31):
        raise ValueError("previous targets must preserve the final 2021 rebalance")
    prior = frozen_previous.filter(pl.col("date") == latest).select("symbol", "target_weight")
    parts: list[pl.DataFrame] = []
    for signal_date in weight_dates:
        target = size_stratified_buffered(
            selected.filter(pl.col("date") == signal_date),
            previous_targets=prior,
            size_groups=portfolio.size_groups,
            per_group=portfolio.per_group,
            buffer_rank=portfolio.buffer_rank,
            portfolio_name="size_stratified_buffered",
        ).with_columns(
            pl.lit(signal_date).cast(pl.Date).alias("date"),
            pl.lit("rolling_ic_family").alias("method"),
            pl.lit(MAIN_CANDIDATE).alias("candidate"),
        )
        parts.append(
            target.select(
                "date",
                "method",
                "portfolio_name",
                "candidate",
                "symbol",
                "target_weight",
            )
        )
        prior = target.select("symbol", "target_weight")
    targets = pl.concat(parts).sort("date", "symbol")
    sums = targets.group_by("date").agg(pl.col("target_weight").sum().alias("weight"))
    if sums.filter((pl.col("weight") - 1.0).abs() > 1e-12).height:
        raise ValueError("final-test target weights must sum to one")
    composite_weights = (
        weights.with_columns(
            pl.lit("rolling_ic_family").alias("method"),
            (pl.col("factor_weight") / len(CANDIDATE_FACTORS)).alias("weight"),
        )
        .select(
            "date",
            "method",
            "factor_name",
            "family",
            "weight",
            "history_start",
            "history_end",
            "history_months",
            "fallback_equal",
        )
        .sort("date", "factor_name")
    )
    return scores, composite_weights, targets
