import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from ashare_multifactor.audit.publication import publish_release, resolve_current
from ashare_multifactor.audit import publication as publication_module
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


def test_publish_ignores_replaced_staged_copy_for_frozen_artifact_tree(
    tmp_path: Path,
) -> None:
    datasets, artifacts = _staged(tmp_path)
    staged = artifacts / "execution_inputs"
    staged.mkdir()
    (staged / "manifest.json").write_bytes(b"attacker staged bytes")
    frozen = {"manifest.json": b"registry-bound bytes"}

    published = publish_release(
        tmp_path / "published",
        run_id="frozen",
        staged_datasets=datasets,
        staged_artifacts=artifacts,
        lineage={"identity": "frozen"},
        frozen_artifact_trees={"execution_inputs": frozen},
    )

    assert (
        published.artifacts / "execution_inputs/manifest.json"
    ).read_bytes() == b"registry-bound bytes"


def test_publish_rejects_artifacts_parent_replacement_during_frozen_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    datasets, artifacts = _staged(tmp_path)
    root = tmp_path / "published"
    write_tree = publication_module.write_frozen_tree_at
    swapped = False

    def replace_artifacts(*args: object, **kwargs: object) -> None:
        nonlocal swapped
        if not swapped:
            staging = root / "releases/.race.tmp"
            (staging / "artifacts").rename(staging / "artifacts.displaced")
            (staging / "artifacts").mkdir()
            swapped = True
        write_tree(*args, **kwargs)

    monkeypatch.setattr(
        publication_module,
        "write_frozen_tree_at",
        replace_artifacts,
    )

    with pytest.raises(ValueError, match="artifacts staging identity changed"):
        publish_release(
            root,
            run_id="race",
            staged_datasets=datasets,
            staged_artifacts=artifacts,
            lineage={"identity": "race"},
            frozen_artifact_trees={
                "execution_inputs": {"manifest.json": b"frozen"}
            },
        )

    assert not (root / "releases/race").exists()


def test_code_identity_records_dirty_diff_and_source_hashes(tmp_path: Path) -> None:
    source = tmp_path / "src" / "package" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    for args in (
        ("init",),
        ("config", "user.email", "test@example.invalid"),
        ("config", "user.name", "Test User"),
        ("add", "src/package/module.py"),
        ("commit", "-m", "baseline"),
    ):
        subprocess.run(
            ("git", *args),
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
    source.write_text("VALUE = 2\n", encoding="utf-8")

    identity = code_identity(tmp_path)

    assert identity["commit"]
    assert identity["dirty"] is True
    assert len(identity["diff_sha256"]) == 64
    assert identity["sources"] == [
        {
            "path": "src/package/module.py",
            "role": "source",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "size_bytes": source.stat().st_size,
        }
    ]
