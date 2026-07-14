from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from ashare_multifactor.audit.identity import code_identity
from ashare_multifactor.audit.records import sha256_file
from ashare_multifactor.research.combination_inputs import StageFiveInputs


def build_combination_lineage(
    upstream: Path,
    config,
    inputs: StageFiveInputs,
) -> dict[str, object]:
    payload = {
        "factor_combination": asdict(config.factor_combination),
        "portfolio_construction": asdict(config.portfolio_construction),
    }
    encoded = json.dumps(payload, default=str, sort_keys=True).encode()
    return {
        "upstream_root": str(upstream.resolve()),
        "upstream_files": [record.to_dict() for record in inputs.consumed_files],
        "config": json.loads(encoded),
        "config_sha256": hashlib.sha256(encoded).hexdigest(),
        "code_identity": code_identity(Path.cwd()),
    }


def build_legacy_lineage(upstream: Path, config, names: tuple[str, ...]) -> dict[str, object]:
    payload = {
        "factor_combination": asdict(config.factor_combination),
        "portfolio_construction": asdict(config.portfolio_construction),
    }
    encoded = json.dumps(payload, default=str, sort_keys=True).encode()
    records = [
        {
            "path": name,
            "sha256": sha256_file(upstream / name),
            "size_bytes": (upstream / name).stat().st_size,
        }
        for name in names
    ]
    return {
        "upstream_root": str(upstream.resolve()),
        "upstream_files": records,
        "config": json.loads(encoded),
        "config_sha256": hashlib.sha256(encoded).hexdigest(),
        "code_identity": code_identity(Path.cwd()),
    }
