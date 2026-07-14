from __future__ import annotations

from collections.abc import Sequence

import polars as pl

from ashare_multifactor.combination.definitions import CANDIDATE_FACTORS


def candidate_registry(
    classifications: pl.DataFrame,
    *,
    requested: Sequence[str] | None = None,
) -> dict[str, tuple[str, ...]]:
    required = {"factor_name", "family", "classification", "primary_score_variant"}
    missing = sorted(required - set(classifications.columns))
    if missing:
        raise ValueError("classification table is missing columns: " + ", ".join(missing))
    if classifications.get_column("factor_name").n_unique() != classifications.height:
        raise ValueError("duplicate factor classifications are not allowed")
    candidate_rows = classifications.filter(pl.col("classification") == "candidate")
    candidates = set(candidate_rows.get_column("factor_name"))
    expected = {factor for factors in CANDIDATE_FACTORS.values() for factor in factors}
    if candidates != expected:
        raise ValueError("candidate classification does not match the frozen registry")
    expected_families = {
        factor: family for family, factors in CANDIDATE_FACTORS.items() for factor in factors
    }
    actual_families = dict(
        candidate_rows.select("factor_name", "family").iter_rows()
    )
    if actual_families != expected_families:
        raise ValueError("candidate factor families do not match the frozen registry")
    variants = set(candidate_rows.get_column("primary_score_variant"))
    if variants != {"score_size_neutral"}:
        raise ValueError("candidate factors must use score_size_neutral")
    if requested is not None:
        invalid = sorted(set(requested) - candidates)
        if invalid:
            raise ValueError("non-candidate factors requested: " + ", ".join(invalid))
    return CANDIDATE_FACTORS.copy()
