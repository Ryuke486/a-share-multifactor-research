from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import shutil
from uuid import uuid4

import polars as pl

from ashare_multifactor.audit.publication import PublishedRelease, publish_release
from ashare_multifactor.combination.definitions import CANDIDATE_FACTORS
from ashare_multifactor.combination.panel import (
    build_composite_scores,
    build_rolling_composite_scores,
)
from ashare_multifactor.combination.rolling_ic import rolling_ic_weights
from ashare_multifactor.config import load_config
from ashare_multifactor.portfolio.diagnostics import (
    compare_portfolios,
    portfolio_diagnostics,
    quality_warnings,
)
from ashare_multifactor.portfolio.ranking import top_n_equal
from ashare_multifactor.portfolio.size_stratified import size_stratified_buffered
from ashare_multifactor.research.combination_evaluation import evaluate_combinations
from ashare_multifactor.research.combination_inputs import (
    StageFiveInputs,
    validate_stage_five_inputs,
)
from ashare_multifactor.research.combination_lineage import (
    build_combination_lineage,
)
from ashare_multifactor.research.combination_paths import combination_paths
from ashare_multifactor.research.combination_outputs import (
    build_output_file_manifest,
    stage_five_dataset_entries,
)
from ashare_multifactor.research.combination_report import (
    render_combination_report,
    write_combination_figure,
)
from ashare_multifactor.research.factor_outputs import write_json_atomic, write_parquet_atomic


UPSTREAM_FILES = (
    "factor_panel.parquet", "factor_classifications.parquet", "rank_ic.parquet",
    "forward_returns.parquet", "factor_correlations.parquet", "lineage.json",
)


def run_combination_pipeline(
    config_path: Path,
    *,
    upstream_root: Path | None = None,
) -> PublishedRelease:
    config = load_config(config_path)
    if config.factor_combination is None or config.portfolio_construction is None:
        raise ValueError("stage five settings are required")
    paths = combination_paths(config)
    upstream = upstream_root or config.paths.processed / "factor_research"
    inputs = validate_stage_five_inputs(upstream)
    _validate_upstream(upstream)
    staging_processed = paths.processed_root.parent / f".{paths.processed_root.name}-{uuid4().hex}.tmp"
    staging_artifacts = paths.artifact_root.parent / f".{paths.artifact_root.name}-{uuid4().hex}.tmp"
    staging_processed.mkdir(parents=True)
    staging_artifacts.mkdir(parents=True)
    try:
        dataset_entries = _run_all(
            config, upstream, staging_processed, staging_artifacts, inputs
        )
        lineage_path = staging_processed / "lineage.json"
        lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
        lineage_path.unlink()
        return publish_release(
            paths.processed_root,
            run_id=uuid4().hex,
            staged_datasets=staging_processed,
            staged_artifacts=staging_artifacts,
            lineage=lineage,
            manifest_metadata={"datasets": dataset_entries},
        )
    except BaseException:
        shutil.rmtree(staging_processed, ignore_errors=True)
        shutil.rmtree(staging_artifacts, ignore_errors=True)
        raise


