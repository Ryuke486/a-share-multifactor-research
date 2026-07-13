from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import shutil

import polars as pl
import pytest

from factor_pipeline_fixtures import write_prepared_daily_panel
from ashare_multifactor.factors.definitions import (
    FACTOR_DEFINITIONS,
    FactorDefinition,
)
import ashare_multifactor.research.factor_lineage as lineage_module
from ashare_multifactor.research.factor_outputs import (
    expected_stage_outputs,
    write_monthly_raw,
)
import ashare_multifactor.research.factor_pipeline as pipeline_module
from ashare_multifactor.research.factor_pipeline import run_factor_pipeline


class _InjectedStageCrash(BaseException):
    pass


def _read_lineage(factor_root: Path) -> dict[str, object]:
    return json.loads((factor_root / "lineage.json").read_text(encoding="utf-8"))


def _mutated_registry(field: str) -> tuple[FactorDefinition, ...]:
    first = FACTOR_DEFINITIONS[0]
    replacements = {
        "direction": {"direction": -first.direction},
        "lookback": {"lookback": first.lookback + 1},
        "source_columns": {"source_columns": ("pb",)},
        "requires_verified_pit": {"requires_verified_pit": not first.requires_verified_pit},
        "size_neutralize": {"size_neutralize": not first.size_neutralize},
    }
    return (replace(first, **replacements[field]), *FACTOR_DEFINITIONS[1:])


def test_audit_lineage_freezes_complete_factor_registry(tmp_path: Path) -> None:
    config_path, daily_root, _ = write_prepared_daily_panel(tmp_path)

    run_factor_pipeline(config_path, "audit")

    registry = _read_lineage(daily_root.parent)["factor_registry"]
    assert len(registry["sha256"]) == 64
    assert registry["payload"] == [
        {
            "direction": definition.direction,
            "family": definition.family,
            "lookback": definition.lookback,
            "name": definition.name,
            "requires_verified_pit": definition.requires_verified_pit,
            "size_neutralize": definition.size_neutralize,
            "source_columns": list(definition.source_columns),
        }
        for definition in FACTOR_DEFINITIONS
    ]


