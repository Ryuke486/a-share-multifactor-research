from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path


def _identity() -> dict[str, str]:
    return {
        "attempt_id": "attempt-001",
        "git_commit": "1" * 40,
        "git_tree": "2" * 40,
        "sealed_protocol_sha256": "3" * 64,
        "prepare_manifest_sha256": "4" * 64,
    }


def test_monitor_reports_green_yellow_and_red_without_mutating_heartbeat(
    tmp_path: Path,
) -> None:
    from ashare_multifactor.final_test.official_collection_monitor import (
        read_collection_status,
        write_collection_heartbeat,
    )

    heartbeat = tmp_path / "collection_heartbeat.json"
    now = datetime(2026, 7, 24, 6, 0, tzinfo=timezone.utc)
    write_collection_heartbeat(
        heartbeat,
        identity=_identity(),
        phase="collect_queries",
        completed_securities=1240,
        total_securities=5351,
        current_symbol="600009",
        current_period=("2022-01-01", "2023-12-31"),
        current_page=(4, 7),
        request_count=1734,
        split_count=7,
        last_progress_at=now,
        heartbeat_at=now,
        eta_seconds=18 * 60 * 60,
    )
    before = heartbeat.read_bytes()

    green = read_collection_status(
        heartbeat,
        expected_identity=_identity(),
        now=now + timedelta(minutes=1),
    )
    yellow = read_collection_status(
        heartbeat,
        expected_identity=_identity(),
        now=now + timedelta(minutes=6),
    )
    red = read_collection_status(
        heartbeat,
        expected_identity=_identity(),
        now=now + timedelta(minutes=11),
    )

    assert green.level == "green"
    assert green.completed_securities == 1240
    assert green.current_page == (4, 7)
    assert yellow.level == "yellow"
    assert yellow.reason == "no network progress for at least 5 minutes"
    assert red.level == "red"
    assert red.reason == "heartbeat is older than 10 minutes"
    assert heartbeat.read_bytes() == before