def _run_all(
    config,
    upstream: Path,
    processed: Path,
    artifacts: Path,
    inputs: StageFiveInputs,
) -> list[dict[str, object]]:
    combination = config.factor_combination
    portfolio = config.portfolio_construction
    panel = pl.read_parquet(upstream / "factor_panel.parquet")
    classifications = pl.read_parquet(upstream / "factor_classifications.parquet")
    rank_ic = pl.read_parquet(upstream / "rank_ic.parquet")
    returns = pl.read_parquet(upstream / "forward_returns.parquet")
    static = build_composite_scores(
        panel, classifications,
        methods=("candidate_equal", "family_equal", "representative_equal"),
        minimum_families=combination.minimum_families,
        analysis_start=combination.analysis_start,
        analysis_end=combination.analysis_end,
    )
    dates = static.get_column("date").unique().sort().to_list()
    rolling = rolling_ic_weights(
        rank_ic, weight_dates=dates, factors=CANDIDATE_FACTORS,
        window_months=combination.ic_window_months,
        minimum_months=combination.ic_minimum_months,
        shrinkage=combination.ic_shrinkage,
        realization_dates=_realization_dates(returns),
    )
    dynamic = build_rolling_composite_scores(
        panel,
        classifications,
        rolling,
        minimum_families=combination.minimum_families,
    )
    scores = pl.concat((static, dynamic)).sort("date", "symbol", "method")
    _validate_keys(scores, ("date", "symbol", "method"))
    static_weights = _static_weights(dates)
    weights = pl.concat((static_weights, rolling.with_columns(
        pl.lit("rolling_ic_family").alias("method"),
        (pl.col("factor_weight") / 3).alias("weight"),
    ).select("date", "method", "factor_name", "family", "weight", "history_start", "history_end",
             "history_months", "fallback_equal")), how="diagonal_relaxed").sort("date", "method", "factor_name")
    _validate_weight_outputs(scores, weights, ("date", "method", "factor_name"))
    write_parquet_atomic(processed / "composite_scores.parquet", scores)
    write_parquet_atomic(processed / "composite_weights.parquet", weights)
    candidate_panel = panel.filter(
        pl.col("factor_name").is_in(
            {factor for factors in CANDIDATE_FACTORS.values() for factor in factors}
        )
    )
    evaluation = evaluate_combinations(scores, returns, candidate_panel)
    write_parquet_atomic(artifacts / "composite_ic.parquet", evaluation.ic)
    write_parquet_atomic(artifacts / "composite_quantiles.parquet", evaluation.quantiles)
    write_parquet_atomic(artifacts / "composite_subperiods.parquet", evaluation.subperiods)
    write_parquet_atomic(artifacts / "composite_correlations.parquet", evaluation.correlations)
    write_parquet_atomic(
        artifacts / "composite_correlation_monthly.parquet",
        evaluation.correlation_monthly,
    )
    targets, diagnostics, issues = _build_targets(panel, scores, combination.primary_method, portfolio)
    _validate_keys(targets, ("date", "portfolio_name", "symbol"))
    _validate_target_weights(targets)
    write_parquet_atomic(processed / "target_weights.parquet", targets)
    write_parquet_atomic(processed / "target_weight_diagnostics.parquet", diagnostics)
    diagnostics.write_csv(artifacts / "portfolio_summary.csv")
    diagnostics.select("date", "portfolio_name", "one_way_turnover").write_csv(
        artifacts / "portfolio_turnover.csv"
    )
    summary = _augment_summary(evaluation.summary, scores, panel)
    summary.write_csv(artifacts / "composite_summary.csv")
    exposures, capacity_issues = _portfolio_exposures(targets, panel, upstream)
    issues = pl.concat((issues, capacity_issues), how="diagonal_relaxed").sort(
        "date", "portfolio_name", "rule"
    )
    issues.write_csv(artifacts / "quality_issues.csv")
    exposures.write_csv(artifacts / "portfolio_exposures.csv")
    write_combination_figure(evaluation.ic, artifacts / "figures")
    lineage = build_combination_lineage(upstream, config, inputs)
    write_json_atomic(processed / "lineage.json", lineage)
    report = render_combination_report(
        summary, diagnostics, issues, combination.primary_method
    )
    (artifacts / "report.md").write_text(report, encoding="utf-8")
    manifest = build_output_file_manifest(processed, artifacts)
    write_json_atomic(artifacts / "manifest.json", manifest)
    return stage_five_dataset_entries(
        processed,
        {
            "composite_scores": scores,
            "composite_weights": weights,
            "target_weights": targets,
            "target_weight_diagnostics": diagnostics,
        },
        analysis_start=combination.analysis_start,
        analysis_end=combination.analysis_end,
    )


