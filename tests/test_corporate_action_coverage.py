from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path

import polars as pl
import pytest

from ashare_multifactor.audit.records import file_record
from ashare_multifactor.final_test.execution_sources import (
    validate_corporate_action_coverage,
)
from ashare_multifactor.final_test.official_query_coverage import OfficialQueryScope
from test_final_test_official_query_index import _write_index


def test_corporate_action_coverage_requires_existing_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="coverage root"):
        validate_corporate_action_coverage(tmp_path / "missing")


def test_corporate_action_coverage_requires_every_target_symbol(tmp_path: Path) -> None:
    root = _write_coverage(tmp_path, symbols=("000001",))
    with pytest.raises(ValueError, match="symbol scope|not exact"):
        validate_corporate_action_coverage(root, symbols=["000001", "600000"])


def test_corporate_action_validator_hashes_the_single_manifest_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _write_coverage(tmp_path)
    manifest = root / "coverage.json"
    disk_bytes = manifest.read_bytes()
    captured_payload = json.loads(disk_bytes)
    captured_payload["capture_marker"] = "B"
    captured_bytes = json.dumps(captured_payload, sort_keys=True).encode()
    original_read_bytes = Path.read_bytes
    manifest_reads = 0

    def capture_once(path: Path) -> bytes:
        nonlocal manifest_reads
        if path == manifest:
            manifest_reads += 1
            return captured_bytes
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", capture_once)

    verified = validate_corporate_action_coverage(
        root, symbols=["000001", "600000"]
    )

    assert manifest_reads == 1
    assert verified["coverage_manifest_sha256"] == hashlib.sha256(
        captured_bytes
    ).hexdigest()
    assert original_read_bytes(manifest) == disk_bytes


def test_zero_event_symbol_still_requires_successful_official_query(tmp_path: Path) -> None:
    root = _write_coverage(tmp_path)
    coverage = pl.read_parquet(root / "query_coverage.parquet").with_columns(
        pl.when(pl.col("symbol") == "600000")
        .then(pl.lit("failed"))
        .otherwise(pl.col("status"))
        .alias("status")
    )
    coverage.write_parquet(root / "query_coverage.parquet")
    _refresh_record(root, "query_coverage")
    with pytest.raises(ValueError, match="query coverage is invalid"):
        validate_corporate_action_coverage(root, symbols=["000001", "600000"])


