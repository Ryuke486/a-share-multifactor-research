from __future__ import annotations

import polars as pl


def portfolio_diagnostics(current: pl.DataFrame, previous: pl.DataFrame) -> dict[str, float | int]:
    current_weights = dict(zip(current["symbol"], current["target_weight"]))
    previous_weights = (
        dict(zip(previous["symbol"], previous["target_weight"]))
        if not previous.is_empty()
        else {}
    )
    symbols = current_weights.keys() | previous_weights.keys()
    turnover = 0.5 * sum(
        abs(current_weights.get(symbol, 0.0) - previous_weights.get(symbol, 0.0))
        for symbol in symbols
    )
    weights = current.get_column("target_weight")
    hhi = float((weights * weights).sum())
    return {
        "one_way_turnover": turnover,
        "holdings": current.height,
        "max_weight": float(weights.max()),
        "hhi": hhi,
        "effective_holdings": 1.0 / hhi,
    }


def compare_portfolios(
    baseline: pl.DataFrame,
    primary: pl.DataFrame,
    *,
    baseline_turnover: float,
    primary_turnover: float,
) -> dict[str, float]:
    baseline_symbols = set(baseline.get_column("symbol"))
    primary_symbols = set(primary.get_column("symbol"))
    denominator = max(min(len(baseline_symbols), len(primary_symbols)), 1)
    return {
        "holding_overlap": len(baseline_symbols & primary_symbols) / denominator,
        "turnover_difference": primary_turnover - baseline_turnover,
    }


def quality_warnings(
    *,
    portfolio_name: str,
    holdings: int,
    expected_holdings: int,
    coverage: float,
    minimum_coverage: float,
    turnover: float,
    turnover_warning: float,
) -> list[dict[str, object]]:
    warnings: list[dict[str, object]] = []
    values = (
        (holdings < expected_holdings, "insufficient_holdings", str(holdings)),
        (coverage < minimum_coverage, "coverage_decline", f"{coverage:.6f}"),
        (turnover > turnover_warning, "high_target_turnover", f"{turnover:.6f}"),
    )
    for active, rule, details in values:
        if active:
            warnings.append(
                {
                    "severity": "warning",
                    "portfolio_name": portfolio_name,
                    "rule": rule,
                    "details": details,
                }
            )
    return warnings