def _static_weights(dates: list[date]) -> pl.DataFrame:
    rows = []
    all_factors = [(family, factor) for family, factors in CANDIDATE_FACTORS.items() for factor in factors]
    representatives = {"reversal_20", "turnover_20", "volatility_60"}
    for signal_date in dates:
        for family, factor in all_factors:
            rows.append({"date": signal_date, "method": "candidate_equal", "factor_name": factor,
                         "family": family, "weight": 1 / 6})
            rows.append({"date": signal_date, "method": "family_equal", "factor_name": factor,
                         "family": family, "weight": 1 / 6})
            if factor in representatives:
                rows.append({"date": signal_date, "method": "representative_equal",
                             "factor_name": factor, "family": family, "weight": 1 / 3})
    return pl.DataFrame(rows)


def _build_targets(panel, scores, method: str, settings):
    log_size = panel.filter(pl.col("factor_name") == "log_market_cap").select(
        "date", "symbol", pl.col("raw_value").alias("log_market_cap")
    )
    primary = scores.filter(pl.col("method") == method).join(
        log_size, on=["date", "symbol"], how="left", validate="1:1"
    )
    target_parts, diagnostic_rows, issue_rows = [], [], []
    previous_by_name = {"top100_equal": pl.DataFrame(), "size_stratified_buffered": pl.DataFrame()}
    for signal_date in primary.get_column("date").unique().sort():
        cross = primary.filter(pl.col("date") == signal_date)
        portfolios = {
            "top100_equal": top_n_equal(cross, portfolio_size=settings.portfolio_size,
                                         portfolio_name="top100_equal"),
            "size_stratified_buffered": size_stratified_buffered(
                cross, previous_targets=previous_by_name["size_stratified_buffered"],
                size_groups=settings.size_groups, per_group=settings.per_group,
                buffer_rank=settings.buffer_rank, portfolio_name="size_stratified_buffered",
            ),
        }
        for name, target in portfolios.items():
            target = target.with_columns(pl.lit(signal_date).alias("date"))
            diag = portfolio_diagnostics(target, previous_by_name[name])
            diagnostic_rows.append({"date": signal_date, "portfolio_name": name, **diag})
            universe_count = panel.filter(pl.col("date") == signal_date).get_column(
                "symbol"
            ).n_unique()
            coverage = cross.height / max(universe_count, 1)
            for warning in quality_warnings(
                portfolio_name=name,
                holdings=target.height,
                expected_holdings=settings.portfolio_size,
                coverage=coverage,
                minimum_coverage=settings.minimum_coverage,
                turnover=float(diag["one_way_turnover"]),
                turnover_warning=settings.turnover_warning,
            ):
                issue_rows.append({"date": signal_date, **warning})
            target_parts.append(target.select("date", "portfolio_name", "symbol", "target_weight",
                                              *(["size_group"] if "size_group" in target.columns else [])))
            previous_by_name[name] = target.select("symbol", "target_weight")
        baseline_diag = diagnostic_rows[-2]
        primary_diag = diagnostic_rows[-1]
        comparison = compare_portfolios(
            portfolios["top100_equal"],
            portfolios["size_stratified_buffered"],
            baseline_turnover=float(baseline_diag["one_way_turnover"]),
            primary_turnover=float(primary_diag["one_way_turnover"]),
        )
        baseline_diag.update({"holding_overlap": comparison["holding_overlap"],
                              "turnover_difference": 0.0})
        primary_diag.update(comparison)
    issues = pl.DataFrame(issue_rows, schema={"date": pl.Date, "severity": pl.String,
        "portfolio_name": pl.String, "rule": pl.String, "details": pl.String})
    return pl.concat(target_parts, how="diagonal_relaxed").sort("date", "portfolio_name", "symbol"), \
        pl.DataFrame(diagnostic_rows).sort("date", "portfolio_name"), issues


