import polars as pl


def build_execution_panel(frame: pl.DataFrame, *, adv_lookback: int = 20) -> pl.DataFrame:
    if adv_lookback <= 0:
        raise ValueError("adv_lookback must be positive")
    required = {
        "date",
        "symbol",
        "open_raw",
        "close_raw",
        "prev_close_raw",
        "amount",
        "is_st",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"missing execution fields: {sorted(missing)}")
    if frame.select(pl.struct("date", "symbol").is_duplicated().any()).item():
        raise ValueError("duplicate execution panel key")
    market_dates = frame.select("date").unique().sort("date")
    symbols = frame.select("symbol").unique().sort("symbol")
    expanded = (
        symbols.join(market_dates, how="cross")
        .join(
            frame.with_columns(pl.lit(True).alias("_observed")),
            on=["symbol", "date"],
            how="left",
        )
        .sort("symbol", "date")
    )
    return (
        expanded
        .with_columns(
            pl.col("amount")
            .shift(1)
            .rolling_mean(window_size=adv_lookback, min_samples=1)
            .over("symbol")
            .alias("adv20"),
            pl.when(pl.col("is_st")).then(0.05).otherwise(0.10).alias("limit_rate"),
            (
                pl.col("open_raw").is_null()
                | ~pl.col("open_raw").is_finite()
                | (pl.col("open_raw") <= 0)
            ).alias("is_suspended_proxy"),
        )
        .filter(pl.col("_observed").fill_null(False))
        .drop("_observed")
        .sort("date", "symbol")
    )
