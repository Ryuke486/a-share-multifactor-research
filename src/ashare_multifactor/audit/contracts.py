from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import polars as pl

from ashare_multifactor.audit.records import file_record


@dataclass(frozen=True)
class DatasetContract:
    name: str
    primary_key: tuple[str, ...]
    date_column: str | None = None
    minimum_date: date | None = None
    maximum_date: date | None = None
    finite_columns: tuple[str, ...] = ()
    non_negative_columns: tuple[str, ...] = ()


def validate_dataset(frame: pl.DataFrame, contract: DatasetContract) -> None:
    required = set(contract.primary_key) | set(contract.finite_columns) | set(
        contract.non_negative_columns
    )
    if contract.date_column is not None:
        required.add(contract.date_column)
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{contract.name} missing columns: " + ", ".join(missing))
    if frame.select(*contract.primary_key).null_count().row(0) != (0,) * len(
        contract.primary_key
    ):
        raise ValueError(f"{contract.name} primary key contains nulls")
    if frame.select(*contract.primary_key).is_duplicated().any():
        raise ValueError(f"{contract.name} has duplicate primary key")
    if contract.date_column is not None and not frame.is_empty():
        values = frame.get_column(contract.date_column)
        if values.null_count():
            raise ValueError(f"{contract.name} date range contains nulls")
        if contract.minimum_date is not None and values.min() < contract.minimum_date:
            raise ValueError(f"{contract.name} date range starts too early")
        if contract.maximum_date is not None and values.max() > contract.maximum_date:
            raise ValueError(f"{contract.name} date range ends too late")
    for column in contract.finite_columns:
        if frame.filter(~pl.col(column).is_finite()).height:
            raise ValueError(f"{contract.name}.{column} must be finite")
    for column in contract.non_negative_columns:
        if frame.filter(pl.col(column) < 0).height:
            raise ValueError(f"{contract.name}.{column} must be non-negative")


def dataset_manifest_entry(
    path: Path,
    *,
    root: Path,
    role: str,
    frame: pl.DataFrame,
    contract: DatasetContract,
    producer: str,
    stage: str,
) -> dict[str, object]:
    validate_dataset(frame, contract)
    record = file_record(path, root=root, role=role).to_dict()
    minimum = None
    maximum = None
    if contract.date_column is not None and not frame.is_empty():
        values = frame.get_column(contract.date_column)
        minimum = values.min().isoformat()
        maximum = values.max().isoformat()
    return {
        **record,
        "dataset": contract.name,
        "schema": {name: str(dtype) for name, dtype in frame.schema.items()},
        "primary_key": list(contract.primary_key),
        "rows": frame.height,
        "minimum_date": minimum,
        "maximum_date": maximum,
        "producer": producer,
        "stage": stage,
    }
