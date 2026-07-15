import json
from pathlib import Path

import pytest

from ashare_multifactor.robustness.pipeline import verify_reproducible_source


def test_publication_rechecks_source_files_against_reproducibility(tmp_path: Path) -> None:
    source = tmp_path / "run_2"
    source.mkdir()
    files = {
        "robustness_results.parquet": b"results",
        "report.md": b"report",
        "protocol_gate.json": b'{"sealed_test_protocol_allowed":true}',
    }
    import hashlib

    hashes = {}
    manifest_files = []
    for name, content in files.items():
        path = source / name
        path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        hashes[name] = digest
        manifest_files.append({"path": name, "sha256": digest, "size": len(content)})
    (source / "run_manifest.json").write_text(
        json.dumps({"run_id": "run_2", "code": {"dirty": False}, "inputs": {}, "files": manifest_files}),
        encoding="utf-8",
    )
    reproducibility = {
        "run_ids": ["run_1", "run_2"],
        "core_hashes": hashes,
        "code": {"dirty": False},
        "inputs": {},
    }

    verify_reproducible_source(source, reproducibility)
    (source / "report.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="source hash changed"):
        verify_reproducible_source(source, reproducibility)
