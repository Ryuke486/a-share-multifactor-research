from __future__ import annotations


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
