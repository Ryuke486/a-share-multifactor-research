import json
from pathlib import Path

import pytest

from ashare_multifactor.audit.publication import publish_release, resolve_current
from ashare_multifactor.audit.identity import code_identity


def _staged(tmp_path: Path) -> tuple[Path, Path]:
    datasets = tmp_path / "staged-datasets"
    artifacts = tmp_path / "staged-artifacts"
    datasets.mkdir(parents=True)
    artifacts.mkdir(parents=True)
    (datasets / "target_weights.parquet").write_bytes(b"weights")
    (artifacts / "report.md").write_text("report", encoding="utf-8")
    return datasets, artifacts


def test_failed_release_does_not_change_current_pointer(tmp_path: Path) -> None:
    datasets, artifacts = _staged(tmp_path)
    root = tmp_path / "published"
    first = publish_release(
        root,
        run_id="first",
        staged_datasets=datasets,
        staged_artifacts=artifacts,
        lineage={"identity": "first"},
    )
    bad_datasets, bad_artifacts = _staged(tmp_path / "second")

    with pytest.raises(RuntimeError, match="before pointer switch"):
        publish_release(
            root,
            run_id="second",
            staged_datasets=bad_datasets,
            staged_artifacts=bad_artifacts,
            lineage={"identity": "second"},
            fail_before_switch=True,
        )

    assert resolve_current(root).run_id == first.run_id


def test_resolver_detects_manifest_or_dataset_tampering(tmp_path: Path) -> None:
    datasets, artifacts = _staged(tmp_path)
    root = tmp_path / "published"
    published = publish_release(
        root,
        run_id="only",
        staged_datasets=datasets,
        staged_artifacts=artifacts,
        lineage={"identity": "only"},
    )
    target = published.datasets / "target_weights.parquet"
    target.write_bytes(b"tampered")

    with pytest.raises(ValueError, match="mismatch"):
        resolve_current(root)


def test_current_pointer_contains_manifest_hash(tmp_path: Path) -> None:
    datasets, artifacts = _staged(tmp_path)
    root = tmp_path / "published"
    published = publish_release(
        root,
        run_id="only",
        staged_datasets=datasets,
        staged_artifacts=artifacts,
        lineage={"identity": "only"},
    )

    current = json.loads((root / "CURRENT.json").read_text(encoding="utf-8"))
    assert current == {"manifest_sha256": published.manifest_sha256, "run_id": "only"}


def test_release_manifest_preserves_dataset_contract_metadata(tmp_path: Path) -> None:
    datasets, artifacts = _staged(tmp_path)
    root = tmp_path / "published"
    published = publish_release(
        root,
        run_id="only",
        staged_datasets=datasets,
        staged_artifacts=artifacts,
        lineage={"identity": "only"},
        manifest_metadata={"datasets": [{"dataset": "target_weights", "rows": 10}]},
    )

    manifest = json.loads(published.manifest.read_text(encoding="utf-8"))
    assert manifest["datasets"] == [{"dataset": "target_weights", "rows": 10}]


def test_code_identity_records_dirty_diff_and_source_hashes() -> None:
    identity = code_identity(Path.cwd())

    assert identity["commit"]
    assert identity["dirty"] is True
    assert len(identity["diff_sha256"]) == 64
    assert any(item["path"].endswith("audit/publication.py") for item in identity["sources"])
