from dataclasses import replace
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from factor_card_fixtures import (
    complete_empty_card_inputs,
    factor_settings,
    set_correlation_pair,
    set_redundancy_flag,
)
from ashare_multifactor.factors.definitions import FACTOR_DEFINITIONS
from ashare_multifactor.research.factor_card_validation import validate_factor_card_inputs
from ashare_multifactor.research.factor_cards import write_factor_cards
from ashare_multifactor.research.factor_metrics import RANK_IC_SCHEMA


def test_card_writer_rejects_missing_summary_contract_before_creating_output(
    tmp_path: Path,
) -> None:
    settings = factor_settings()
    evaluation, classifications, redundancy = complete_empty_card_inputs(
        FACTOR_DEFINITIONS, settings
    )
    invalid = replace(evaluation, factor_summary=evaluation.factor_summary.head(-1))
    output = tmp_path / "factor_research"

    with pytest.raises(ValueError, match="factor_summary.*complete"):
        write_factor_cards(
            invalid,
            classifications,
            redundancy,
            FACTOR_DEFINITIONS,
            settings,
            output,
        )

    assert not output.exists()


def test_card_writer_rejects_duplicate_machine_rows_before_creating_output(
    tmp_path: Path,
) -> None:
    settings = factor_settings()
    evaluation, classifications, redundancy = complete_empty_card_inputs(
        FACTOR_DEFINITIONS, settings
    )
    duplicate = pl.concat([evaluation.factor_summary, evaluation.factor_summary.head(1)])
    invalid = replace(evaluation, factor_summary=duplicate)
    output = tmp_path / "factor_research"

    with pytest.raises(ValueError, match="factor_summary.*duplicate"):
        write_factor_cards(
            invalid,
            classifications,
            redundancy,
            FACTOR_DEFINITIONS,
            settings,
            output,
        )

    assert not output.exists()


def test_card_writer_rejects_wrong_classification_primary_variant_before_writes(
    tmp_path: Path,
) -> None:
    settings = factor_settings()
    evaluation, classifications, redundancy = complete_empty_card_inputs(
        FACTOR_DEFINITIONS, settings
    )
    factor_name = FACTOR_DEFINITIONS[0].name
    invalid = classifications.with_columns(
        pl.when(pl.col("factor_name") == factor_name)
        .then(pl.lit("score"))
        .otherwise(pl.col("primary_score_variant"))
        .alias("primary_score_variant")
    )
    output = tmp_path / "factor_research"

    with pytest.raises(ValueError, match="classification.*primary score variant"):
        write_factor_cards(
            evaluation,
            invalid,
            redundancy,
            FACTOR_DEFINITIONS,
            settings,
            output,
        )

    assert not output.exists()


def test_card_writer_rejects_out_of_period_metric_date_before_creating_output(
    tmp_path: Path,
) -> None:
    settings = factor_settings()
    evaluation, classifications, redundancy = complete_empty_card_inputs(
        FACTOR_DEFINITIONS, settings
    )
    definition = FACTOR_DEFINITIONS[0]
    rank_ic = pl.DataFrame(
        [
            {
                "date": date(2017, 1, 31),
                "factor_name": definition.name,
                "family": definition.family,
                "score_variant": "score_size_neutral",
                "horizon": settings.primary_horizon,
                "n_obs": 20,
                "rank_ic": 0.1,
                "reason": None,
            }
        ],
        schema=RANK_IC_SCHEMA,
    )
    invalid = replace(evaluation, rank_ic=rank_ic)
    output = tmp_path / "factor_research"

    with pytest.raises(ValueError, match="rank_ic.*analysis period"):
        write_factor_cards(
            invalid,
            classifications,
            redundancy,
            FACTOR_DEFINITIONS,
            settings,
            output,
        )

    assert not output.exists()


def test_card_validation_rejects_missing_expected_redundancy_flag() -> None:
    definitions = FACTOR_DEFINITIONS[:2]
    settings = factor_settings()
    evaluation, classifications, redundancy = complete_empty_card_inputs(definitions, settings)
    redundancy = set_correlation_pair(
        redundancy,
        definitions[0],
        definitions[1],
        mean_correlation=0.75,
        common_months=120,
    )

    with pytest.raises(ValueError, match="redundancy_flags.*missing expected"):
        validate_factor_card_inputs(
            evaluation,
            classifications,
            redundancy,
            definitions,
            settings,
        )


def test_card_validation_rejects_flag_without_threshold_correlation() -> None:
    definitions = FACTOR_DEFINITIONS[:2]
    settings = factor_settings()
    evaluation, classifications, redundancy = complete_empty_card_inputs(definitions, settings)
    redundancy = set_redundancy_flag(
        redundancy,
        definitions[0],
        definitions[1],
        mean_correlation=0.75,
        common_months=120,
    )

    with pytest.raises(ValueError, match="redundancy_flags.*unexpected"):
        validate_factor_card_inputs(
            evaluation,
            classifications,
            redundancy,
            definitions,
            settings,
        )


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("mean_correlation", -0.70),
        ("absolute_correlation", 0.70),
        ("correlation_direction", "positive"),
        ("common_months", 119),
    ],
)
def test_card_validation_rejects_flag_fields_inconsistent_with_correlation_matrix(
    column: str,
    value: object,
) -> None:
    definitions = FACTOR_DEFINITIONS[:2]
    settings = factor_settings()
    evaluation, classifications, redundancy = complete_empty_card_inputs(definitions, settings)
    redundancy = set_correlation_pair(
        redundancy,
        definitions[0],
        definitions[1],
        mean_correlation=-0.75,
        common_months=120,
    )
    redundancy = set_redundancy_flag(
        redundancy,
        definitions[0],
        definitions[1],
        mean_correlation=-0.75,
        common_months=120,
    )
    flags = redundancy.redundancy_flags.with_columns(
        pl.lit(value).cast(redundancy.redundancy_flags.schema[column]).alias(column)
    )
    redundancy = replace(redundancy, redundancy_flags=flags)

    with pytest.raises(ValueError, match="redundancy_flags.*does not match"):
        validate_factor_card_inputs(
            evaluation,
            classifications,
            redundancy,
            definitions,
            settings,
        )


def test_card_validation_rejects_asymmetric_correlation_matrix_rows() -> None:
    definitions = FACTOR_DEFINITIONS[:2]
    settings = factor_settings()
    evaluation, classifications, redundancy = complete_empty_card_inputs(definitions, settings)
    left, right = definitions
    forward = (pl.col("factor_a") == left.name) & (pl.col("factor_b") == right.name)
    correlations = redundancy.factor_correlations.with_columns(
        pl.when(forward)
        .then(pl.lit(0.75))
        .otherwise(pl.col("mean_correlation"))
        .alias("mean_correlation"),
        pl.when(forward)
        .then(pl.lit(120))
        .otherwise(pl.col("common_months"))
        .alias("common_months"),
        pl.when(forward)
        .then(pl.lit(None, dtype=pl.String))
        .otherwise(pl.col("reason"))
        .alias("reason"),
    )
    redundancy = replace(redundancy, factor_correlations=correlations)

    with pytest.raises(ValueError, match="factor_correlations.*mirrored"):
        validate_factor_card_inputs(
            evaluation,
            classifications,
            redundancy,
            definitions,
            settings,
        )
