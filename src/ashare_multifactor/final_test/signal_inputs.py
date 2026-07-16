from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import math
from pathlib import Path

import polars as pl

from ashare_multifactor.audit.publication import PublishedRelease, resolve_current
from ashare_multifactor.audit.records import FileRecord, verify_file_record
from ashare_multifactor.config import ResearchConfig
from ashare_multifactor.data.field_audit import FieldReadiness
from ashare_multifactor.factors.definitions import FACTOR_DEFINITIONS
from ashare_multifactor.final_test.gate import FinalTestAuthorization


MAIN_CANDIDATE = "rolling_ic_family_size_stratified_buffered"
FINAL_HISTORY_DATE = date(2021, 12, 31)


@dataclass(frozen=True)
class FinalTestSignalInputs:
    historical_daily: pl.DataFrame
    readiness: FieldReadiness
    classifications: pl.DataFrame
    historical_rank_ic: pl.DataFrame
    historical_forward_returns: pl.DataFrame
    previous_targets: pl.DataFrame


def resolve_final_test_signal_inputs(
    config: ResearchConfig,
    authorization: FinalTestAuthorization,
    *,
    data_root: Path,
) -> FinalTestSignalInputs:
    """Resolve every pre-test signal input through frozen release identities."""
    factor = config.factor_research
    if factor is None:
        raise ValueError("factor research settings are missing")
    data_root = data_root.resolve()
    robustness = resolve_current(data_root / "processed/robustness")
    if robustness.run_id != authorization.robustness_release:
        raise ValueError("current robustness release differs from authorization")
    sealed = _read_json(robustness.artifacts / "sealed_test_protocol.json")
    if sealed.get("sealed_protocol_sha256") != authorization.sealed_protocol_sha256:
        raise ValueError("current sealed protocol differs from authorization")
    if sealed.get("main_candidate") != MAIN_CANDIDATE:
        raise ValueError("sealed Stage-8 main candidate changed")

    validation = resolve_current(data_root / "processed/validation_evaluation")
    upstream = sealed.get("upstream_validation")
    if not isinstance(upstream, dict) or (
        upstream.get("run_id") != validation.run_id
        or upstream.get("manifest_sha256") != validation.manifest_sha256
    ):
        raise ValueError("validation release differs from sealed Stage-8 validation identity")
    lineage = _read_json(validation.lineage)
    inputs = _reproducibility_inputs(lineage)

    validation_rank_ic = pl.read_parquet(
        _release_file(validation, "artifacts/research/factor_rank_ic.parquet")
    )
    validation_returns = pl.read_parquet(
        _release_file(validation, "datasets/inputs/forward_returns.parquet")
    )
    _assert_pretest(validation_rank_ic, "packaged validation Rank IC")
    _assert_pretest(validation_returns, "packaged validation forward returns")
    previous_targets = pl.read_parquet(
        _release_file(
            validation,
            f"datasets/inputs/target_weights_{MAIN_CANDIDATE}.parquet",
        )
    )
    _validate_previous_targets(previous_targets)

    factor_root = data_root / "processed/factor_research"
    stage_four = _required_records(inputs, "stage_four_files")
    classifications = pl.read_parquet(
        _selected_stage_four_file(stage_four, factor_root, "factor_classifications.parquet")
    )
    historical_rank_ic = pl.read_parquet(
        _selected_stage_four_file(stage_four, factor_root, "rank_ic.parquet")
    )
    historical_returns = pl.read_parquet(
        _selected_stage_four_file(stage_four, factor_root, "forward_returns.parquet")
    )
    readiness_path = _selected_stage_four_file(
        stage_four, factor_root, "data_readiness.json"
    )
    readiness = FieldReadiness(**_read_json(readiness_path))

    required_year_count = max(2, math.ceil(factor.minimum_history / 252) + 1)
    required_years = tuple(
        range(FINAL_HISTORY_DATE.year - required_year_count + 1, FINAL_HISTORY_DATE.year + 1)
    )
    daily = _load_validation_daily_history(
        _required_records(inputs, "validation_daily_panel_files"),
        data_root,
        required_years,
        minimum_observations=max(
            factor.minimum_history,
            max(definition.lookback for definition in FACTOR_DEFINITIONS),
        ),
    )
    return FinalTestSignalInputs(
        historical_daily=daily,
        readiness=readiness,
        classifications=classifications,
        historical_rank_ic=pl.concat(
            (historical_rank_ic, validation_rank_ic), how="diagonal_relaxed"
        ).sort("date", "factor_name"),
        historical_forward_returns=pl.concat(
            (historical_returns, validation_returns), how="diagonal_relaxed"
        ).sort("date", "symbol"),
        previous_targets=previous_targets,
    )