def _portfolio_exposures(
    targets: pl.DataFrame, panel: pl.DataFrame, upstream: Path
) -> tuple[pl.DataFrame, pl.DataFrame]:
    candidate_names = {factor for factors in CANDIDATE_FACTORS.values() for factor in factors}
    factor_values = panel.filter(
        pl.col("factor_name").is_in(candidate_names | {"log_market_cap"})
    ).select(
        "date", "symbol", "factor_name", "family",
        pl.when(pl.col("factor_name") == "log_market_cap")
        .then(pl.col("raw_value"))
        .otherwise(pl.col("score_size_neutral"))
        .alias("value"),
    )
    joined = targets.join(factor_values, on=["date", "symbol"], how="left", validate="m:m")
    factor_rows = joined.group_by("date", "portfolio_name", "factor_name", "family").agg(
        (pl.col("target_weight") * pl.col("value")).sum().alias("exposure")
    ).with_columns(pl.lit("factor").alias("exposure_type"))
    family_rows = joined.filter(pl.col("factor_name").is_in(candidate_names)).group_by(
        "date", "portfolio_name", "family"
    ).agg(
        (pl.col("target_weight") * pl.col("value")).sum()
        .truediv(pl.col("factor_name").n_unique())
        .alias("exposure")
    ).with_columns(
        pl.col("family").alias("factor_name"), pl.lit("family").alias("exposure_type")
    )
    liquidity = _signal_liquidity(upstream, targets.get_column("date").unique().sort().to_list())
    capacity_input = targets.join(liquidity, on=["date", "symbol"], how="left", validate="m:1")
    incomplete = capacity_input.group_by("date", "portfolio_name").agg(
        pl.col("amount_20_mean").is_null().sum().alias("missing")
    ).filter(pl.col("missing") > 0)
    capacity = capacity_input.group_by(
        "date", "portfolio_name"
    ).agg(
        pl.when(pl.col("amount_20_mean").is_not_null().all())
        .then((pl.col("target_weight") / pl.col("amount_20_mean")).max())
        .otherwise(None)
        .alias("exposure"),
    ).with_columns(
        pl.lit("amount_20_mean").alias("factor_name"),
        pl.lit("capacity").alias("family"),
        pl.lit("capacity").alias("exposure_type"),
    )
    exposures = pl.concat((factor_rows, family_rows, capacity), how="diagonal_relaxed").select(
        "date", "portfolio_name", "exposure_type", "family", "factor_name", "exposure"
    ).sort("date", "portfolio_name", "exposure_type", "factor_name")
    issues = incomplete.select(
        "date", pl.lit("warning").alias("severity"), "portfolio_name",
        pl.lit("insufficient_amount_history").alias("rule"),
        pl.col("missing").cast(pl.String).alias("details"),
    )
    return exposures, issues


def _signal_liquidity(upstream: Path, signal_dates: list[date]) -> pl.DataFrame:
    parts = sorted((upstream / "daily_panel").glob("year=*/part-*.parquet"))
    daily = pl.scan_parquet(parts).select("date", "symbol", "amount").sort("symbol", "date")
    return daily.with_columns(
        pl.col("amount").rolling_mean(window_size=20, min_samples=20).over("symbol")
        .alias("amount_20_mean")
    ).filter(pl.col("date").is_in(signal_dates)).select(
        "date", "symbol", "amount_20_mean"
    ).collect()


