from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from ashare_multifactor.audit.contracts import DatasetContract, dataset_manifest_entry
from ashare_multifactor.audit.records import sha256_file


def stage_five_dataset_entries(
    root: Path,
    frames: dict[str, pl.DataFrame],
    *,
    analysis_start: date,
    analysis_end: date,
) -> list[dict[str, object]]:
    contracts = {
        "composite_scores": DatasetContract(
            "composite_scores", ("date", "symbol", "method"), "date",
            analysis_start, analysis_end, ("score", "raw_score"),
        ),
        "composite_weights": DatasetContract(
            "composite_weights", ("date", "method", "factor_name"), "date",
            analysis_start, analysis_end, ("weight",), ("weight",),
        ),
        "target_weights": DatasetContract(
            "target_weights", ("date", "portfolio_name", "symbol"), "date",
            analysis_start, analysis_end, ("target_weight",), ("target_weight",),
        ),
        "target_weight_diagnostics": DatasetContract(
            "target_weight_diagnostics", ("date", "portfolio_name"), "date",
            analysis_start, analysis_end,
            ("one_way_turnover", "holdings", "max_weight", "hhi", "effective_holdings"),
        ),
    }
    entries = []
    for name, contract in contracts.items():
        entry = dataset_manifest_entry(
                root / f"{name}.parquet",
                root=root,
                role="stage_five_dataset",
                frame=frames[name],
                contract=contract,
                producer="ashare_multifactor.research.combination_pipeline",
                stage="factor_combination",
            )
        entry["path"] = f"datasets/{entry['path']}"
        entries.append(entry)
    return entries


def build_output_file_manifest(processed: Path, artifacts: Path) -> dict[str, object]:
    files = []
    for label, root in (("processed", processed), ("artifacts", artifacts)):
        for path in sorted(
            item for item in root.rglob("*") if item.is_file() and item.name != "manifest.json"
        ):
            files.append(
                {
                    "path": f"{label}/{path.relative_to(root).as_posix()}",
                    "sha256": sha256_file(path),
                    "size_bytes": path.stat().st_size,
                }
            )
    return {"files": files}
