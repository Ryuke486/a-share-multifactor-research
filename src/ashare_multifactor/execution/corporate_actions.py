from __future__ import annotations

from datetime import date
import hashlib

import polars as pl


def _action_id(row: dict[str, object]) -> str:
    payload = "|".join(
        str(row.get(field, ""))
        for field in (
            "ex_date",
            "effective_date",
            "symbol",
            "cash_per_share",
            "share_ratio",
            "source",
        )
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def normalize_corporate_actions(frame: pl.DataFrame) -> pl.DataFrame:
    required = {
        "symbol",
        "effective_date",
        "cash_per_share",
        "share_ratio",
        "source",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"missing corporate action fields: {sorted(missing)}")
    ex_date = (
        pl.col("ex_date").cast(pl.Date)
        if "ex_date" in frame.columns
        else pl.col("effective_date").cast(pl.Date)
    )
    normalized = frame.select(
        ex_date.alias("ex_date"),
        pl.col("effective_date").cast(pl.Date),
        pl.col("symbol").cast(pl.String).str.zfill(6),
        pl.col("cash_per_share").fill_null(0.0).cast(pl.Float64),
        pl.col("share_ratio").fill_null(0.0).cast(pl.Float64),
        pl.col("source").cast(pl.String),
    )
    if normalized.filter(
        (pl.col("ex_date") < date(2005, 1, 1))
        | (pl.col("ex_date") > date(2016, 12, 31))
        | (pl.col("effective_date") < date(2005, 1, 1))
        | (pl.col("effective_date") > date(2016, 12, 31))
        | (pl.col("cash_per_share") < 0)
        | (pl.col("share_ratio") < -1)
    ).height:
        raise ValueError("invalid corporate action")
    rows = normalized.sort("effective_date", "symbol", "source").to_dicts()
    if not rows:
        return pl.DataFrame(
            schema={
                "action_id": pl.String,
                "ex_date": pl.Date,
                "effective_date": pl.Date,
                "symbol": pl.String,
                "cash_per_share": pl.Float64,
                "share_ratio": pl.Float64,
                "source": pl.String,
            }
        )
    return pl.DataFrame(
        [{"action_id": _action_id(row), **row} for row in rows]
    ).select(
        "action_id",
        "ex_date",
        "effective_date",
        "symbol",
        "cash_per_share",
        "share_ratio",
        "source",
    )


def apply_corporate_actions(
    positions: dict[str, int],
    cash: float,
    actions: pl.DataFrame,
    effective_date: date,
    *,
    applied_ids: set[str],
) -> tuple[dict[str, int], float]:
    updated = positions.copy()
    for row in actions.filter(pl.col("effective_date") == effective_date).to_dicts():
        action_id = row["action_id"]
        if action_id in applied_ids:
            raise ValueError(f"corporate action already applied: {action_id}")
        shares = updated.get(row["symbol"], 0)
        cash += shares * row["cash_per_share"]
        updated[row["symbol"]] = round(shares * (1 + row["share_ratio"]))
        applied_ids.add(action_id)
    return updated, cash
