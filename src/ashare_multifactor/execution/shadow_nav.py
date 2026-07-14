from __future__ import annotations

import polars as pl


def shadow_nav_audit(
    execution_panel: pl.DataFrame,
    corporate_actions: pl.DataFrame,
    positions: pl.DataFrame,
    nav: pl.DataFrame | None = None,
    *,
    threshold: float,
) -> tuple[dict[str, object], pl.DataFrame]:
    """Compare held-portfolio raw economic returns with adjusted-price returns."""
    if not 0 < threshold < 1:
        raise ValueError("shadow threshold must be inside (0, 1)")
    required = {"date", "symbol", "close_raw", "close_adj"}
    if required - set(execution_panel.columns):
        raise ValueError("shadow audit requires raw and adjusted close prices")
    observed_dates = execution_panel.select("symbol", "date").sort("symbol", "date")
    aligned_actions = (
        corporate_actions.sort("symbol", "ex_date")
        .join_asof(
            observed_dates,
            left_on="ex_date",
            right_on="date",
            by="symbol",
            strategy="forward",
            check_sortedness=False,
        )
        .filter(pl.col("date").is_not_null())
    )
    actions = aligned_actions.group_by("date", "symbol").agg(
        pl.col("cash_per_share").sum().alias("cash_per_share"),
        (pl.col("share_ratio") + 1.0).product().alias("share_multiplier"),
    )
    returns = (
        execution_panel.sort("symbol", "date")
        .with_columns(
            pl.col("close_raw").shift(1).over("symbol").alias("lag_close_raw"),
            pl.col("close_adj").shift(1).over("symbol").alias("lag_close_adj"),
        )
        .join(
            actions,
            on=["date", "symbol"],
            how="left",
        )
        .with_columns(
            pl.col("cash_per_share").fill_null(0.0),
            pl.col("share_multiplier").fill_null(1.0),
        )
        .with_columns(
            (pl.col("close_adj") / pl.col("lag_close_adj") - 1.0).alias(
                "adjusted_return"
            ),
            (
                (
                    pl.col("close_raw") * pl.col("share_multiplier")
                    + pl.col("cash_per_share")
                )
                / pl.col("lag_close_raw")
                - 1.0
            ).alias("raw_economic_return"),
        )
        .with_columns(
            (pl.col("adjusted_return") - pl.col("raw_economic_return")).alias(
                "return_difference"
            )
        )
    )
    market_dates = execution_panel["date"].unique().sort().to_list()
    next_dates = pl.DataFrame(
        {"position_date": market_dates[:-1], "date": market_dates[1:]}
    )
    prior_positions = (
        positions.select(
            pl.col("date").alias("position_date"), "symbol", "market_value"
        )
        .join(next_dates, on="position_date", how="inner")
        .drop("position_date")
        if nav is not None
        else positions.select("date", "symbol", "market_value")
    )
    held = returns.join(
        prior_positions,
        on=["date", "symbol"],
        how="inner",
    ).filter(pl.col("return_difference").is_finite())
    daily = (
        held.group_by("date")
        .agg(
            (pl.col("return_difference") * pl.col("market_value")).sum().alias(
                "weighted_difference"
            ),
            (pl.col("return_difference").abs() * pl.col("market_value")).sum().alias(
                "weighted_absolute_difference"
            ),
            pl.col("market_value").sum().alias("holdings_value"),
            pl.len().alias("held_observations"),
        )
        .with_columns(
            (pl.col("weighted_difference") / pl.col("holdings_value")).alias(
                "portfolio_return_difference"
            ),
            (
                pl.col("weighted_absolute_difference") / pl.col("holdings_value")
            ).alias("portfolio_absolute_difference"),
        )
        .sort("date")
    )
    if nav is not None:
        prior_nav = (
            nav.select(pl.col("date").alias("position_date"), pl.col("nav").alias("prior_nav"))
            .join(next_dates, on="position_date", how="inner")
            .drop("position_date")
        )
        daily = daily.join(prior_nav, on="date", how="left").with_columns(
            (pl.col("weighted_difference") / pl.col("prior_nav")).alias(
                "portfolio_return_difference"
            ),
            (pl.col("weighted_absolute_difference") / pl.col("prior_nav")).alias(
                "portfolio_absolute_difference"
            ),
        )
    maximum = float(daily["portfolio_return_difference"].abs().max() or 0.0)
    maximum_absolute = float(daily["portfolio_absolute_difference"].max() or 0.0)
    breaches = daily.filter(
        (pl.col("portfolio_return_difference").abs() > threshold)
        | (pl.col("portfolio_absolute_difference") > threshold)
    )
    summary: dict[str, object] = {
        "status": "ready" if breaches.is_empty() else "blocked",
        "threshold": threshold,
        "maximum_daily_divergence": maximum,
        "maximum_daily_absolute_divergence": maximum_absolute,
        "breach_days": breaches.height,
        "observations": held.height,
        "method": "held-value-weighted raw economic return versus backward-adjusted return",
    }
    return summary, daily
