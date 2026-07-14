import json
from pathlib import Path

import polars as pl

import pytest

from ashare_multifactor.research.formal_backtest_pipeline import (
    _load_stale_evidence,
    _readiness_markdown,
    _validate_source_coverage,
    run_formal_pipeline,
)


def test_free_source_coverage_requires_complete_cache_and_payment_dates(tmp_path: Path) -> None:
    source = tmp_path / "cninfo_dividend"
    source.mkdir()
    payment = source / "normalized_payment_dates.parquet"
    pl.DataFrame(
        {
            "symbol": ["000001"],
            "effective_date": [None],
            "cash_per_share": [0.1],
        }
    ).write_parquet(payment)
    (source / "batch_metadata.json").write_text(
        json.dumps({"cached_count": 1, "errors": {"000002": "failed"}}),
        encoding="utf-8",
    )
    (source / "normalized_payment_dates.metadata.json").write_text(
        json.dumps({"missing_count": 1, "sha256": "wrong"}),
        encoding="utf-8",
    )

    coverage, blockers = _validate_source_coverage(tmp_path, expected_symbols=2)

    assert coverage["cached_symbols"] == 1
    assert coverage["payment_dates_missing"] == 1
    assert len(blockers) == 4


def test_stale_evidence_rejects_cache_hash_mismatch(tmp_path: Path) -> None:
    config = tmp_path / "evidence.csv"
    config.write_text(
        "symbol,cache_file,sha256,source_url\n"
        "000001,evidence.pdf,wrong,https://static.cninfo.com.cn/evidence.pdf\n",
        encoding="utf-8",
    )
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "evidence.pdf").write_bytes(b"evidence")

    with pytest.raises(ValueError, match="stale evidence hash mismatch"):
        _load_stale_evidence(config, cache)


def test_readiness_report_is_generated_from_structured_audit() -> None:
    report = _readiness_markdown(
        {
            "status": "ready",
            "blockers": [],
            "target_rows": 14_400,
            "execution_rows": 4_629_321,
            "corporate_action_rows": 15_923,
            "source_coverage": {"cached_symbols": 2_008, "payment_dates_missing": 0},
        }
    )
    assert "**结论：** `ready`" in report
    assert "2,008" in report


def test_superseded_release_requires_audit_reason(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="supplied together"):
        run_formal_pipeline(
            tmp_path,
            publish=False,
            superseded_run_id="old_release",
        )
