from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import hashlib
import json
import math

import numpy as np
import polars as pl

from ashare_multifactor.research.factor_metrics import quantile_metrics
from ashare_multifactor.robustness.protocol import FINAL_TEST_METRICS


FACTOR_METRICS = ("rank_ic", "group_spread", "group_monotonicity")
PORTFOLIO_METRICS = (
    "annual_return",
    "annual_volatility",
    "sharpe_zero_rate",
    "maximum_drawdown",
    "turnover",
    "cost_erosion",
    "target_deviation",
    "unfilled_rate",
)
PERIODS = ("research", "validation", "test")


def compute_final_test_metrics(
    *,
    period: str,
    factor_rank_ic: pl.DataFrame,
    factor_groups: pl.DataFrame,
    backtest: dict[str, pl.DataFrame],
    metric_manifest: Sequence[str],
    sealed_protocol: Mapping[str, object],
) -> pl.DataFrame:
    """Return only pre-registered metrics in an auditable long-table schema."""
    metrics = _validate_manifest(metric_manifest, sealed_protocol)
    if period not in PERIODS:
        raise ValueError(f"unknown evaluation period: {period}")
    rows: list[dict[str, object]] = []
    requested_factors = [metric for metric in metrics if metric in FACTOR_METRICS]
    if requested_factors:
        rows.extend(
            _factor_rows(
                period,
                factor_rank_ic,
                factor_groups,
                requested_factors,
            )
        )

    requested_portfolio = [
        metric for metric in metrics if metric in PORTFOLIO_METRICS
    ]
    if requested_portfolio:
        values = _portfolio_values(
            backtest,
            requested_portfolio,
        )
        rows.extend(
            {
                "period": period,
                "scope": "portfolio",
                "entity": "main",
                "metric": metric,
                "value": values[metric],
            }
            for metric in requested_portfolio
        )
    return pl.DataFrame(rows, infer_schema_length=None).sort(
        "scope", "entity", "metric"
    )


def build_period_comparison(
    period_metrics: Iterable[pl.DataFrame],
    *,
    metric_manifest: Sequence[str],
    sealed_protocol: Mapping[str, object],
) -> pl.DataFrame:
    """Place frozen periods beside one another; never pool or rank their values."""
    metrics = _validate_manifest(metric_manifest, sealed_protocol)
    frames = list(period_metrics)
    if not frames:
        raise ValueError("period metrics are empty")
    combined = pl.concat(frames, how="vertical_relaxed")
    required = {"period", "scope", "entity", "metric", "value"}
    if not required.issubset(combined.columns):
        raise ValueError("period metrics have an invalid schema")
    unknown = set(combined.get_column("metric")) - set(metrics)
    if unknown:
        raise ValueError("comparison contains metrics outside the sealed manifest")
    periods = set(combined.get_column("period"))
    if periods != set(PERIODS):
        raise ValueError("comparison requires separate research, validation, and test rows")
    for period in PERIODS:
        present = set(
            combined.filter(pl.col("period") == period).get_column("metric")
        )
        if present != set(metrics):
            raise ValueError("every sealed metric is required in every frozen period")
    keys = ["scope", "entity", "metric", "period"]
    if combined.select(pl.struct(keys).is_duplicated().any()).item():
        raise ValueError("comparison contains duplicate period metric rows")

    wide = combined.pivot(
        on="period",
        index=["scope", "entity", "metric"],
        values="value",
    )
    if any(name not in wide.columns for name in PERIODS):
        raise ValueError("comparison is missing a frozen period")
    wide = wide.select("scope", "entity", "metric", *PERIODS)
    if wide.select(pl.any_horizontal(pl.col(PERIODS).is_null()).any()).item():
        raise ValueError("a metric is not available in every frozen period")
    order = {metric: index for index, metric in enumerate(metrics)}
    return wide.with_columns(
        pl.col("metric").replace_strict(order).alias("__metric_order")
    ).sort("__metric_order", "scope", "entity").drop("__metric_order")


