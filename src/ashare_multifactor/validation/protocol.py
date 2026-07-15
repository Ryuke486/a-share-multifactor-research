from __future__ import annotations

from collections.abc import Callable
from datetime import date
import json
from pathlib import Path


EARLIEST_ALLOWED_DATE = date(2003, 1, 1)
FINAL_TEST_START = date(2022, 1, 1)
APPROVED_STAGE_SIX_RELEASE = "b995878_stage6_authoritative"


def assert_validation_read_allowed(
    start: date,
    end: date,
    *,
    before_read: Callable[[], object] | None = None,
) -> None:
    """Reject invalid or final-test reads before invoking an optional scanner."""
    if end < start:
        raise ValueError("read range end precedes start")
    if start < EARLIEST_ALLOWED_DATE:
        raise ValueError("read range precedes validation warmup")
    if end >= FINAL_TEST_START:
        raise ValueError("final test period is sealed")
    if before_read is not None:
        before_read()


def assert_stage_six_release_allowed(formal_backtest_root: Path) -> None:
    """Require the audited stage-six release or an explicitly approved successor."""
    pointer = json.loads(
        (formal_backtest_root / "CURRENT.json").read_text(encoding="utf-8")
    )
    run_id = pointer.get("run_id")
    if run_id == APPROVED_STAGE_SIX_RELEASE:
        return
    lineage_path = formal_backtest_root / "releases" / str(run_id) / "lineage.json"
    if lineage_path.is_file():
        lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
        if (
            lineage.get("validation_approved") is True
            and lineage.get("supersedes", {}).get("run_id")
            == APPROVED_STAGE_SIX_RELEASE
        ):
            return
    raise ValueError(f"unapproved stage six release: {run_id}")
