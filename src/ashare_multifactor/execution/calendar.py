from datetime import date

import polars as pl


def next_trading_dates(
    market_dates: list[date],
    signal_dates: list[date],
    *,
    research_end: date,
) -> pl.DataFrame:
    if any(value > research_end for value in signal_dates):
        raise ValueError("signal outside research period")
    calendar = sorted({value for value in market_dates if value <= research_end})
    rows: list[dict[str, date | None]] = []
    for signal_date in signal_dates:
        execution_date = next((value for value in calendar if value > signal_date), None)
        rows.append({"signal_date": signal_date, "execution_date": execution_date})
    return pl.DataFrame(rows, schema={"signal_date": pl.Date, "execution_date": pl.Date})