def _augment_summary(
    summary: pl.DataFrame, scores: pl.DataFrame, factor_panel: pl.DataFrame
) -> pl.DataFrame:
    diagnostics = []
    universe = factor_panel.group_by("date").agg(
        pl.col("symbol").n_unique().alias("universe_count")
    )
    for method in sorted(set(scores.get_column("method"))):
        frame = scores.filter(pl.col("method") == method)
        dates = frame.get_column("date").unique().sort().to_list()
        turnovers, correlations = [], []
        previous_top: set[str] | None = None
        previous = None
        for signal_date in dates:
            cross = frame.filter(pl.col("date") == signal_date).sort("symbol")
            top_count = max(1, int(cross.height * 0.1))
            top = set(cross.sort("score", "symbol", descending=[True, False]).head(top_count)["symbol"])
            if previous_top is not None:
                turnovers.append(1 - len(top & previous_top) / max(len(top), 1))
            if previous is not None:
                paired = previous.join(cross.select("symbol", pl.col("score").alias("current")),
                                       on="symbol", how="inner")
                if paired.height >= 3:
                    correlations.append(float(pl.select(
                        pl.corr(paired["score"], paired["current"], method="spearman")
                    ).item()))
            previous_top, previous = top, cross.select("symbol", "score")
        monthly_coverage = frame.group_by("date").len().join(
            universe, on="date", how="left", validate="1:1"
        ).select((pl.col("len") / pl.col("universe_count")).alias("coverage"))
        coverage_rate = float(monthly_coverage.get_column("coverage").mean())
        diagnostics.append({"method": method, "coverage_rate": coverage_rate,
                            "ranking_stability": sum(correlations) / len(correlations),
                            "top10_turnover": sum(turnovers) / len(turnovers)})
    result = summary.join(pl.DataFrame(diagnostics), on="method", how="left", validate="m:1")
    baseline = result.filter((pl.col("method") == "candidate_equal") & (pl.col("horizon") == 20))
    baseline_ic = baseline.get_column("mean_ic").item()
    return result.with_columns((pl.col("mean_ic") - baseline_ic).alias("delta_mean_ic_vs_candidate_equal"))


def _validate_upstream(root: Path) -> None:
    missing = [name for name in UPSTREAM_FILES if not (root / name).is_file()]
    if missing:
        raise FileNotFoundError("missing stage four inputs: " + ", ".join(missing))
    validate_stage_five_inputs(root)
    panel = pl.scan_parquet(root / "factor_panel.parquet").select(
        pl.col("date").min().alias("min"), pl.col("date").max().alias("max")
    ).collect()
    if panel["min"].item() < date(2005, 1, 1) or panel["max"].item() > date(2016, 12, 31):
        raise ValueError("stage four input leaves the research period")


def _realization_dates(returns: pl.DataFrame) -> pl.DataFrame:
    return returns.group_by("date").agg(
        (pl.col("date").first() + pl.duration(days=pl.col("forward_calendar_days_20").max()))
        .alias("realization_date")
    )


def _validate_keys(frame: pl.DataFrame, keys: tuple[str, ...]) -> None:
    if frame.select(*keys).is_duplicated().any():
        raise ValueError("duplicate output keys: " + ", ".join(keys))


def _validate_weight_outputs(scores: pl.DataFrame, weights: pl.DataFrame, keys: tuple[str, ...]) -> None:
    _validate_keys(scores, ("date", "symbol", "method"))
    _validate_keys(weights, keys)
    if scores.filter(~pl.col("score").is_finite()).height:
        raise ValueError("composite scores must be finite")
    if weights.filter(~pl.col("weight").is_finite() | (pl.col("weight") < 0)).height:
        raise ValueError("composite weights must be finite and non-negative")
    sums = weights.group_by("date", "method").agg(pl.col("weight").sum().alias("total"))
    if sums.filter((pl.col("total") - 1).abs() > 1e-12).height:
        raise ValueError("composite weights must sum to one")


def _validate_target_weights(targets: pl.DataFrame) -> None:
    if targets.filter(~pl.col("target_weight").is_finite() | (pl.col("target_weight") < 0)).height:
        raise ValueError("target weights must be finite and non-negative")
    if targets.filter(~pl.col("date").is_between(date(2005, 1, 1), date(2016, 12, 31))).height:
        raise ValueError("target weights leave the research period")
    sums = targets.group_by("date", "portfolio_name").agg(
        pl.col("target_weight").sum().alias("total")
    )
    if sums.filter((pl.col("total") - 1).abs() > 1e-12).height:
        raise ValueError("target weights must sum to one")
