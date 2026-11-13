"""Fama-MacBeth table of the registered factors (descriptive supplement).

Two specifications are fixed here, before looking at any coefficient: the six
factors the frozen classification marked ``candidate`` (the ones the composite
uses) and all fourteen registered factors. Scores are the published primary
variants, already direction-adjusted, so a positive premium agrees with the
pre-registered direction. Nothing here feeds back into a factor, weight or choice.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from ashare_multifactor.audit.publication import PublishedRelease
from ashare_multifactor.supplements.fama_macbeth import (
    fama_macbeth_summary,
    monthly_cross_sectional_regressions,
)
from ashare_multifactor.supplements.ic_pipeline import (
    _PERIODS,
    PRIMARY_HORIZON,
    _published_lag,
    _scores,
)

MINIMUM_CROSS_SECTION = 100
SPECIFICATIONS = ("candidates", "all_registered")


def build_fama_macbeth_table(
    root: Path, stage5: PublishedRelease, stage7: PublishedRelease
) -> pl.DataFrame:
    classifications = pl.read_csv(root / "artifacts/factor_research/factor_classifications.csv")
    variants = classifications.select(
        "factor_name", pl.col("primary_score_variant").alias("variant")
    )
    registered = sorted(classifications.get_column("factor_name").to_list())
    candidates = sorted(
        classifications.filter(pl.col("classification") == "candidate")
        .get_column("factor_name")
        .to_list()
    )
    scores = _scores(root, stage5, stage7, variants).filter(pl.col("object_type") == "factor")
    return fama_macbeth_table(
        scores,
        specifications={"candidates": candidates, "all_registered": registered},
        label=f"forward_return_{PRIMARY_HORIZON}",
        published_lag=_published_lag(PRIMARY_HORIZON),
    )


def fama_macbeth_table(
    scores: pl.DataFrame,
    *,
    specifications: dict[str, list[str]],
    label: str,
    published_lag: int,
) -> pl.DataFrame:
    """Summarise every specification in every period from long-form factor scores."""
    parts: list[pl.DataFrame] = []
    for period, _, _ in _PERIODS:
        period_scores = scores.filter(pl.col("period") == period)
        labels = period_scores.select("date", "symbol", label).unique(
            subset=["date", "symbol"], keep="first", maintain_order=True
        )
        wide = period_scores.pivot(
            on="object_name", index=["date", "symbol"], values="score_value"
        ).join(labels, on=["date", "symbol"], how="left", validate="1:1")
        for specification, factors in specifications.items():
            missing = [name for name in factors if name not in wide.columns]
            if missing:
                raise ValueError(f"{period}: factor scores missing for {missing}")
            monthly = monthly_cross_sectional_regressions(
                wide, factors=factors, label=label, minimum=MINIMUM_CROSS_SECTION
            )
            parts.append(
                fama_macbeth_summary(monthly, published_lag=published_lag).select(
                    pl.lit(period).alias("period"),
                    pl.lit(specification).alias("specification"),
                    pl.lit(len(factors)).alias("regressors"),
                    pl.all(),
                )
            )
    return pl.concat(parts)
