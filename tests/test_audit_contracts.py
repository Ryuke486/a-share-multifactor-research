from datetime import date
from pathlib import Path

import polars as pl
import pytest

from ashare_multifactor.audit.contracts import (
    DatasetContract,
    dataset_manifest_entry,
    validate_dataset,
)
from ashare_multifactor.audit.records import file_record, verify_file_record


def _contract() -> DatasetContract:
    return DatasetContract(
        name="target_weights",
        primary_key=("date", "portfolio_name", "symbol"),
        date_column="date",
        minimum_date=date(2005, 1, 1),
        maximum_date=date(2016, 12, 31),
        finite_columns=("target_weight",),
        non_negative_columns=("target_weight",),
    )


def _targets(*, signal_date: date = date(2005, 1, 31)) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [signal_date],
            "portfolio_name": ["main"],
            "symbol": ["000001"],
            "target_weight": [1.0],
        }
    )


def test_file_record_detects_content_replacement(tmp_path: Path) -> None:
    path = tmp_path / "input.bin"
    path.write_bytes(b"original")
    record = file_record(path, root=tmp_path, role="factor_panel")
    path.write_bytes(b"replaced")

    with pytest.raises(ValueError, match="digest mismatch"):
        verify_file_record(record, root=tmp_path)


def test_dataset_contract_rejects_duplicate_primary_key() -> None:
    frame = pl.concat((_targets(), _targets()))

    with pytest.raises(ValueError, match="duplicate primary key"):
        validate_dataset(frame, _contract())


def test_dataset_contract_rejects_date_outside_research_period() -> None:
    with pytest.raises(ValueError, match="date range"):
        validate_dataset(_targets(signal_date=date(2017, 1, 31)), _contract())


def test_dataset_contract_rejects_non_finite_value() -> None:
    frame = _targets().with_columns(pl.lit(float("nan")).alias("target_weight"))

    with pytest.raises(ValueError, match="finite"):
        validate_dataset(frame, _contract())


def test_dataset_manifest_entry_records_contract_metadata(tmp_path: Path) -> None:
    path = tmp_path / "target_weights.parquet"
    frame = _targets()
    frame.write_parquet(path)

    entry = dataset_manifest_entry(
        path,
        root=tmp_path,
        role="stage_five_output",
        frame=frame,
        contract=_contract(),
        producer="ashare_multifactor.portfolio",
        stage="factor_combination",
    )

    assert entry["primary_key"] == ["date", "portfolio_name", "symbol"]
    assert entry["rows"] == 1
    assert entry["minimum_date"] == "2005-01-31"
    assert entry["maximum_date"] == "2005-01-31"
    assert entry["schema"]["symbol"] == "String"
    assert entry["producer"] == "ashare_multifactor.portfolio"
