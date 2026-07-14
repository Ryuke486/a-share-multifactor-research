from __future__ import annotations

import polars as pl


def audit_stale_positions(
    positions: pl.DataFrame,
    nav: pl.DataFrame,
    execution_panel: pl.DataFrame,
    security_events: pl.DataFrame,
    public_evidence: pl.DataFrame,
    *,
    review_days: int,
) -> tuple[dict[str, object], pl.DataFrame]:
    if review_days <= 0:
        raise ValueError("review_days must be positive")
    reviewed = (
        positions.filter(pl.col("is_stale") & (pl.col("stale_days") > review_days))
        .join(nav.select("date", "nav"), on="date", how="left")
        .with_columns((pl.col("market_value") / pl.col("nav")).alias("asset_weight"))
    )
    schema = {
        "symbol": pl.String,
        "first_stale_date": pl.Date,
        "last_stale_date": pl.Date,
        "maximum_stale_days": pl.Int64,
        "maximum_asset_weight": pl.Float64,
        "classification": pl.String,
    }
    if reviewed.is_empty():
        return {
            "status": "ready",
            "review_days": review_days,
            "reviewed_symbols": 0,
            "unexplained_symbols": 0,
        }, pl.DataFrame(schema=schema)
    intervals = reviewed.group_by("symbol").agg(
        pl.col("date").min().alias("first_stale_date"),
        pl.col("date").max().alias("last_stale_date"),
        pl.col("stale_days").max().alias("maximum_stale_days"),
        pl.col("asset_weight").max().alias("maximum_asset_weight"),
    )
    resumed = (
        execution_panel.filter(
            pl.col("open_raw").is_not_null()
            & pl.col("open_raw").is_finite()
            & (pl.col("open_raw") > 0)
        )
        .select("symbol", pl.col("date").alias("resume_date"))
        .join(intervals.select("symbol", "last_stale_date"), on="symbol", how="inner")
        .filter(pl.col("resume_date") > pl.col("last_stale_date"))
        .select("symbol")
        .unique()
    )
    event_symbols = (
        security_events.select("source_symbol").unique().rename({"source_symbol": "symbol"})
        if not security_events.is_empty()
        else pl.DataFrame(schema={"symbol": pl.String})
    )
    evidence_symbols = (
        public_evidence.select("symbol").unique()
        if not public_evidence.is_empty()
        else pl.DataFrame(schema={"symbol": pl.String})
    )
    intervals = (
        intervals.join(resumed.with_columns(pl.lit(True).alias("resumed")), on="symbol", how="left")
        .join(
            event_symbols.with_columns(pl.lit(True).alias("has_security_event")),
            on="symbol",
            how="left",
        )
        .join(
            evidence_symbols.with_columns(pl.lit(True).alias("has_public_evidence")),
            on="symbol",
            how="left",
        )
        .with_columns(
            pl.when(pl.col("resumed").fill_null(False))
            .then(pl.lit("explained_resumption"))
            .when(pl.col("has_security_event").fill_null(False))
            .then(pl.lit("explained_security_event"))
            .when(pl.col("has_public_evidence").fill_null(False))
            .then(pl.lit("explained_public_evidence"))
            .otherwise(pl.lit("unexplained"))
            .alias("classification")
        )
        .select(*schema)
        .sort(
            ["maximum_stale_days", "symbol"],
            descending=[True, False],
        )
    )
    unexplained = intervals.filter(pl.col("classification") == "unexplained").height
    return {
        "status": "ready" if unexplained == 0 else "blocked",
        "review_days": review_days,
        "reviewed_symbols": intervals.height,
        "unexplained_symbols": unexplained,
        "maximum_stale_days": int(intervals["maximum_stale_days"].max()),
        "maximum_asset_weight": float(intervals["maximum_asset_weight"].max()),
    }, intervals
