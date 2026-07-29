"""Evaluation of verified historical candidate/PDF pairs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl

from ashare_multifactor.final_test import official_announcement_routing as routing
from ashare_multifactor.final_test.official_candidate_review_admission import (
    _STRONG_REASON_PREFIX,
    _date_in_text,
    _lag_interval,
)
from ashare_multifactor.final_test.official_candidate_review_admission_storage import (
    _UNRESOLVED_SCHEMA,
)


_DECISION_SCHEMA = {
    "pair_id": pl.String,
    "historical_candidate_id": pl.String,
    "symbol": pl.String,
    "ex_date": pl.Date,
    "catalog_id": pl.String,
    "status": pl.String,
    "route": pl.String,
    "candidate_type": pl.String,
    "routing_reason": pl.String,
    "pdf_sha256": pl.String,
    "lag_calendar_days": pl.Int64,
    "failure_codes": pl.List(pl.String),
}


def evaluate_historical_candidate_pairs(
    derivation: pl.DataFrame,
    *,
    discovery: pl.DataFrame,
    pdfs: dict[str, dict[str, object]],
    date_rule: dict[str, Any],
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Recompute routing, text-date, and lag-window admission per pair."""
    discovery_by_id = {
        str(row["catalog_id"]): row
        for row in discovery.iter_rows(named=True)
    }
    minimum_lag, maximum_lag = _lag_interval(date_rule)
    decisions: list[dict[str, object]] = []
    unresolved: list[dict[str, object]] = []
    for pair in derivation.sort("pair_id").iter_rows(named=True):
        catalog_id = str(pair["catalog_id"])
        announcement = discovery_by_id.get(catalog_id)
        if announcement is None:
            raise ValueError("historical candidate announcement is missing")
        _verify_pair_binding(pair, announcement)
        pdf = pdfs[str(pair["source_url"])]
        if (
            Path(str(pair["pdf_path"])).name
            != Path(str(pdf["pdf_path"])).name
            or pair["pdf_sha256"] != pdf["pdf_sha256"]
            or pair["pdf_size_bytes"] != pdf["pdf_size_bytes"]
            or pair["pdf_page_count"] != pdf["pdf_page_count"]
        ):
            raise ValueError("historical candidate PDF binding differs")
        routed = routing.route_announcement_title(
            str(pair["announcement_title"])
        )
        strong = (
            routed["route"] == "candidate"
            and routed["candidate_type"] == "corporate_action"
            and routed["reason"].startswith(_STRONG_REASON_PREFIX)
        )
        ex_date = pair["ex_date"]
        announcement_date = pair["announcement_date"]
        lag = (ex_date - announcement_date).days
        date_match = _date_in_text(ex_date, str(pdf["normalized_text"]))
        window_hit = minimum_lag <= lag <= maximum_lag
        admitted = strong and date_match and window_hit
        failure_codes = _failure_codes(
            strong=strong,
            date_match=date_match,
            window_hit=window_hit,
        )
        decisions.append(
            {
                "pair_id": str(pair["pair_id"]),
                "historical_candidate_id": str(
                    pair["historical_candidate_id"]
                ),
                "symbol": str(pair["symbol"]),
                "ex_date": ex_date,
                "catalog_id": catalog_id,
                "status": "admitted" if admitted else "unresolved",
                "route": routed["route"],
                "candidate_type": routed["candidate_type"],
                "routing_reason": routed["reason"],
                "pdf_sha256": str(pdf["pdf_sha256"]),
                "lag_calendar_days": lag,
                "failure_codes": failure_codes,
            }
        )
        if not admitted:
            unresolved.append(
                {
                    "candidate_id": str(pair["pair_id"]),
                    "symbol": str(pair["symbol"]),
                    "ex_date": ex_date,
                    "effective_date": None,
                    "failure_codes": failure_codes,
                    "same_symbol_announcement_count": 1,
                    "strong_implementation_announcement_count": int(strong),
                    "valid_cached_official_pdf_count": 1,
                    "frozen_date_window_hit_count": int(window_hit),
                    "related_catalog_ids": [catalog_id],
                }
            )
    decision_frame = pl.DataFrame(
        decisions,
        schema=_DECISION_SCHEMA,
        strict=True,
    ).sort("pair_id")
    unresolved_frame = pl.DataFrame(
        unresolved,
        schema=_UNRESOLVED_SCHEMA,
        strict=True,
    ).sort("candidate_id")
    return decision_frame, unresolved_frame


def _verify_pair_binding(
    pair: dict[str, object],
    announcement: dict[str, object],
) -> None:
    if (
        pair["symbol"] != announcement["symbol"]
        or pair["market"] != announcement["market"]
        or pair["announcement_id"] != announcement["announcement_id"]
        or pair["announcement_title"] != announcement["announcement_title"]
        or pair["source_url"] != announcement["source_url"]
        or pair["announcement_date"].isoformat()
        != announcement["announcement_date"]
        or pair["lag_calendar_days"]
        != (pair["ex_date"] - pair["announcement_date"]).days
    ):
        raise ValueError("historical candidate/announcement binding differs")


def _failure_codes(
    *,
    strong: bool,
    date_match: bool,
    window_hit: bool,
) -> list[str]:
    if not strong:
        primary = "no_strong_implementation_route"
    elif not date_match:
        primary = "candidate_date_not_found_in_pdf"
    elif not window_hit:
        primary = "outside_frozen_date_window"
    else:
        return []
    return [primary, "no_single_announcement_satisfies_all_requirements"]
