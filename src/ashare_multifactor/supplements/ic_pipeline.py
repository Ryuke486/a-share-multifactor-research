"""Executable-label IC (item 5) and Newey-West lag sensitivity (item 6).

Both tables are descriptive. Before computing anything new, every published Rank IC
series and every published BH q-value is reproduced from the same inputs; any
difference stops the run, so the new columns differ from the frozen results only
by the label or the lag being varied.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from ashare_multifactor.audit.publication import PublishedRelease
from ashare_multifactor.supplements.ic_labels import executable_forward_returns
from ashare_multifactor.supplements.ic_statistics import (
    automatic_lag,
    benjamini_hochberg_with_lag,
    ic_summary,
    monthly_rank_ic,
)


HORIZONS = (5, 20)
PRIMARY_HORIZON = 20
COMPOSITE_METHODS = ("family_equal", "rolling_ic_family")
FACTOR_MINIMUM_CROSS_SECTION = 20
COMPOSITE_MINIMUM_CROSS_SECTION = 3
_IC_TOLERANCE = 1e-10
_PERIODS = (
    ("research_2005_2016", date(2005, 1, 1), date(2016, 12, 31)),
    ("validation_2017_2021", date(2017, 1, 1), date(2021, 12, 31)),
)


def build_ic_supplements(
    root: Path,
    stage5: PublishedRelease,
    stage7: PublishedRelease,
    panel_files: list[Path],
    *,
    fdr_threshold: float,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    variants = pl.read_csv(root / "artifacts/factor_research/factor_classifications.csv").select(
        "factor_name", pl.col("primary_score_variant").alias("variant")
    )
    scores = _scores(root, stage5, stage7, variants)
    published = _published_ic(root, stage5, stage7, variants)
    panel = (
        pl.scan_parquet(panel_files)
        .select("date", "symbol", "open_adj", "close_adj", "volume", "amount")
        .filter(pl.col("date") <= _PERIODS[-1][2])
        .collect()
    )
    labelled = []
    for period, _, end in _PERIODS:
        period_scores = scores.filter(pl.col("period") == period)
        labels = executable_forward_returns(
            panel,
            period_scores.select("date", "symbol").unique(),
            horizons=HORIZONS,
            cutoff=end,
        )
        labelled.append(period_scores.join(labels, on=["date", "symbol"], how="left"))
    frame = pl.concat(labelled)
    executable = _executable_table(frame, published)
    lags = _lag_table(published, root, stage7, fdr_threshold)
    return executable, lags


def _scores(
    root: Path,
    stage5: PublishedRelease,
    stage7: PublishedRelease,
    variants: pl.DataFrame,
) -> pl.DataFrame:
    labels = [f"forward_return_{horizon}" for horizon in HORIZONS]

    def factors(path: Path, period: str, start: date, end: date) -> pl.DataFrame:
        panel = pl.read_parquet(
            path,
            columns=["date", "symbol", "factor_name", "score", "score_size_neutral", *labels],
        ).filter(pl.col("date").is_between(start, end))
        return panel.join(variants, on="factor_name", how="inner").select(
            pl.lit(period).alias("period"),
            pl.lit("factor").alias("object_type"),
            pl.col("factor_name").alias("object_name"),
            "date",
            "symbol",
            pl.when(pl.col("variant") == "score")
            .then(pl.col("score"))
            .otherwise(pl.col("score_size_neutral"))
            .alias("score_value"),
            *labels,
        )

    def composites(
        scores_path: Path, returns_path: Path, period: str, start: date, end: date
    ) -> pl.DataFrame:
        returns = pl.read_parquet(returns_path, columns=["date", "symbol", *labels])
        return (
            pl.read_parquet(scores_path, columns=["date", "symbol", "method", "score"])
            .filter(pl.col("method").is_in(COMPOSITE_METHODS))
            .filter(pl.col("date").is_between(start, end))
            .join(returns, on=["date", "symbol"], how="left", validate="m:1")
            .select(
                pl.lit(period).alias("period"),
                pl.lit("composite").alias("object_type"),
                pl.col("method").alias("object_name"),
                "date",
                "symbol",
                pl.col("score").alias("score_value"),
                *labels,
            )
        )

    (research, research_start, research_end), (validation, validation_start, validation_end) = (
        _PERIODS
    )
    return pl.concat(
        [
            factors(
                root / "processed/factor_research/factor_panel.parquet",
                research, research_start, research_end,
            ),
            factors(
                stage7.datasets / "inputs/factor_panel.parquet",
                validation, validation_start, validation_end,
            ),
            composites(
                stage5.datasets / "composite_scores.parquet",
                root / "processed/factor_research/forward_returns.parquet",
                research, research_start, research_end,
            ),
            composites(
                stage7.datasets / "inputs/composite_scores.parquet",
                stage7.datasets / "inputs/forward_returns.parquet",
                validation, validation_start, validation_end,
            ),
        ]
    )


def _published_ic(
    root: Path,
    stage5: PublishedRelease,
    stage7: PublishedRelease,
    variants: pl.DataFrame,
) -> pl.DataFrame:
    """Published monthly Rank IC series keyed like the recomputed ones."""

    def factor_series(frame: pl.DataFrame, period: str) -> pl.DataFrame:
        return frame.join(
            variants, left_on=["factor_name", "score_variant"], right_on=["factor_name", "variant"]
        ).select(
            pl.lit(period).alias("period"),
            pl.col("factor_name").alias("object_name"),
            pl.col("date").cast(pl.Date),
            "horizon",
            "rank_ic",
        )

    def composite_series(frame: pl.DataFrame, period: str) -> pl.DataFrame:
        return frame.filter(pl.col("method").is_in(COMPOSITE_METHODS)).select(
            pl.lit(period).alias("period"),
            pl.col("method").alias("object_name"),
            pl.col("date").cast(pl.Date),
            "horizon",
            "rank_ic",
        )

    return pl.concat(
        [
            factor_series(
                pl.read_csv(root / "artifacts/factor_research/rank_ic.csv", try_parse_dates=True),
                _PERIODS[0][0],
            ),
            factor_series(
                pl.read_parquet(stage7.artifacts / "research/factor_rank_ic.parquet"),
                _PERIODS[1][0],
            ),
            composite_series(
                pl.read_parquet(stage5.artifacts / "composite_ic.parquet"), _PERIODS[0][0]
            ),
            composite_series(
                pl.read_parquet(stage7.artifacts / "research/composite_rank_ic.parquet"),
                _PERIODS[1][0],
            ),
        ]
    ).filter(pl.col("rank_ic").is_not_null() & pl.col("horizon").is_in(HORIZONS))


def _published_lag(horizon: int) -> int:
    return max(0, -(-horizon // 21) - 1)


def _executable_table(frame: pl.DataFrame, published: pl.DataFrame) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    groups = frame.partition_by(["period", "object_type", "object_name"], as_dict=True)
    for (period, object_type, object_name), group in sorted(groups.items()):
        minimum = (
            FACTOR_MINIMUM_CROSS_SECTION if object_type == "factor" else COMPOSITE_MINIMUM_CROSS_SECTION
        )
        for horizon in HORIZONS:
            close, executable = f"forward_return_{horizon}", f"executable_return_{horizon}"
            full = monthly_rank_ic(group, score="score_value", label=close, minimum=minimum)
            _assert_reproduces(full, published, period, object_name, horizon)
            common = group.filter(pl.col(close).is_finite() & pl.col(executable).is_finite())
            lag = _published_lag(horizon)
            close_ic = monthly_rank_ic(common, score="score_value", label=close, minimum=minimum)
            open_ic = monthly_rank_ic(common, score="score_value", label=executable, minimum=minimum)
            close_summary = ic_summary(close_ic.get_column("rank_ic").to_list(), lag=lag)
            open_summary = ic_summary(open_ic.get_column("rank_ic").to_list(), lag=lag)
            rows.append(
                {
                    "period": period,
                    "object_type": object_type,
                    "object_name": object_name,
                    "horizon": horizon,
                    "label_coverage": common.height / group.filter(pl.col(close).is_finite()).height,
                    "months": open_summary["n"],
                    "close_mean_ic": close_summary["mean"],
                    "close_icir": close_summary["icir"],
                    "close_t": close_summary["t"],
                    "open_mean_ic": open_summary["mean"],
                    "open_icir": open_summary["icir"],
                    "open_t": open_summary["t"],
                    "mean_ic_change": open_summary["mean"] - close_summary["mean"],
                    "mean_ic_retained": open_summary["mean"] / close_summary["mean"],
                }
            )
    return pl.DataFrame(rows)


def _assert_reproduces(
    recomputed: pl.DataFrame,
    published: pl.DataFrame,
    period: str,
    object_name: str,
    horizon: int,
) -> None:
    reference = published.filter(
        (pl.col("period") == period)
        & (pl.col("object_name") == object_name)
        & (pl.col("horizon") == horizon)
    ).select("date", pl.col("rank_ic").alias("published"))
    joined = reference.join(recomputed, on="date", how="full", coalesce=True)
    mismatched = joined.filter(
        pl.col("published").is_null()
        | pl.col("rank_ic").is_null()
        | ((pl.col("published") - pl.col("rank_ic")).abs() > _IC_TOLERANCE)
    )
    if mismatched.height:
        raise ValueError(
            f"{period} {object_name} h={horizon}: {mismatched.height} months do not reproduce "
            "the published Rank IC"
        )


def _lag_table(
    published: pl.DataFrame,
    root: Path,
    stage7: PublishedRelease,
    fdr_threshold: float,
) -> pl.DataFrame:
    summaries = {
        "research_2005_2016": pl.read_csv(root / "artifacts/factor_research/factor_summary.csv"),
        "validation_2017_2021": pl.read_parquet(stage7.artifacts / "research/factor_summary.parquet"),
    }
    variants = pl.read_csv(root / "artifacts/factor_research/factor_classifications.csv").select(
        "factor_name", pl.col("primary_score_variant").alias("score_variant")
    )
    lag = _published_lag(PRIMARY_HORIZON)
    rows: list[dict[str, Any]] = []
    for period, _, _ in _PERIODS:
        primary = published.filter(
            (pl.col("period") == period) & (pl.col("horizon") == PRIMARY_HORIZON)
        ).sort("date")
        series = {
            name: frame.get_column("rank_ic").to_list()
            for (name,), frame in primary.group_by("object_name", maintain_order=True)
        }
        factor_names = set(variants.get_column("factor_name").to_list())
        factor_series = {name: values for name, values in series.items() if name in factor_names}
        months = len(next(iter(factor_series.values())))
        auto = automatic_lag(months)
        at_published = benjamini_hochberg_with_lag(factor_series, lag=lag)
        at_auto = benjamini_hochberg_with_lag(factor_series, lag=auto)
        reference = summaries[period].join(variants, on=["factor_name", "score_variant"]).filter(
            pl.col("horizon") == PRIMARY_HORIZON
        )
        for row in reference.iter_rows(named=True):
            name = row["factor_name"]
            if abs(at_published[name]["t"] - row["nw_t"]) > _IC_TOLERANCE or abs(
                at_published[name]["q"] - row["bh_q"]
            ) > _IC_TOLERANCE:
                raise ValueError(f"{period} {name}: published NW t or BH q does not reproduce")
        for name, values in sorted(series.items()):
            is_factor = name in factor_series
            base = at_published[name] if is_factor else ic_summary(values, lag=lag)
            alternative = at_auto[name] if is_factor else ic_summary(values, lag=auto)
            rows.append(
                {
                    "period": period,
                    "object_type": "factor" if is_factor else "composite",
                    "object_name": name,
                    "months": base["n"],
                    "mean_ic": base["mean"],
                    "published_lag": lag,
                    "t_published_lag": base["t"],
                    "automatic_lag": auto,
                    "t_automatic_lag": alternative["t"],
                    "q_published_lag": base.get("q"),
                    "q_automatic_lag": alternative.get("q"),
                    "fdr_pass_published_lag": (
                        base["q"] <= fdr_threshold if is_factor else None
                    ),
                    "fdr_pass_automatic_lag": (
                        alternative["q"] <= fdr_threshold if is_factor else None
                    ),
                }
            )
    return pl.DataFrame(rows)
