"""Benchmark and long/short-leg supplements to the v1.0 report.

The supplements are descriptive: they read frozen Stage 4-7 outputs, never change a
target, a selection or a release, and stay inside 2005-2021. The only new input is
the 2017-2021 daily panel, rebuilt through the stage-two builder into an isolated
root and checked against the prices stored in the Stage 7 release.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

import polars as pl

from ashare_multifactor.audit.publication import PublishedRelease, resolve_current
from ashare_multifactor.config import ResearchConfig, load_config
from ashare_multifactor.data.build import build_parquet_dataset
from ashare_multifactor.supplements.benchmark import (
    relative_performance,
    universe_benchmark_index,
)
from ashare_multifactor.supplements.figure import plot_benchmark_comparison
from ashare_multifactor.supplements.figure_data import (
    correlation_matrix,
    cost_components,
    drawdowns,
    quintile_excess,
    rolling_mean_ic,
)
from ashare_multifactor.supplements.ic_pipeline import build_ic_supplements
from ashare_multifactor.supplements.legs import quantile_leg_decomposition
from ashare_multifactor.supplements.report_figures import (
    plot_correlation_heatmap,
    plot_cost_components,
    plot_drawdowns,
    plot_quintile_panels,
    plot_rolling_ic,
)
from ashare_multifactor.validation.protocol import assert_validation_read_allowed


ANALYSIS_START = date(2005, 1, 1)
RESEARCH_END = date(2016, 12, 31)
VALIDATION_START = date(2017, 1, 1)
ANALYSIS_END = date(2021, 12, 31)
CANDIDATES = (
    "family_equal_size_stratified_buffered",
    "family_equal_top100_equal",
    "rolling_ic_family_size_stratified_buffered",
)
COMPOSITE_METHODS = ("family_equal", "rolling_ic_family")
BUILD_ROOT = Path("processed/v1_supplements/build")
OUTPUT_ROOT = Path("artifacts/v1_supplements")
FIGURE_NAME = "benchmark_comparison.png"
HEATMAP_ORDER = (
    "ep_ttm", "bp", "sp_ttm",
    "momentum_60", "momentum_120", "momentum_12_1",
    "reversal_5", "reversal_20",
    "turnover_20", "amihud_20",
    "volatility_20", "volatility_60", "downside_volatility_60",
    "log_market_cap",
)
QUINTILE_PANELS = (
    "family_equal", "reversal_5", "reversal_20", "turnover_20",
    "amihud_20", "volatility_20", "volatility_60",
)
_TOLERANCE = 1e-12


def run_supplements(config_path: Path, *, root: Path = Path(".")) -> Path:
    """Build every supplement table, the figure and a manifest binding all inputs."""
    root = root.resolve()
    config = load_config(root / config_path)
    output = root / OUTPUT_ROOT
    inputs: dict[str, str] = {}

    stage5 = _release(root / "processed/factor_combination", inputs, "stage5")
    stage6 = _release(root / "processed/formal_backtest", inputs, "stage6")
    stage7 = _release(root / "processed/validation_evaluation", inputs, "stage7")

    validation_panel = _rebuild_validation_panel(config, root)
    research_panel = root / "processed/factor_research/daily_panel"
    _verify_research_panel(research_panel)
    panel_files = _panel_files(research_panel, range(2005, 2017)) + _panel_files(
        validation_panel, range(2017, 2022)
    )
    for path in panel_files:
        inputs[str(path.relative_to(root))] = _sha256(path)
    prices = _read_prices(panel_files)
    _verify_against_stage7_prices(prices, stage7)

    members = _benchmark_members(root, stage7, prices, inputs)
    navs = {candidate: _candidate_nav(stage7, candidate, inputs) for candidate in CANDIDATES}
    trading_dates = navs[CANDIDATES[0]].get_column("date").to_list()
    panel_dates = set(prices.get_column("date").unique().to_list())
    if set(trading_dates) != panel_dates:
        raise ValueError("strategy NAV dates differ from the daily-panel trading calendar")
    index = universe_benchmark_index(prices, members, trading_dates=trading_dates)

    selection = json.loads((stage7.artifacts / "research/selection_decision.json").read_text())
    selected = str(selection["selected_candidate"])
    relative = _relative_table(navs, index, stage6, stage7, inputs)
    calendar = _calendar_years(navs[selected], index)
    quantiles, variants = _quantile_frames(root, stage5, stage7, inputs)
    legs = _leg_table(quantiles, variants)
    for path in (
        root / "processed/factor_research/factor_panel.parquet",
        root / "artifacts/factor_research/rank_ic.csv",
        root / "artifacts/factor_research/factor_summary.csv",
    ):
        _record(path, inputs, root)
    executable_ic, newey_west_lags = build_ic_supplements(
        root,
        stage5,
        stage7,
        panel_files,
        fdr_threshold=config.factor_research.fdr_q_threshold,
    )

    if output.exists():
        shutil.rmtree(output)
    (output / "figures").mkdir(parents=True)
    levels = _growth_levels(navs[selected], index)
    levels.write_parquet(output / "benchmark_daily.parquet")
    relative.write_csv(output / "relative_performance.csv")
    calendar.write_csv(output / "calendar_year_returns.csv")
    legs.write_csv(output / "leg_decomposition.csv")
    executable_ic.write_csv(output / "executable_ic.csv")
    newey_west_lags.write_csv(output / "newey_west_lags.csv")
    plot_benchmark_comparison(levels, output / "figures" / FIGURE_NAME, split=VALIDATION_START)
    _write_report_figures(
        root, output, stage5, stage6, stage7, levels, _quintile_excess_table(quantiles), inputs
    )
    _write_manifest(output, root, inputs, selected)
    return output


def _release(path: Path, inputs: dict[str, str], label: str) -> PublishedRelease:
    release = resolve_current(path)
    inputs[f"{label}:{release.run_id}/manifest.json"] = release.manifest_sha256
    return release


def _record(path: Path, inputs: dict[str, str], root: Path | None = None) -> Path:
    key = str(path.relative_to(root)) if root is not None else str(path)
    inputs[key] = _sha256(path)
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _panel_files(panel: Path, years: range) -> list[Path]:
    files = [panel / f"year={year}/part-000.parquet" for year in years]
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        raise FileNotFoundError("daily panel is incomplete: " + ", ".join(missing))
    return files


def _verify_research_panel(panel: Path) -> None:
    manifest = json.loads((panel / "manifest.json").read_text(encoding="utf-8"))
    for partition in manifest["partitions"]:
        if 2005 <= int(partition["year"]) <= 2016:
            path = panel / partition["relative_path"]
            if _sha256(path) != partition["sha256"]:
                raise ValueError(f"research daily panel partition changed: {path}")


def _rebuild_validation_panel(config: ResearchConfig, root: Path) -> Path:
    """Rebuild 2017-2021 through the stage-two builder in an isolated processed root."""
    assert_validation_read_allowed(VALIDATION_START, ANALYSIS_END)
    isolated = replace(config, paths=replace(config.paths, processed=root / BUILD_ROOT))
    target = root / BUILD_ROOT / "validation_evaluation/daily_panel"
    if not (target / "manifest.json").is_file():
        build_parquet_dataset(
            isolated,
            VALIDATION_START,
            ANALYSIS_END,
            output_root=target,
        )
    return target


def _read_prices(files: list[Path]) -> pl.DataFrame:
    frame = (
        pl.scan_parquet(files)
        .select("date", "symbol", "close_adj", "total_market_cap")
        .filter(pl.col("date").is_between(ANALYSIS_START, ANALYSIS_END))
        .collect()
    )
    if frame.get_column("date").max() > ANALYSIS_END:
        raise ValueError("daily panel crosses the sealed final-test boundary")
    return frame


def _verify_against_stage7_prices(prices: pl.DataFrame, stage7: PublishedRelease) -> None:
    """Every price the Stage 7 executor used must equal the rebuilt panel exactly."""
    executed = (
        pl.scan_parquet(stage7.datasets / "inputs/execution_panel.parquet")
        .filter(pl.col("date") >= VALIDATION_START)
        .select("date", "symbol", "close_adj")
        .collect()
    )
    joined = executed.join(
        prices.select("date", "symbol", pl.col("close_adj").alias("rebuilt")),
        on=["date", "symbol"],
        how="left",
    )
    missing = joined.filter(pl.col("rebuilt").is_null() & pl.col("close_adj").is_not_null())
    differing = joined.filter(pl.col("rebuilt") != pl.col("close_adj"))
    if missing.height or differing.height:
        raise ValueError(
            "rebuilt validation panel differs from Stage 7 execution prices: "
            f"{missing.height} missing, {differing.height} differing"
        )


def _benchmark_members(
    root: Path,
    stage7: PublishedRelease,
    prices: pl.DataFrame,
    inputs: dict[str, str],
) -> pl.DataFrame:
    """Signal-date universe membership exactly as Stage 4 and Stage 7 labelled it."""
    research = _record(root / "processed/factor_research/forward_returns.parquet", inputs, root)
    validation = stage7.datasets / "inputs/forward_returns.parquet"
    keys = pl.concat(
        [
            pl.read_parquet(research, columns=["date", "symbol"]).filter(
                pl.col("date").is_between(ANALYSIS_START, RESEARCH_END)
            ),
            pl.read_parquet(validation, columns=["date", "symbol"]).filter(
                pl.col("date").is_between(VALIDATION_START, ANALYSIS_END)
            ),
        ]
    )
    members = keys.join(
        prices.select("date", "symbol", pl.col("total_market_cap").alias("market_cap")),
        on=["date", "symbol"],
        how="left",
        validate="1:1",
    )
    invalid = members.filter(~(pl.col("market_cap").is_finite() & (pl.col("market_cap") > 0)))
    if invalid.height:
        raise ValueError(f"{invalid.height} benchmark members lack a positive market cap")
    return members


def _candidate_nav(
    stage7: PublishedRelease, candidate: str, inputs: dict[str, str]
) -> pl.DataFrame:
    path = stage7.datasets / f"backtests/{candidate}/nav.parquet"
    inputs[f"stage7:{candidate}/nav.parquet"] = _sha256(path)
    nav = pl.read_parquet(path).select("date", "nav", "zero_cost_nav").sort("date")
    if nav.get_column("date").max() > ANALYSIS_END:
        raise ValueError("candidate NAV crosses the sealed final-test boundary")
    return nav


def _relative_table(
    navs: dict[str, pl.DataFrame],
    index: pl.DataFrame,
    stage6: PublishedRelease,
    stage7: PublishedRelease,
    inputs: dict[str, str],
) -> pl.DataFrame:
    summary_path = stage6.artifacts / "summary.json"
    inputs[f"stage6:{stage6.run_id}/artifacts/summary.json"] = _sha256(summary_path)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    initial = float(summary["start_nav"])
    published = pl.read_parquet(stage7.artifacts / "research/validation_metrics.parquet")
    research_paths = {candidate: _slice(nav, ANALYSIS_START, RESEARCH_END) for candidate, nav in navs.items()}
    reference = research_paths[CANDIDATES[0]]
    for candidate, path in research_paths.items():
        if not path.equals(reference):
            raise ValueError(f"{candidate} does not share the frozen research-period path")

    rows: list[dict[str, Any]] = []
    research_index = _slice(index, ANALYSIS_START, RESEARCH_END)
    research_years = (reference["date"][-1] - reference["date"][0]).days / 365.25
    rows.extend(
        _period_rows(
            "research_2005_2016",
            "research_path",
            reference,
            research_index,
            start_levels=(initial, initial, 1.0, 1.0),
            years=research_years,
            published_net=float(summary["annual_return"]),
        )
    )
    validation_years = (ANALYSIS_END - VALIDATION_START).days / 365.25
    for candidate, nav in navs.items():
        boundary = nav.filter(pl.col("date") <= RESEARCH_END).tail(1)
        index_boundary = index.filter(pl.col("date") <= RESEARCH_END).tail(1)
        expected = published.filter(pl.col("candidate") == candidate).item(0, "net_annual_return")
        rows.extend(
            _period_rows(
                "validation_2017_2021",
                candidate,
                _slice(nav, VALIDATION_START, ANALYSIS_END),
                _slice(index, VALIDATION_START, ANALYSIS_END),
                start_levels=(
                    boundary.item(0, "nav"),
                    boundary.item(0, "zero_cost_nav"),
                    index_boundary.item(0, "equal_weight"),
                    index_boundary.item(0, "cap_weight"),
                ),
                years=validation_years,
                published_net=float(expected),
            )
        )
    return pl.DataFrame(rows)


def _period_rows(
    period: str,
    portfolio: str,
    nav: pl.DataFrame,
    index: pl.DataFrame,
    *,
    start_levels: tuple[float, float, float, float],
    years: float,
    published_net: float,
) -> list[dict[str, Any]]:
    if nav.get_column("date").to_list() != index.get_column("date").to_list():
        raise ValueError("strategy and benchmark dates are misaligned")
    nav_start, zero_start, equal_start, cap_start = start_levels
    strategy = {
        "net": [nav_start, *nav.get_column("nav").to_list()],
        "zero_cost": [zero_start, *nav.get_column("zero_cost_nav").to_list()],
    }
    benchmark = {
        "equal_weight": [equal_start, *index.get_column("equal_weight").to_list()],
        "cap_weight": [cap_start, *index.get_column("cap_weight").to_list()],
    }
    rows = []
    for cost_mode, strategy_levels in strategy.items():
        for benchmark_name, benchmark_levels in benchmark.items():
            metrics = relative_performance(
                strategy_levels=strategy_levels,
                benchmark_levels=benchmark_levels,
                years=years,
            )
            if cost_mode == "net" and abs(metrics["strategy_annual_return"] - published_net) > _TOLERANCE:
                raise ValueError(f"{period} {portfolio} does not reproduce its published return")
            rows.append(
                {
                    "period": period,
                    "portfolio": portfolio,
                    "cost_mode": cost_mode,
                    "benchmark": benchmark_name,
                    "start_date": nav.item(0, "date"),
                    "end_date": nav.item(-1, "date"),
                    "years": years,
                    **metrics,
                }
            )
    return rows


def _slice(frame: pl.DataFrame, start: date, end: date) -> pl.DataFrame:
    return frame.filter(pl.col("date").is_between(start, end)).sort("date")


def _growth_levels(nav: pl.DataFrame, index: pl.DataFrame) -> pl.DataFrame:
    first = nav.row(0, named=True)
    return nav.join(index, on="date", how="inner", validate="1:1").select(
        "date",
        (pl.col("nav") / first["nav"]).alias("strategy_net"),
        (pl.col("zero_cost_nav") / first["zero_cost_nav"]).alias("strategy_zero_cost"),
        "equal_weight",
        "cap_weight",
        "member_count",
    )


def _calendar_years(nav: pl.DataFrame, index: pl.DataFrame) -> pl.DataFrame:
    levels = _growth_levels(nav, index)
    year_end = (
        levels.with_columns(pl.col("date").dt.year().alias("year"))
        .group_by("year")
        .agg(pl.all().exclude("year", "member_count").sort_by("date").last())
        .sort("year")
    )
    columns = ("strategy_net", "strategy_zero_cost", "equal_weight", "cap_weight")
    return year_end.select(
        "year",
        *[(pl.col(column) / pl.col(column).shift(1).fill_null(1.0) - 1.0).alias(column) for column in columns],
    ).with_columns(
        (pl.col("strategy_net") - pl.col("equal_weight")).alias("net_minus_equal_weight")
    )


def _quantile_frames(
    root: Path,
    stage5: PublishedRelease,
    stage7: PublishedRelease,
    inputs: dict[str, str],
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Monthly quantile returns of every factor (primary variant) and composite, by period."""
    stage4 = root / "artifacts/factor_research"
    classifications = pl.read_csv(_record(stage4 / "factor_classifications.csv", inputs, root))
    variants = classifications.select(
        "factor_name", pl.col("primary_score_variant").alias("score_variant"), "classification"
    )
    research_factors = pl.read_csv(
        _record(stage4 / "quantile_returns.csv", inputs, root), try_parse_dates=True
    )
    validation_factors = pl.read_parquet(
        stage7.artifacts / "research/factor_quantile_returns.parquet"
    )
    research_composites = pl.read_parquet(stage5.artifacts / "composite_quantiles.parquet")
    validation_composites = pl.read_parquet(
        stage7.artifacts / "research/composite_quantile_returns.parquet"
    )

    def factors(frame: pl.DataFrame) -> pl.DataFrame:
        return frame.join(variants, on=["factor_name", "score_variant"], how="inner").select(
            "date",
            pl.lit("factor").alias("object_type"),
            pl.col("factor_name").alias("object_name"),
            "quantile",
            "n_obs",
            pl.col("mean_forward_return_20").alias("mean_return"),
        )

    def composites(frame: pl.DataFrame) -> pl.DataFrame:
        return frame.filter(pl.col("method").is_in(COMPOSITE_METHODS)).select(
            "date",
            pl.lit("composite").alias("object_type"),
            pl.col("method").alias("object_name"),
            "quantile",
            "n_obs",
            "mean_return",
        )

    parts = []
    for period, start, end, factor_frame, composite_frame in (
        ("research_2005_2016", ANALYSIS_START, RESEARCH_END, research_factors, research_composites),
        (
            "validation_2017_2021",
            VALIDATION_START,
            ANALYSIS_END,
            validation_factors,
            validation_composites,
        ),
    ):
        for frame in (factors(factor_frame), composites(composite_frame)):
            parts.append(
                frame.filter(pl.col("date").is_between(start, end)).with_columns(
                    pl.lit(period).alias("period")
                )
            )
    return pl.concat(parts), variants


