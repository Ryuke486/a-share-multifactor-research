from __future__ import annotations

import polars as pl


def market_for_symbol(symbol: str) -> str:
    """Return the exchange market for a normalized six-digit mainland symbol."""
    normalized = str(symbol)
    if len(normalized) != 6 or not normalized.isdigit():
        raise ValueError(f"invalid mainland security symbol: {symbol}")
    if normalized.startswith("92") or normalized[0] in {"4", "8"}:
        return "bj"
    if normalized[0] in {"5", "6", "9"}:
        return "sh"
    if normalized[0] in {"0", "1", "2", "3"}:
        return "sz"
    raise ValueError(f"unsupported mainland security symbol: {symbol}")


def filter_supported_markets(
    frame: pl.DataFrame,
    supported_markets: tuple[str, ...],
    *,
    symbol_column: str = "symbol",
) -> pl.DataFrame:
    return frame.filter(
        pl.col(symbol_column)
        .cast(pl.String)
        .str.zfill(6)
        .map_elements(market_for_symbol, return_dtype=pl.String)
        .is_in(supported_markets)
    )