def _validate_manifest(
    metric_manifest: Sequence[str],
    sealed_protocol: Mapping[str, object],
) -> tuple[str, ...]:
    metrics = tuple(str(metric) for metric in metric_manifest)
    sealed = dict(sealed_protocol)
    recorded = str(sealed.pop("sealed_protocol_sha256", ""))
    canonical = json.dumps(
        sealed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    if not recorded or hashlib.sha256(canonical).hexdigest() != recorded:
        raise ValueError("verified seal hash does not match its payload")
    if sealed.get("status") != "sealed":
        raise ValueError("final-test metric seal is not closed")
    sealed_metrics = tuple(str(metric) for metric in sealed.get("metrics", ()))
    if metrics != sealed_metrics:
        raise ValueError("metric manifest must exactly match verified seal metrics")
    if metrics != FINAL_TEST_METRICS:
        raise ValueError("verified seal metrics differ from the frozen Task-5 vocabulary")
    return metrics


def _factor_rows(
    period: str,
    rank_ic: pl.DataFrame,
    groups: pl.DataFrame,
    metrics: Sequence[str],
) -> list[dict[str, object]]:
    required_ic = {"factor_name", "score_variant", "horizon", "rank_ic"}
    required_groups = {
        "date",
        "factor_name",
        "score_variant",
        "quantile",
        "mean_forward_return_20",
    }
    needs_ic = "rank_ic" in metrics
    needs_groups = bool(set(metrics) & {"group_spread", "group_monotonicity"})
    if needs_ic and (
        not required_ic.issubset(rank_ic.columns) or rank_ic.is_empty()
    ):
        raise ValueError("factor Rank IC input is empty or has an invalid schema")
    if needs_groups and (
        not required_groups.issubset(groups.columns) or groups.is_empty()
    ):
        raise ValueError("factor group input is empty or has an invalid schema")
    if needs_ic and needs_groups and set(rank_ic.get_column("factor_name")) != set(
        groups.get_column("factor_name")
    ):
        raise ValueError("factor IC and group entities differ")

    rows: list[dict[str, object]] = []
    source = rank_ic if needs_ic else groups
    for factor in sorted(set(source.get_column("factor_name"))):
        variants = set(
            source.filter(pl.col("factor_name") == factor).get_column("score_variant")
        )
        if len(variants) != 1:
            raise ValueError(f"factor input must contain one sealed score variant: {factor}")
        variant = next(iter(variants))
        values: dict[str, float] = {}
        if needs_ic:
            selected_ic = rank_ic.filter(
                (pl.col("factor_name") == factor)
                & (pl.col("score_variant") == variant)
            )
            if set(selected_ic.get_column("horizon")) != {20}:
                raise ValueError(f"factor input must use the frozen 20-day horizon: {factor}")
            factor_ic = selected_ic.get_column("rank_ic")
            values["rank_ic"] = _finite_mean(factor_ic, f"Rank IC for {factor}")
        if needs_groups:
            factor_groups = groups.filter(
                (pl.col("factor_name") == factor)
                & (pl.col("score_variant") == variant)
            )
            monthly_groups = factor_groups.group_by("date").agg(
                pl.col("quantile").sort().alias("quantiles")
            )
            if any(
                row["quantiles"] != [1, 2, 3, 4, 5]
                for row in monthly_groups.iter_rows(named=True)
            ):
                raise ValueError(
                    f"factor input must contain complete frozen five groups monthly: {factor}"
                )
            spread, monotonicity = quantile_metrics(
                factor_groups,
                factor,
                str(variant),
                5,
            )
            if spread is None or monotonicity is None:
                raise ValueError(f"factor groups are incomplete or duplicated: {factor}")
            if "group_spread" in metrics:
                values["group_spread"] = float(spread)
            if "group_monotonicity" in metrics:
                values["group_monotonicity"] = float(monotonicity)
        if not all(math.isfinite(values[metric]) for metric in metrics):
            raise ValueError(f"factor metric is not finite: {factor}")
        rows.extend(
            {
                "period": period,
                "scope": "factor",
                "entity": factor,
                "metric": metric,
                "value": values[metric],
            }
            for metric in metrics
        )
    return rows


def _portfolio_values(
    backtest: dict[str, pl.DataFrame],
    metrics: Sequence[str],
) -> dict[str, float]:
    values: dict[str, float] = {}
    nav_metrics = {
        "annual_return",
        "annual_volatility",
        "sharpe_zero_rate",
        "maximum_drawdown",
        "cost_erosion",
    }
    if set(metrics) & nav_metrics:
        if "nav" not in backtest:
            raise ValueError("backtest NAV output is missing")
        nav = backtest["nav"].sort("date")
        required_nav = {"date", "nav"}
        if "cost_erosion" in metrics:
            required_nav.add("zero_cost_nav")
        if nav.height < 2 or not required_nav.issubset(nav.columns):
            raise ValueError("NAV output is empty or has an invalid schema")
        path = nav.get_column("nav").to_numpy()
        if np.any(~np.isfinite(path)) or np.any(path <= 0):
            raise ValueError("NAV path must be finite and positive")
        years = max(
            int(
                round(
                    (
                        nav.get_column("date").tail(1).item()
                        - nav.get_column("date").head(1).item()
                    ).days
                    / 365.25
                )
            ),
            1,
        )
        returns = path[1:] / path[:-1] - 1.0
        volatility = (
            float(np.std(returns, ddof=1) * math.sqrt(252.0))
            if len(returns) > 1
            else 0.0
        )
        annual_return = float((path[-1] / path[0]) ** (1.0 / years) - 1.0)
        if "annual_return" in metrics:
            values["annual_return"] = annual_return
        if "annual_volatility" in metrics:
            values["annual_volatility"] = volatility
        if "sharpe_zero_rate" in metrics:
            values["sharpe_zero_rate"] = (
                float(np.mean(returns) / np.std(returns, ddof=1) * math.sqrt(252.0))
                if volatility > 0
                else 0.0
            )
        if "maximum_drawdown" in metrics:
            peaks = np.maximum.accumulate(path)
            values["maximum_drawdown"] = float(np.min(path / peaks - 1.0))
        if "cost_erosion" in metrics:
            zero_path = nav.get_column("zero_cost_nav").to_numpy()
            if np.any(~np.isfinite(zero_path)) or np.any(zero_path <= 0):
                raise ValueError("zero-cost NAV path must be finite and positive")
            zero_return = float(
                (zero_path[-1] / zero_path[0]) ** (1.0 / years) - 1.0
            )
            values["cost_erosion"] = zero_return - annual_return
    if "turnover" in metrics:
        trades = backtest.get("trades")
        nav = backtest.get("nav")
        if trades is None or "amount" not in trades.columns:
            raise ValueError("trades output has an invalid schema")
        if nav is None or nav.height < 2 or not {"date", "nav"}.issubset(nav.columns):
            raise ValueError("NAV output is empty or has an invalid schema")
        nav = nav.sort("date")
        years = max(
            int(round((nav.item(-1, "date") - nav.item(0, "date")).days / 365.25)),
            1,
        )
        gross = float(trades.get_column("amount").sum()) if trades.height else 0.0
        values["turnover"] = gross / (
            2.0 * float(nav.get_column("nav").mean()) * years
        )
    if "target_deviation" in metrics:
        diagnostics = backtest.get("target_diagnostics")
        if (
            diagnostics is None
            or diagnostics.is_empty()
            or "target_deviation_l1" not in diagnostics.columns
        ):
            raise ValueError("target diagnostics are empty or have an invalid schema")
        values["target_deviation"] = _finite_mean(
            diagnostics["target_deviation_l1"], "target deviation"
        )
    if "unfilled_rate" in metrics:
        orders = backtest.get("orders")
        if orders is None or not {"quantity", "remaining_quantity"}.issubset(
            orders.columns
        ):
            raise ValueError("orders output is empty or has an invalid schema")
        requested = float(orders.get_column("quantity").sum()) if orders.height else 0.0
        remaining = (
            float(orders.get_column("remaining_quantity").sum())
            if orders.height
            else 0.0
        )
        values["unfilled_rate"] = remaining / requested if requested else 0.0
    if not all(math.isfinite(value) for value in values.values()):
        raise ValueError("portfolio metric is not finite")
    return values


def _finite_mean(series: pl.Series, label: str) -> float:
    value = series.cast(pl.Float64, strict=False).mean()
    if value is None or not math.isfinite(value):
        raise ValueError(f"{label} is not finite")
    return float(value)