def _leg_table(quantiles: pl.DataFrame, variants: pl.DataFrame) -> pl.DataFrame:
    parts = [
        quantile_leg_decomposition(
            frame.drop("period", "object_type"), quantile_count=5
        ).with_columns(pl.lit(period).alias("period"), pl.lit(object_type).alias("object_type"))
        for (period, object_type), frame in quantiles.group_by(
            "period", "object_type", maintain_order=True
        )
    ]
    return (
        pl.concat(parts)
        .join(
            variants.select(pl.col("factor_name").alias("object_name"), "classification"),
            on="object_name",
            how="left",
        )
        .select(
            "period",
            "object_type",
            "object_name",
            "classification",
            pl.exclude("period", "object_type", "object_name", "classification"),
        )
        .sort("period", "object_type", "object_name")
    )


def _quintile_excess_table(quantiles: pl.DataFrame) -> pl.DataFrame:
    parts = [
        quintile_excess(frame.drop("period", "object_type")).with_columns(
            pl.lit(period).alias("period")
        )
        for (period,), frame in quantiles.group_by("period", maintain_order=True)
    ]
    return pl.concat(parts).select("period", "object_name", "quantile", "mean_excess", "months")


def _write_report_figures(
    root: Path,
    output: Path,
    stage5: PublishedRelease,
    stage6: PublishedRelease,
    stage7: PublishedRelease,
    levels: pl.DataFrame,
    quintiles: pl.DataFrame,
    inputs: dict[str, str],
) -> None:
    figures = output / "figures"
    correlations = pl.read_csv(
        _record(root / "artifacts/factor_research/factor_correlations.csv", inputs, root)
    )
    plot_correlation_heatmap(
        correlation_matrix(correlations, order=HEATMAP_ORDER),
        HEATMAP_ORDER,
        figures / "factor_correlation_heatmap.png",
    )

    def composite_ic(frame: pl.DataFrame, start: date, end: date) -> pl.DataFrame:
        return frame.filter(
            (pl.col("horizon") == 20)
            & pl.col("method").is_in(COMPOSITE_METHODS)
            & pl.col("date").is_between(start, end)
            & pl.col("rank_ic").is_not_null()
        ).select("date", "method", "rank_ic")

    rolling = rolling_mean_ic(
        pl.concat(
            [
                composite_ic(
                    pl.read_parquet(stage5.artifacts / "composite_ic.parquet"),
                    ANALYSIS_START,
                    RESEARCH_END,
                ),
                composite_ic(
                    pl.read_parquet(stage7.artifacts / "research/composite_rank_ic.parquet"),
                    VALIDATION_START,
                    ANALYSIS_END,
                ),
            ]
        )
    )
    rolling.write_csv(output / "rolling_ic.csv")
    plot_rolling_ic(rolling, figures / "composite_rolling_ic.png", split=VALIDATION_START)

    breakdown_path = stage6.artifacts / "cost_breakdown.csv"
    inputs[f"stage6:{stage6.run_id}/artifacts/cost_breakdown.csv"] = _sha256(breakdown_path)
    traded = float(
        pl.read_parquet(stage6.datasets / "trades.parquet", columns=["amount"])["amount"].sum()
    )
    components = cost_components(pl.read_csv(breakdown_path).row(0, named=True), traded_amount=traded)
    components.write_csv(output / "cost_components.csv")
    plot_cost_components(components, figures / "research_cost_components.png")

    losses = drawdowns(levels, columns=("strategy_net", "equal_weight", "cap_weight"))
    losses.write_parquet(output / "drawdowns.parquet")
    plot_drawdowns(losses, figures / "drawdowns.png", split=VALIDATION_START)

    quintiles.write_csv(output / "quintile_excess.csv")
    plot_quintile_panels(quintiles, QUINTILE_PANELS, figures / "quintile_excess.png")


def _write_manifest(output: Path, root: Path, inputs: dict[str, str], selected: str) -> None:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain", "--", "src", "configs"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    )
    outputs = {
        str(path.relative_to(output)): _sha256(path)
        for path in sorted(output.rglob("*"))
        if path.is_file()
    }
    manifest = {
        "purpose": "descriptive v1.0 supplements; never used for selection",
        "analysis_period": [ANALYSIS_START.isoformat(), ANALYSIS_END.isoformat()],
        "selected_candidate": selected,
        "code_commit": commit,
        "code_dirty": dirty,
        "inputs": dict(sorted(inputs.items())),
        "outputs": outputs,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
