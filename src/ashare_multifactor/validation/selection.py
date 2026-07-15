from __future__ import annotations

from typing import Any

import polars as pl

from ashare_multifactor.config import ValidationEvaluationSettings


def select_validation_candidate(
    metrics: pl.DataFrame,
    settings: ValidationEvaluationSettings,
) -> dict[str, Any]:
    """Apply the frozen improvement, drawdown, and turnover gates verbatim."""
    required = {"candidate", "net_annual_return", "maximum_drawdown", "turnover"}
    missing = sorted(required - set(metrics.columns))
    if missing:
        raise ValueError("validation metrics are missing: " + ", ".join(missing))
    if metrics.get_column("candidate").n_unique() != metrics.height:
        raise ValueError("duplicate validation candidates")
    rows = {row["candidate"]: row for row in metrics.iter_rows(named=True)}
    if set(rows) != set(settings.candidates):
        raise ValueError("validation metrics do not match the frozen candidates")
    baseline = rows[settings.default_candidate]
    decisions: list[dict[str, Any]] = []
    passing: list[dict[str, Any]] = []
    for candidate in settings.candidates:
        row = rows[candidate]
        failed: list[str] = []
        if candidate != settings.default_candidate:
            if row["net_annual_return"] < (
                baseline["net_annual_return"] + settings.minimum_return_improvement
            ):
                failed.append("minimum_return_improvement")
            if row["maximum_drawdown"] < (
                baseline["maximum_drawdown"]
                - settings.maximum_drawdown_deterioration
            ):
                failed.append("maximum_drawdown_deterioration")
            if row["turnover"] > (
                baseline["turnover"] + settings.maximum_turnover_increase
            ):
                failed.append("maximum_turnover_increase")
        decision = {
            "candidate": candidate,
            "passed": not failed,
            "failed_rules": failed,
            "metrics": {
                "net_annual_return": row["net_annual_return"],
                "maximum_drawdown": row["maximum_drawdown"],
                "turnover": row["turnover"],
            },
        }
        decisions.append(decision)
        if candidate != settings.default_candidate and not failed:
            passing.append(decision)
    selected = (
        max(
            passing,
            key=lambda item: (item["metrics"]["net_annual_return"], item["candidate"]),
        )["candidate"]
        if passing
        else settings.default_candidate
    )
    for decision in decisions:
        decision["selected"] = decision["candidate"] == selected
    return {
        "status": "selected_by_frozen_rules",
        "primary_metric": settings.primary_metric,
        "default_candidate": settings.default_candidate,
        "selected_candidate": selected,
        "candidates": decisions,
    }
