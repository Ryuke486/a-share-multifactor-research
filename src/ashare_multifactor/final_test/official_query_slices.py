"""Deterministic time partitions for persistently drifting official queries."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import replace
from datetime import date, timedelta

from ashare_multifactor.final_test.official_query_coverage import OfficialQueryScope


def split_query_scope(
    scope: OfficialQueryScope,
) -> tuple[OfficialQueryScope, ...]:
    """Split one scope through the approved two-year, year, quarter, month ladder."""
    months = _month_count(scope.start, scope.end)
    if months <= 1:
        return ()
    chunk_months = 24 if months > 24 else 12 if months > 12 else 3 if months > 3 else 1
    children: list[OfficialQueryScope] = []
    start = scope.start
    while start <= scope.end:
        next_start = _add_months(date(start.year, start.month, 1), chunk_months)
        end = min(scope.end, next_start - timedelta(days=1))
        children.append(replace(scope, start=start, end=end))
        start = end + timedelta(days=1)
    return tuple(children)


def _month_count(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + end.month - start.month + 1


def _add_months(value: date, months: int) -> date:
    ordinal = value.year * 12 + value.month - 1 + months
    year, month_index = divmod(ordinal, 12)
    month = month_index + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))
