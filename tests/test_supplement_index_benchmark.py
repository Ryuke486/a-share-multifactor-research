from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from ashare_multifactor.supplements.index_benchmark import (
    INDEX_CODES,
    collect_index_rows,
    index_levels,
    read_index_cache,
    write_index_cache,
)

FIELDS = ["date", "code", "close", "preclose"]


def _rows(code: str, closes: list[float], *, first_preclose: float = 100.0) -> list[list[str]]:
    days = ["2006-01-04", "2006-01-05", "2006-01-06"]
    previous = [first_preclose, *closes[:-1]]
    return [
        [day, code, f"{close:.4f}", f"{before:.4f}"]
        for day, close, before in zip(days, closes, previous, strict=True)
    ]


def _query(calls: list[tuple[str, str, str]]):
    def query(code: str, start: str, end: str) -> tuple[list[str], list[list[str]]]:
        calls.append((code, start, end))
        return FIELDS, _rows(code, [101.0, 99.0, 102.0])

    return query


def test_collection_issues_one_bounded_query_per_index_and_never_asks_past_2021() -> None:
    calls: list[tuple[str, str, str]] = []

    frame = collect_index_rows(_query(calls))

    assert [code for code, _, _ in calls] == list(INDEX_CODES.values())
    assert {(start, end) for _, start, end in calls} == {("2006-01-01", "2021-12-31")}
    assert frame.height == 3 * len(INDEX_CODES)
    assert frame.schema["date"] == pl.Date
    assert frame.get_column("date").max() <= date(2021, 12, 31)


def test_rows_after_the_validation_period_are_rejected() -> None:
    def query(code: str, start: str, end: str) -> tuple[list[str], list[list[str]]]:
        rows = _rows(code, [101.0, 99.0, 102.0])
        rows.append(["2022-01-04", code, "103.0000", "102.0000"])
        return FIELDS, rows

    with pytest.raises(ValueError, match="sealed"):
        collect_index_rows(query)


def test_broken_price_chains_duplicates_and_non_positive_levels_are_rejected() -> None:
    def broken(code: str, start: str, end: str) -> tuple[list[str], list[list[str]]]:
        rows = _rows(code, [101.0, 99.0, 102.0])
        rows[2][3] = "55.0000"
        return FIELDS, rows

    def duplicated(code: str, start: str, end: str) -> tuple[list[str], list[list[str]]]:
        rows = _rows(code, [101.0, 99.0, 102.0])
        return FIELDS, [*rows, rows[0]]

    def empty(code: str, start: str, end: str) -> tuple[list[str], list[list[str]]]:
        return FIELDS, []

    with pytest.raises(ValueError, match="previous close"):
        collect_index_rows(broken)
    with pytest.raises(ValueError, match="duplicate"):
        collect_index_rows(duplicated)
    with pytest.raises(ValueError, match="no rows"):
        collect_index_rows(empty)


def test_levels_start_from_the_previous_close_and_must_match_the_trading_calendar() -> None:
    frame = collect_index_rows(_query([]))
    calendar = [date(2005, 12, 30), date(2006, 1, 4), date(2006, 1, 5), date(2006, 1, 6)]

    levels = index_levels(frame, trading_dates=calendar)

    assert levels.get_column("date").to_list() == calendar
    assert levels.get_column("csi300").to_list() == [100.0, 101.0, 99.0, 102.0]
    assert levels.get_column("csi500").to_list() == [100.0, 101.0, 99.0, 102.0]
    with pytest.raises(ValueError, match="trading calendar"):
        index_levels(frame, trading_dates=[*calendar, date(2006, 1, 9)])
    with pytest.raises(ValueError, match="trading calendar"):
        index_levels(frame, trading_dates=[date(2006, 1, 4), date(2006, 1, 5), date(2006, 1, 6)])


def test_cache_round_trip_is_bound_by_hash_and_refuses_tampering(tmp_path: Path) -> None:
    frame = collect_index_rows(_query([]))
    output = tmp_path / "index_daily.parquet"

    write_index_cache(frame, output, fetched_at="2026-11-13T00:00:00+00:00", requests=6)

    restored, metadata = read_index_cache(output)
    assert restored.equals(frame)
    assert metadata["source"] == "baostock_query_history_k_data_plus"
    assert metadata["query_end"] == "2021-12-31"
    assert metadata["codes"] == INDEX_CODES

    sidecar = output.with_suffix(".metadata.json")
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["rows"] += 1
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="metadata"):
        read_index_cache(output)
    with pytest.raises(FileNotFoundError, match="download-index-benchmarks"):
        read_index_cache(tmp_path / "missing.parquet")
