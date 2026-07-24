"""Recall audit and stratified human-review queue for frozen title routing."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import re

import polars as pl

from ashare_multifactor.final_test.official_announcement_routing import (
    route_announcement_title,
)


_KNOWN_COLUMNS = {"case_id", "title", "expected_event_type"}
_CATALOG_COLUMNS = {
    "catalog_id",
    "announcement_title",
    "source_url",
    "symbol",
    "market",
}
_ROUTING_COLUMNS = {"catalog_id", "route", "candidate_type", "reason"}
_YEAR = re.compile(r"/((?:19|20)[0-9]{2})-[0-9]{2}-[0-9]{2}/")


@dataclass(frozen=True)
class RoutingAuditResult:
    """Known-case recall result plus an unreviewed exclusion sample."""

    report: dict[str, object]
    known_results: pl.DataFrame
    exclusion_queue: pl.DataFrame


def audit_routing_frames(
    catalog: pl.DataFrame,
    routing: pl.DataFrame,
    *,
    known_cases: pl.DataFrame,
    exclusion_sample_size: int,
) -> RoutingAuditResult:
    """Fail on known misses and create a deterministic human exclusion queue."""
    if (
        not isinstance(catalog, pl.DataFrame)
        or not _CATALOG_COLUMNS <= set(catalog.columns)
        or not isinstance(routing, pl.DataFrame)
        or not _ROUTING_COLUMNS <= set(routing.columns)
        or not isinstance(known_cases, pl.DataFrame)
        or set(known_cases.columns) != _KNOWN_COLUMNS
        or not isinstance(exclusion_sample_size, int)
        or isinstance(exclusion_sample_size, bool)
        or exclusion_sample_size <= 0
    ):
        raise ValueError("official routing audit input is invalid")
    if (
        catalog.get_column("catalog_id").n_unique() != catalog.height
        or routing.get_column("catalog_id").n_unique() != routing.height
        or set(catalog.get_column("catalog_id"))
        != set(routing.get_column("catalog_id"))
    ):
        raise ValueError("official routing audit catalog scope differs")

    known_rows = []
    for case in known_cases.sort("case_id").iter_rows(named=True):
        routed = route_announcement_title(case["title"])
        known_rows.append(
            {
                **case,
                "route": routed["route"],
                "candidate_type": routed["candidate_type"],
                "reason": routed["reason"],
                "captured": routed["route"] != "excluded",
            }
        )
    known_results = pl.DataFrame(known_rows).sort("case_id")
    misses = known_results.filter(~pl.col("captured"))
    if misses.height:
        raise ValueError("official routing audit has a known event excluded miss")

    excluded = (
        catalog.join(routing, on="catalog_id", how="inner")
        .filter(pl.col("route") == "excluded")
        .sort("catalog_id")
    )
    exclusion_queue = _stratified_exclusion_sample(
        excluded,
        sample_size=exclusion_sample_size,
    )
    known_count = known_results.height
    report = {
        "known_case_count": known_count,
        "known_captured_count": known_count,
        "known_miss_count": 0,
        "known_capture_rate": 1.0,
        "excluded_population_count": excluded.height,
        "requested_exclusion_sample_count": exclusion_sample_size,
        "exclusion_sample_count": exclusion_queue.height,
        "exclusion_sample_scope_complete": (
            exclusion_queue.height == exclusion_sample_size
        ),
        "exclusion_review_complete": False,
        "exclusion_confirmed_miss_count": None,
    }
    return RoutingAuditResult(
        report=report,
        known_results=known_results,
        exclusion_queue=exclusion_queue,
    )


def _stratified_exclusion_sample(
    excluded: pl.DataFrame,
    *,
    sample_size: int,
) -> pl.DataFrame:
    rows = []
    groups: dict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
    for row in excluded.iter_rows(named=True):
        groups[
            (
                str(row["market"]),
                _url_year(str(row["source_url"])),
                str(row["reason"]),
            )
        ].append(row)
    for values in groups.values():
        values.sort(key=lambda row: str(row["catalog_id"]))
    positions = {key: 0 for key in groups}
    keys = sorted(groups)
    target = min(sample_size, excluded.height)
    while len(rows) < target:
        advanced = False
        for key in keys:
            position = positions[key]
            if position >= len(groups[key]):
                continue
            row = groups[key][position]
            positions[key] += 1
            rows.append(
                {
                    **row,
                    "audit_year": key[1],
                    "review_status": "needs_human_review",
                    "review_label": "",
                    "review_note": "",
                }
            )
            advanced = True
            if len(rows) == target:
                break
        if not advanced:
            break
    if rows:
        return pl.DataFrame(rows).sort("catalog_id")
    return excluded.with_columns(
        pl.lit("").alias("audit_year"),
        pl.lit("needs_human_review").alias("review_status"),
        pl.lit("").alias("review_label"),
        pl.lit("").alias("review_note"),
    )


def _url_year(value: str) -> str:
    match = _YEAR.search(value)
    return match.group(1) if match is not None else "unknown"