def _release_file(release: PublishedRelease, relative: str) -> Path:
    manifest = _read_json(release.manifest)
    records = [
        item
        for item in manifest.get("files", [])
        if isinstance(item, dict) and item.get("path") == relative
    ]
    if len(records) != 1:
        raise ValueError(f"validation release lacks exact manifest identity: {relative}")
    return verify_file_record(records[0], root=release.root)


def _reproducibility_inputs(lineage: dict[str, object]) -> dict[str, object]:
    reproducibility = lineage.get("reproducibility")
    inputs = reproducibility.get("inputs") if isinstance(reproducibility, dict) else None
    if not isinstance(inputs, dict):
        raise ValueError("validation lineage lacks reproducibility inputs")
    return inputs


def _required_records(
    inputs: dict[str, object], key: str
) -> list[dict[str, object]]:
    records = inputs.get(key)
    if not isinstance(records, list) or not records or any(
        not isinstance(item, dict) for item in records
    ):
        raise ValueError(f"validation lineage lacks {key}")
    return records


def _selected_stage_four_file(
    records: list[dict[str, object]], root: Path, name: str
) -> Path:
    selected = [record for record in records if record.get("path") == name]
    if len(selected) != 1:
        raise ValueError(f"stage four lineage must bind exactly one {name}")
    return _verify_lineage_record(selected[0], root, "stage_four")


def _load_validation_daily_history(
    records: list[dict[str, object]],
    data_root: Path,
    required_years: tuple[int, ...],
    *,
    minimum_observations: int,
) -> pl.DataFrame:
    verified = [
        _verify_lineage_record(record, data_root, "validation_daily")
        for record in records
    ]
    by_year: dict[int, Path] = {}
    for path in verified:
        if path.suffix != ".parquet" or not path.parent.name.startswith("year="):
            continue
        year = int(path.parent.name.removeprefix("year="))
        if year in required_years:
            expected = (
                f"processed/validation_evaluation/daily_panel/year={year}/"
                "part-000.parquet"
            )
            if path.relative_to(data_root).as_posix() != expected:
                raise ValueError("validation daily history path is not canonical")
            if year in by_year:
                raise ValueError("validation daily history contains duplicate years")
            by_year[year] = path
    if tuple(sorted(by_year)) != required_years:
        years = f"{required_years[0]}-{required_years[-1]}"
        raise ValueError(f"validation daily history must be continuous {years}")
    daily = pl.concat((pl.read_parquet(by_year[year]) for year in required_years)).sort(
        "date", "symbol"
    )
    if (
        daily.is_empty()
        or daily.get_column("date").min().year != required_years[0]
        or daily.get_column("date").max() != FINAL_HISTORY_DATE
    ):
        raise ValueError("validation daily history fails minimum/end-date continuity")
    trading_dates = daily.get_column("date").n_unique()
    if trading_dates < minimum_observations:
        raise ValueError(
            "validation daily history requires at least "
            f"{minimum_observations} distinct trading dates"
        )
    return daily


def _verify_lineage_record(
    record: dict[str, object], root: Path, default_role: str
) -> Path:
    size = record.get("size_bytes", record.get("size"))
    normalized = FileRecord(
        path=str(record.get("path", "")),
        role=str(record.get("role", default_role)),
        sha256=str(record.get("sha256", "")),
        size_bytes=int(size) if isinstance(size, int) else -1,
    )
    return verify_file_record(normalized, root=root)


def _validate_previous_targets(targets: pl.DataFrame) -> None:
    required = {"date", "candidate", "symbol", "target_weight"}
    if not required.issubset(targets.columns):
        raise ValueError("packaged main-candidate targets have invalid schema")
    if targets.is_empty() or set(targets.get_column("candidate")) != {MAIN_CANDIDATE}:
        raise ValueError("packaged targets substitute the sealed main candidate")
    if targets.get_column("date").max() != FINAL_HISTORY_DATE:
        raise ValueError("packaged main-candidate holdings must end on 2021-12-31")


def _assert_pretest(frame: pl.DataFrame, label: str) -> None:
    if frame.is_empty() or "date" not in frame.columns:
        raise ValueError(f"{label} is empty or unkeyed")
    if frame.get_column("date").max() > FINAL_HISTORY_DATE:
        raise ValueError(f"{label} extends beyond 2021-12-31")


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload
