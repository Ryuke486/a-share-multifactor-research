from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import polars as pl

from ashare_multifactor.robustness.protocol import RobustnessProtocol


def build_robustness_long_table(
    results: Iterable[dict[str, Any]],
    protocol: RobustnessProtocol,
) -> pl.DataFrame:
    """Normalize every pre-registered result, including failures, into one schema."""
    definitions = {item.experiment_id: item for item in protocol.experiments}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        experiment_id = str(result["experiment_id"])
        if experiment_id not in definitions:
            raise ValueError(f"result is not pre-registered: {experiment_id}")
        grouped.setdefault(experiment_id, []).append(result)
    rows: list[dict[str, Any]] = []
    for experiment_id, experiment in definitions.items():
        entries = grouped.get(experiment_id) or [
            {
                "experiment_id": experiment_id,
                "status": "not_run",
                "sample": "not_run",
                "observations": 0,
                "reason": "pre_registered_experiment_not_run",
                "metrics": {},
            }
        ]
        for entry in entries:
            metrics = dict(entry.get("metrics", {}))
            if not metrics:
                metrics = {"__status__": None}
            for metric, value in sorted(metrics.items()):
                rows.append(
                    {
                        "experiment_id": experiment_id,
                        "category": experiment.category,
                        "unique_change": experiment.change,
                        "status": str(entry.get("status", "ready")),
                        "sample": str(entry.get("sample", "2005-2021")),
                        "start_date": entry.get("start_date"),
                        "end_date": entry.get("end_date"),
                        "observations": int(entry.get("observations", 0)),
                        "metric": str(metric),
                        "value": float(value) if value is not None else None,
                        "reason": entry.get("reason"),
                    }
                )
    frame = pl.DataFrame(rows, infer_schema_length=None)
    baselines = _baseline_values(frame)
    enriched = []
    tolerances = {
        "annual_return": float(protocol.interpretation["return_tolerance"]),
        "maximum_drawdown": float(protocol.interpretation["drawdown_tolerance"]),
        "turnover": float(protocol.interpretation["turnover_tolerance"]),
    }
    for row in frame.iter_rows(named=True):
        baseline_id = _baseline_id(row["category"])
        baseline = baselines.get((baseline_id, row["metric"]))
        delta = (
            row["value"] - baseline
            if row["value"] is not None and baseline is not None
            else None
        )
        status = row["status"]
        if status != "ready":
            interpretation = "failed"
        elif row["metric"] in tolerances and delta is not None:
            if row["metric"] == "maximum_drawdown":
                stable = delta >= -tolerances[row["metric"]]
            else:
                stable = abs(delta) <= tolerances[row["metric"]]
            interpretation = "stable" if stable else "sensitive"
        else:
            interpretation = "reported"
        enriched.append(
            row
            | {
                "baseline_experiment_id": baseline_id,
                "baseline_value": baseline,
                "baseline_delta": delta,
                "interpretation": interpretation,
            }
        )
    return pl.DataFrame(enriched, infer_schema_length=None).sort(
        "experiment_id", "sample", "metric"
    )


def assess_test_protocol_gate(
    table: pl.DataFrame,
    *,
    fatal_events: Iterable[dict[str, object]],
    protocol: RobustnessProtocol,
) -> dict[str, object]:
    fatal_statuses = set(protocol.interpretation["fatal_statuses"])
    fatal = [item for item in fatal_events if item.get("status") in fatal_statuses]
    fatal.extend(
        {
            "status": row["status"],
            "reason": row["reason"],
            "experiment_id": row["experiment_id"],
        }
        for row in table.filter(pl.col("status").is_in(fatal_statuses))
        .select("status", "reason", "experiment_id")
        .unique()
        .iter_rows(named=True)
    )
    missing = sorted(
        table.filter(pl.col("status") == "not_run")
        .get_column("experiment_id")
        .unique()
        .to_list()
    )
    if fatal:
        status = "fatal_defect"
    elif missing:
        status = "incomplete"
    else:
        status = "ready_to_seal"
    return {
        "status": status,
        "sealed_test_protocol_allowed": status == "ready_to_seal",
        "fatal_events": fatal,
        "missing_experiments": missing,
    }


def _baseline_id(category: str) -> str | None:
    if category == "execution":
        return "execution_full_cost"
    if category in {"portfolio", "factor_ablation", "rolling_window"}:
        return "portfolio_baseline"
    return None


def _baseline_values(frame: pl.DataFrame) -> dict[tuple[str, str], float]:
    rows = frame.filter(
        pl.col("experiment_id").is_in(
            ["execution_full_cost", "portfolio_baseline"]
        )
        & pl.col("value").is_not_null()
    )
    return {
        (row["experiment_id"], row["metric"]): row["value"]
        for row in rows.iter_rows(named=True)
    }