def test_zero_event_symbol_requires_hash_bound_cninfo_query_package(
    tmp_path: Path,
) -> None:
    root = _write_coverage(tmp_path)
    payload = json.loads((root / "coverage.json").read_text(encoding="utf-8"))
    payload.pop("official_query_coverage")
    (root / "coverage.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="query.*coverage"):
        validate_corporate_action_coverage(root, symbols=["000001", "600000"])


def test_zero_event_symbol_does_not_require_fake_document_evidence(
    tmp_path: Path,
) -> None:
    root = _write_coverage(tmp_path)
    coverage = pl.read_parquet(root / "query_coverage.parquet").drop("evidence_id")
    coverage.write_parquet(root / "query_coverage.parquet")
    payload = json.loads((root / "coverage.json").read_text(encoding="utf-8"))
    payload["schema_version"] = "2"
    (root / "coverage.json").write_text(json.dumps(payload), encoding="utf-8")
    _refresh_record(root, "query_coverage")

    verified = validate_corporate_action_coverage(
        root,
        symbols=["000001", "600000"],
    )

    assert (
        verified["coverage"]
        .filter(pl.col("symbol") == "600000")
        .get_column("event_count")
        .item()
        == 0
    )


def test_baostock_candidate_requires_official_evidence(tmp_path: Path) -> None:
    root = _write_coverage(tmp_path)
    diff = pl.read_parquet(root / "candidate_diff.parquet").with_columns(
        pl.lit("missing").alias("evidence_id")
    )
    diff.write_parquet(root / "candidate_diff.parquet")
    _refresh_record(root, "candidate_diff")
    with pytest.raises(ValueError, match="candidate lacks official evidence"):
        validate_corporate_action_coverage(root)


def test_corporate_action_evidence_cache_hash_is_immutable(tmp_path: Path) -> None:
    root = _write_coverage(tmp_path)
    (root / "szse.pdf").write_bytes(b"changed")
    with pytest.raises(ValueError, match="evidence hash changed"):
        validate_corporate_action_coverage(root)


def test_candidate_difference_must_be_explained(tmp_path: Path) -> None:
    root = _write_coverage(tmp_path)
    diff = pl.read_parquet(root / "candidate_diff.parquet").with_columns(
        pl.lit("").alias("explanation")
    )
    diff.write_parquet(root / "candidate_diff.parquet")
    _refresh_record(root, "candidate_diff")
    with pytest.raises(ValueError, match="difference is unexplained"):
        validate_corporate_action_coverage(root)


def test_corrected_candidate_requires_old_new_dates_and_reason(tmp_path: Path) -> None:
    root = _write_coverage(tmp_path)
    diff = pl.read_parquet(root / "candidate_diff.parquet").with_columns(
        pl.lit("corrected").alias("status"),
        pl.lit(None, dtype=pl.Date).alias("corrected_ex_date"),
    )
    diff.write_parquet(root / "candidate_diff.parquet")
    _refresh_record(root, "candidate_diff")
    with pytest.raises(ValueError, match="date reconciliation"):
        validate_corporate_action_coverage(root)


def test_normalized_actions_come_only_from_official_rows(tmp_path: Path) -> None:
    root = _write_coverage(tmp_path)
    verified = validate_corporate_action_coverage(
        root, symbols=["000001", "600000"]
    )
    actions = verified["actions"]
    assert actions.get_column("source").to_list() == ["szse"]
    assert actions.get_column("cash_per_share").to_list() == [0.2]


def test_preperiod_announcement_is_valid_for_final_period_action(tmp_path: Path) -> None:
    root = _write_coverage(tmp_path)
    official = pl.read_parquet(root / "official_actions.parquet").with_columns(
        pl.lit(date(2021, 12, 20)).alias("announcement_date")
    )
    official.write_parquet(root / "official_actions.parquet")
    _refresh_record(root, "official_actions")
    assert validate_corporate_action_coverage(root)["actions"].height == 1


def test_announcement_after_effective_date_is_rejected(tmp_path: Path) -> None:
    root = _write_coverage(tmp_path)
    official = pl.read_parquet(root / "official_actions.parquet").with_columns(
        pl.lit(date(2023, 6, 6)).alias("announcement_date")
    )
    official.write_parquet(root / "official_actions.parquet")
    _refresh_record(root, "official_actions")
    with pytest.raises(ValueError, match="lacks official evidence"):
        validate_corporate_action_coverage(root)


def test_missing_announcement_date_is_rejected(tmp_path: Path) -> None:
    root = _write_coverage(tmp_path)
    official = pl.read_parquet(root / "official_actions.parquet").with_columns(
        pl.lit(None, dtype=pl.Date).alias("announcement_date")
    )
    official.write_parquet(root / "official_actions.parquet")
    _refresh_record(root, "official_actions")
    with pytest.raises(ValueError, match="lacks official evidence"):
        validate_corporate_action_coverage(root)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ex_date", None),
        ("effective_date", None),
        ("cash_per_share", None),
        ("cash_per_share", float("nan")),
        ("share_ratio", float("inf")),
    ],
)
def test_corporate_action_execution_contract_rejects_null_or_nonfinite_values(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    root = _write_coverage(tmp_path)
    dtype = pl.Date if field in {"ex_date", "effective_date"} else pl.Float64
    official = pl.read_parquet(root / "official_actions.parquet").with_columns(
        pl.lit(value, dtype=dtype).alias(field)
    )
    official.write_parquet(root / "official_actions.parquet")
    candidates = pl.read_parquet(root / "candidates.parquet").with_columns(
        pl.lit(value, dtype=dtype).alias(field)
    )
    candidates.write_parquet(root / "candidates.parquet")
    _refresh_record(root, "official_actions")
    _refresh_record(root, "candidate_file")

    with pytest.raises(ValueError, match="corporate-action.*contract"):
        validate_corporate_action_coverage(root)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("symbol", "000002", "row lacks official evidence"),
        ("ex_date", date(2023, 6, 2), "ex_date mismatch"),
        ("effective_date", date(2023, 6, 6), "effective_date mismatch"),
        ("cash_per_share", 0.3, "cash_per_share mismatch"),
        ("share_ratio", 0.1, "share_ratio mismatch"),
    ],
)
def test_accepted_candidate_requires_exact_official_values(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    root = _write_coverage(tmp_path)
    official = pl.read_parquet(root / "official_actions.parquet").with_columns(
        pl.lit(value).alias(field)
    )
    official.write_parquet(root / "official_actions.parquet")
    _refresh_record(root, "official_actions")
    with pytest.raises(ValueError, match=message):
        validate_corporate_action_coverage(root)


def test_candidate_cannot_map_to_multiple_official_rows(tmp_path: Path) -> None:
    root = _write_coverage(tmp_path)
    official = pl.read_parquet(root / "official_actions.parquet")
    duplicate = official.with_columns(pl.lit(date(2023, 6, 6)).alias("effective_date"))
    pl.concat((official, duplicate)).write_parquet(root / "official_actions.parquet")
    coverage = pl.read_parquet(root / "query_coverage.parquet").with_columns(
        pl.when(pl.col("symbol") == "000001").then(2).otherwise(pl.col("event_count"))
        .alias("event_count")
    )
    coverage.write_parquet(root / "query_coverage.parquet")
    _refresh_record(root, "official_actions")
    _refresh_record(root, "query_coverage")
    with pytest.raises(ValueError, match="multiple official"):
        validate_corporate_action_coverage(root)


def _write_coverage(
    root: Path, *, symbols: tuple[str, ...] = ("000001", "600000")
) -> Path:
    root.mkdir(exist_ok=True)
    evidence_rows = []
    for market, source in (("sz", "szse"), ("sh", "sse")):
        cached = root / f"{source}.pdf"
        cached.write_bytes(source.encode())
        evidence_rows.append(
            {
                "evidence_id": f"ev-{market}",
                "source": source,
                "market": market,
                "source_url": (
                    "https://disc.static.szse.cn/download/disc/action.pdf"
                    if market == "sz"
                    else "https://www.sse.com.cn/disclosure/listedinfo/announcement/action.pdf"
                ),
                "cache_file": cached.name,
                "sha256": file_record(cached, root=root, role="cache").sha256,
            }
        )
    pl.DataFrame(evidence_rows).write_parquet(root / "evidence_index.parquet")
    coverage_rows = []
    for symbol in symbols:
        market = "sz" if symbol.startswith(("0", "3")) else "sh"
        source = "szse" if market == "sz" else "sse"
        coverage_rows.append(
            {
                "symbol": symbol,
                "source": source,
                "market": market,
                "query_start": date(2022, 1, 1),
                "query_end": date(2025, 12, 31),
                "status": "ok",
                "event_count": 1 if symbol == "000001" else 0,
                "evidence_id": f"ev-{market}",
            }
        )
    pl.DataFrame(coverage_rows).write_parquet(root / "query_coverage.parquet")
    pl.DataFrame(
        {
            "candidate_id": ["candidate-1"], "symbol": ["000001"],
            "ex_date": [date(2023, 6, 1)], "effective_date": [date(2023, 6, 5)],
            "cash_per_share": [0.2], "share_ratio": [0.0],
        }
    ).write_parquet(root / "candidates.parquet")
    pl.DataFrame(
        {
            "candidate_id": ["candidate-1"],
            "status": ["accepted"],
            "evidence_id": ["ev-sz"],
            "explanation": ["official announcement confirms candidate"],
            "original_ex_date": [date(2023, 6, 1)],
            "corrected_ex_date": [date(2023, 6, 1)],
            "correction_reason": [None],
            "original_symbol": ["000001"], "corrected_symbol": [None],
            "original_effective_date": [date(2023, 6, 5)],
            "corrected_effective_date": [None],
            "original_cash_per_share": [0.2], "corrected_cash_per_share": [None],
            "original_share_ratio": [0.0], "corrected_share_ratio": [None],
        }
    ).write_parquet(root / "candidate_diff.parquet")
    pl.DataFrame(
        {
            "symbol": ["000001"],
            "announcement_date": [date(2023, 5, 1)],
            "ex_date": [date(2023, 6, 1)],
            "effective_date": [date(2023, 6, 5)],
            "cash_per_share": [0.2],
            "share_ratio": [0.0],
            "market": ["sz"],
            "source": ["szse"],
            "evidence_id": ["ev-sz"],
            "candidate_id": ["candidate-1"],
        }
    ).write_parquet(root / "official_actions.parquet")
    query_index = _write_index(
        root,
        [
            OfficialQueryScope(
                symbol=symbol,
                market="sz" if symbol.startswith(("0", "3")) else "sh",
                category="corporate_actions",
                query_category="",
                start=date(2022, 1, 1),
                end=date(2025, 12, 31),
            )
            for symbol in sorted(symbols)
        ],
    )
    roles = {
        "candidate_file": ("candidates.parquet", "baostock_corporate_action_candidates"),
        "query_coverage": ("query_coverage.parquet", "official_corporate_action_coverage"),
        "evidence_index": (
            "evidence_index.parquet",
            "official_corporate_action_evidence_index",
        ),
        "official_actions": ("official_actions.parquet", "official_corporate_actions"),
        "candidate_diff": ("candidate_diff.parquet", "corporate_action_candidate_diff"),
        "official_query_coverage": (
            query_index.name,
            "official_query_coverage",
        ),
    }
    normalized = sorted(symbols)
    payload: dict[str, object] = {
        "status": "ready",
        "period": ["2022-01-01", "2025-12-31"],
        "scope": "all_final_execution_symbols",
        "symbol_count": len(normalized),
        "symbols_sha256": hashlib.sha256(
            ("\n".join(normalized) + "\n").encode()
        ).hexdigest(),
    }
    for field, (name, role) in roles.items():
        payload[field] = file_record(root / name, root=root, role=role).to_dict()
    (root / "coverage.json").write_text(json.dumps(payload), encoding="utf-8")
    return root


def _refresh_record(root: Path, field: str) -> None:
    payload = json.loads((root / "coverage.json").read_text(encoding="utf-8"))
    record = payload[field]
    payload[field] = file_record(
        root / record["path"], root=root, role=record["role"]
    ).to_dict()
    (root / "coverage.json").write_text(json.dumps(payload), encoding="utf-8")
