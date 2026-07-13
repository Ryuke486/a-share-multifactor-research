"""Field coverage metrics and evidence gates for factor research."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

import polars as pl

from ashare_multifactor.factors.definitions import FACTOR_DEFINITIONS, FactorDefinition


_VALUATION_FIELDS = frozenset({"pe_ttm", "pb", "ps_ttm"})
_NON_NUMERIC_FIELDS = frozenset({"industry", "is_st"})
_PROTOCOL_NOTE = "Field is accepted for use by the frozen stage-four research protocol."


@dataclass(frozen=True)
class FieldReadiness:
    factor_status: dict[str, str]
    industry_neutralization_enabled: bool
    field_metrics: dict[str, dict[str, object]]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def audit_factor_fields(
    frame: pl.DataFrame,
    definitions: Sequence[FactorDefinition] = FACTOR_DEFINITIONS,
    *,
    valuation_verified: bool = False,
    industry_verified: bool = False,
    historical_st_verified: bool = False,
) -> FieldReadiness:
    """Summarize factor inputs and apply explicit point-in-time evidence gates."""
    definitions = tuple(definitions)
    affected = _affected_factors(definitions)
    fields = tuple(affected) + ("industry", "is_st")
    metrics = {
        field: _field_metrics(
            frame,
            field,
            affected.get(field, _neutralized_factor_names(definitions)),
            point_in_time_status=_point_in_time_status(
                field,
                valuation_verified=valuation_verified,
                industry_verified=industry_verified,
                historical_st_verified=historical_st_verified,
            ),
            evidence_note=_evidence_note(
                field,
                valuation_verified=valuation_verified,
                industry_verified=industry_verified,
                historical_st_verified=historical_st_verified,
            ),
        )
        for field in fields
    }
    metrics["is_st"]["affected_factors"] = [definition.name for definition in definitions]

    factor_status = {
        definition.name: _factor_status(
            frame,
            definition,
            valuation_verified=valuation_verified,
        )
        for definition in definitions
    }
    industry_available = (
        "industry" in frame.columns and metrics["industry"]["non_null_rate"] > 0
    )
    return FieldReadiness(
        factor_status=factor_status,
        industry_neutralization_enabled=industry_verified and industry_available,
        field_metrics=metrics,
    )


def write_data_readiness(path: Path, readiness: FieldReadiness) -> None:
    """Write the deterministic ``data_readiness.json`` artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(readiness.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _affected_factors(
    definitions: Sequence[FactorDefinition],
) -> dict[str, list[str]]:
    affected: dict[str, list[str]] = {}
    for definition in definitions:
        for field in definition.required_fields:
            affected.setdefault(field, []).append(definition.name)
    return affected


def _neutralized_factor_names(definitions: Sequence[FactorDefinition]) -> list[str]:
    return [definition.name for definition in definitions if definition.size_neutralize]


def _factor_status(
    frame: pl.DataFrame,
    definition: FactorDefinition,
    *,
    valuation_verified: bool,
) -> str:
    if any(field not in frame.columns for field in definition.required_fields):
        return "missing"
    if any(frame.get_column(field).null_count() == frame.height for field in definition.required_fields):
        return "missing"
    if definition.requires_verified_pit and not valuation_verified:
        return "unverified"
    return "ready"


def _field_metrics(
    frame: pl.DataFrame,
    field: str,
    affected_factors: list[str],
    *,
    point_in_time_status: str,
    evidence_note: str,
) -> dict[str, object]:
    if field not in frame.columns:
        numeric = field not in _NON_NUMERIC_FIELDS
        return {
            "non_null_rate": 0.0,
            "finite_rate": 0.0 if numeric else None,
            "positive_rate": 0.0 if numeric else None,
            "earliest_date": None,
            "latest_date": None,
            "point_in_time_status": point_in_time_status,
            "evidence_note": evidence_note,
            "affected_factors": list(affected_factors),
        }

    non_null = pl.col(field).is_not_null()
    non_null_rate = _rate(frame, non_null)
    earliest, latest = _field_date_range(frame, field)
    numeric = frame.schema[field].is_numeric()
    finite_rate = None
    positive_rate = None
    if numeric:
        numeric_column = pl.col(field).cast(pl.Float64, strict=False)
        finite = numeric_column.is_finite()
        finite_rate = _rate(frame, finite)
        positive_rate = _rate(frame, finite & (numeric_column > 0))
    return {
        "non_null_rate": non_null_rate,
        "finite_rate": finite_rate,
        "positive_rate": positive_rate,
        "earliest_date": earliest,
        "latest_date": latest,
        "point_in_time_status": point_in_time_status,
        "evidence_note": evidence_note,
        "affected_factors": list(affected_factors),
    }


def _rate(frame: pl.DataFrame, condition: pl.Expr) -> float:
    if frame.is_empty():
        return 0.0
    count = frame.select(condition.fill_null(False).sum()).item()
    return count / frame.height


def _field_date_range(frame: pl.DataFrame, field: str) -> tuple[str | None, str | None]:
    if "date" not in frame.columns:
        return None, None
    dates = frame.filter(pl.col(field).is_not_null()).get_column("date").drop_nulls()
    if dates.is_empty():
        return None, None
    return _date_string(dates.min()), _date_string(dates.max())


def _date_string(value: date | datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _point_in_time_status(
    field: str,
    *,
    valuation_verified: bool,
    industry_verified: bool,
    historical_st_verified: bool,
) -> str:
    if field in _VALUATION_FIELDS:
        return "verified" if valuation_verified else "unverified"
    if field == "industry":
        return "verified" if industry_verified else "unverified"
    if field == "is_st":
        return "verified" if historical_st_verified else "unverified"
    return "protocol_accepted"


def _evidence_note(
    field: str,
    *,
    valuation_verified: bool,
    industry_verified: bool,
    historical_st_verified: bool,
) -> str:
    if field in _VALUATION_FIELDS:
        if valuation_verified:
            return "Historical point-in-time valuation provenance has been explicitly verified."
        return "Historical point-in-time valuation provenance has not been verified."
    if field == "industry":
        if industry_verified:
            return "Historical industry classification provenance has been explicitly verified."
        return "Historical industry classification provenance has not been verified."
    if field == "is_st":
        if historical_st_verified:
            return "Historical ST status provenance has been explicitly verified."
        return (
            "Historical ST status provenance has not been verified; "
            "this affects the universe filter."
        )
    return _PROTOCOL_NOTE
