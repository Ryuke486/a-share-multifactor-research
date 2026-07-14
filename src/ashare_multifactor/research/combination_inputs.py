from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from ashare_multifactor.audit.records import FileRecord, verify_file_record
from ashare_multifactor.research.factor_lineage import validate_file_records


CORE_INPUTS = (
    "factor_panel.parquet",
    "factor_classifications.parquet",
    "rank_ic.parquet",
    "forward_returns.parquet",
    "factor_correlations.parquet",
)


@dataclass(frozen=True)
class StageFiveInputs:
    root: Path
    consumed_files: tuple[FileRecord, ...]


def validate_stage_five_inputs(
    root: Path,
    *,
    validate_core: bool = True,
) -> StageFiveInputs:
    lineage_path = root / "lineage.json"
    if not lineage_path.is_file():
        raise FileNotFoundError(lineage_path)
    lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
    records: list[FileRecord] = []
    if validate_core:
        expected = set(CORE_INPUTS)
        core_records = []
        for stage in ("factors", "evaluate"):
            for record in lineage.get("stages", {}).get(stage, {}).get("outputs", []):
                if record.get("path") in expected:
                    core_records.append(record)
        if {record["path"] for record in core_records} != expected:
            raise ValueError("stage four lineage does not bind every required upstream file")
        validate_file_records(core_records, root)
        records.extend(_record(item, "stage_four_core") for item in core_records)
    daily = lineage.get("daily_panel", {})
    daily_records = [daily.get("manifest"), daily.get("quality_issues")]
    daily_records.extend(daily.get("partitions", []))
    if not daily_records or any(item is None for item in daily_records):
        raise ValueError("stage four lineage does not bind daily panel inputs")
    for item in daily_records:
        record = _record(item, "daily_panel")
        verify_file_record(record, root=root)
        records.append(record)
    lineage_record = FileRecord(
        path="lineage.json",
        role="stage_four_lineage",
        sha256=_sha(lineage_path),
        size_bytes=lineage_path.stat().st_size,
    )
    records.append(lineage_record)
    return StageFiveInputs(root.resolve(), tuple(sorted(records, key=lambda item: item.path)))


def _record(item: dict[str, object], role: str) -> FileRecord:
    return FileRecord(
        path=str(item["path"]),
        role=role,
        sha256=str(item["sha256"]),
        size_bytes=int(item["size_bytes"]),
    )


def _sha(path: Path) -> str:
    from ashare_multifactor.audit.records import sha256_file

    return sha256_file(path)