@pytest.mark.parametrize(
    "field",
    (
        "direction",
        "lookback",
        "source_columns",
        "requires_verified_pit",
        "size_neutralize",
    ),
)
def test_changed_factor_registry_rejects_frozen_lineage_before_data_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    config_path, _, _ = write_prepared_daily_panel(tmp_path)
    run_factor_pipeline(config_path, "audit")
    monkeypatch.setattr(pipeline_module, "FACTOR_DEFINITIONS", _mutated_registry(field))

    def forbidden_read(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("daily data was read before registry rejection")

    monkeypatch.setattr(pipeline_module, "_read_daily", forbidden_read)
    with pytest.raises(ValueError, match="factor registry does not match frozen lineage"):
        run_factor_pipeline(config_path, "factors")


def test_all_validates_daily_panel_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path, daily_root, _ = write_prepared_daily_panel(tmp_path)
    prepared = tmp_path / "prepared-daily-panel"
    shutil.move(daily_root, prepared)
    original_validate = pipeline_module._validate_source
    calls = 0

    def fake_build(*_args: object, **_kwargs: object) -> None:
        shutil.copytree(prepared, daily_root)

    def counted_validate(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "_run_build", fake_build)
    monkeypatch.setattr(pipeline_module, "_validate_source", counted_validate)

    run_factor_pipeline(config_path, "all")

    assert calls == 1


def test_daily_identity_reuses_validated_manifest_records_without_rehashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, daily_root, _ = write_prepared_daily_panel(tmp_path)
    factor_root = daily_root.parent
    original_sha256 = lineage_module._sha256
    daily_hashes: list[str] = []

    def tracked_sha256(path: Path) -> str:
        if path.is_relative_to(daily_root):
            daily_hashes.append(path.relative_to(factor_root).as_posix())
        return original_sha256(path)

    monkeypatch.setattr(lineage_module, "_sha256", tracked_sha256)

    run_factor_pipeline(config_path, "all")

    assert daily_hashes == []
    lineage = _read_lineage(factor_root)
    for stage in ("audit", "factors"):
        assert not any(
            record["path"].startswith("daily_panel/")
            for record in lineage["stages"][stage]["inputs"]
        )


def test_separate_stage_still_rejects_tampered_daily_partition(tmp_path: Path) -> None:
    config_path, daily_root, _ = write_prepared_daily_panel(tmp_path)
    run_factor_pipeline(config_path, "audit")
    partition = next(daily_root.rglob("*.parquet"))
    partition.write_bytes(partition.read_bytes() + b"tampered")

    with pytest.raises(ValueError, match="daily panel partition digest mismatch"):
        run_factor_pipeline(config_path, "factors")


@pytest.mark.parametrize(
    ("stage", "remaining_stages"),
    (("factors", {"audit"}), ("evaluate", {"audit", "factors"})),
)
def test_stage_crash_before_root_publish_leaves_no_mixed_live_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    remaining_stages: set[str],
) -> None:
    config_path, daily_root, _ = write_prepared_daily_panel(tmp_path)
    factor_root = daily_root.parent
    artifact_root = tmp_path / "artifacts/factor_research"
    run_factor_pipeline(config_path, "all")

    def crash_before_publish(staging: Path, live: Path) -> None:
        assert staging.parent == live.parent
        assert all(
            path.is_file()
            for path in expected_stage_outputs(staging, stage, tuple(FACTOR_DEFINITIONS))
        )
        staging_lineage = _read_lineage(staging)
        assert stage in staging_lineage["stages"]
        assert (staging / "daily_panel/manifest.json").stat().st_ino == (
            live / "daily_panel/manifest.json"
        ).stat().st_ino
        raise _InjectedStageCrash("after writes, before root publish")

    monkeypatch.setattr(
        pipeline_module,
        "_publish_stage_root",
        crash_before_publish,
        raising=False,
    )

    with pytest.raises(_InjectedStageCrash, match="before root publish"):
        run_factor_pipeline(config_path, stage)

    lineage = _read_lineage(factor_root)
    assert set(lineage["stages"]) == remaining_stages
    for invalid_stage in (
        ("factors", "evaluate", "report") if stage == "factors" else ("evaluate", "report")
    ):
        assert not any(
            path.exists()
            for path in expected_stage_outputs(
                factor_root, invalid_stage, tuple(FACTOR_DEFINITIONS)
            )
        )
    assert not artifact_root.exists()
    assert not list(factor_root.parent.glob(".factor_research-*.stage.tmp"))


def test_next_run_removes_only_controlled_orphan_stage_directories(tmp_path: Path) -> None:
    config_path, daily_root, _ = write_prepared_daily_panel(tmp_path)
    factor_root = daily_root.parent
    orphan = factor_root.parent / ".factor_research-deadbeef.stage.tmp"
    unrelated = factor_root.parent / ".factor_research-user.tmp"
    orphan.mkdir()
    unrelated.mkdir()
    (orphan / "partial.parquet").write_bytes(b"partial")

    run_factor_pipeline(config_path, "audit")

    assert not orphan.exists()
    assert unrelated.is_dir()


def test_empty_monthly_panel_writes_every_registered_family_with_stable_schema(
    tmp_path: Path,
) -> None:
    schema = {
        "date": pl.Date,
        "symbol": pl.String,
        "factor_name": pl.String,
        "family": pl.String,
        "raw_value": pl.Float64,
    }
    empty = pl.DataFrame(schema=schema)
    output = tmp_path / "monthly_raw"

    write_monthly_raw(output, empty, tuple(FACTOR_DEFINITIONS))

    families = tuple(dict.fromkeys(definition.family for definition in FACTOR_DEFINITIONS))
    for family in families:
        partition = output / f"family={family}/part-000.parquet"
        assert partition.is_file()
        frame = pl.read_parquet(partition)
        assert frame.schema == empty.schema
        assert frame.is_empty()
