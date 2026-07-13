from dataclasses import replace
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from factor_card_fixtures import (
    RANK_IC_SCHEMA,
    complete_empty_card_inputs,
    factor_settings,
)
from ashare_multifactor.factors.definitions import FACTOR_DEFINITIONS
from ashare_multifactor.research.factor_cards import write_factor_cards


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
