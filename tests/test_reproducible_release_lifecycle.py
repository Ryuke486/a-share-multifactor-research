from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ashare_multifactor.audit import reproducible_release as lifecycle
from ashare_multifactor.robustness import pipeline as robustness_pipeline
from ashare_multifactor.validation import pipeline as validation_pipeline


FIXED_CODE = {"commit": "a" * 40, "dirty": False, "tree": "b" * 40}
FIXED_INPUTS = {"frozen": {"sha256": "c" * 64, "size": 7}}


def _json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()


def _expected_stage7_manifest(run_id: str, *, dirty: bool = False) -> dict[str, object]:
    return {
        "run_id": run_id,
        "code": {**FIXED_CODE, "dirty": dirty},
        "inputs": FIXED_INPUTS,
        "core_file_count": 98,
        "files": [
            {
                "path": relative,
                "sha256": hashlib.sha256(f"stage7:{relative}\n".encode()).hexdigest(),
                "size": len(f"stage7:{relative}\n".encode()),
            }
            for relative in lifecycle._STAGE7_V1.core_paths
        ],
        "resource_usage": {
            "elapsed_seconds": 1.25,
            "maximum_rss_bytes": 2048,
        },
    }


class Stage7Adapter:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self.bind_count = 0

    def bind(self, code_root: Path, *, run_id: str) -> lifecycle._RunBinding:
        self.bind_count += 1
        return lifecycle._RunBinding(
            profile="stage7_v1",
            data_root=self.data_root,
            run_id=run_id,
            code=FIXED_CODE,
            inputs=FIXED_INPUTS,
        )

    def execute(
        self,
        binding: lifecycle._RunBinding,
        run_root: Path,
    ) -> dict[str, object]:
        for relative in lifecycle._STAGE7_V1.core_paths:
            destination = run_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(f"stage7:{relative}\n".encode())
        return {
            "resource_usage": {
                "elapsed_seconds": 1.25,
                "maximum_rss_bytes": 2048,
            }
        }

    def prepare_release(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("run_once must not prepare a release")


class Stage8Adapter(Stage7Adapter):
    def bind(self, code_root: Path, *, run_id: str) -> lifecycle._RunBinding:
        self.bind_count += 1
        return lifecycle._RunBinding(
            profile="stage8_v1",
            data_root=self.data_root,
            run_id=run_id,
            code=FIXED_CODE,
            inputs=FIXED_INPUTS,
        )

    def execute(
        self,
        binding: lifecycle._RunBinding,
        run_root: Path,
    ) -> dict[str, object]:
        for relative in lifecycle._STAGE8_V1.core_paths:
            destination = run_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(f"stage8:{relative}\n".encode())
        (run_root / "extra.txt").write_text("legacy extra\n")
        return {
            "resource_usage": {
                "elapsed_seconds": 9.0,
                "maximum_rss_bytes": 9999,
            }
        }


class FailingStage7Adapter(Stage7Adapter):
    def execute(
        self,
        binding: lifecycle._RunBinding,
        run_root: Path,
    ) -> dict[str, object]:
        (run_root / "partial.txt").write_text("partial\n")
        raise RuntimeError("stage execution failed")


class DriftingAdapter(Stage7Adapter):
    def __init__(self, data_root: Path, drift: str) -> None:
        super().__init__(data_root)
        self.drift = drift
        self.bind_count = 0

    def bind(self, code_root: Path, *, run_id: str) -> lifecycle._RunBinding:
        self.bind_count += 1
        code = FIXED_CODE
        inputs = FIXED_INPUTS
        if self.bind_count > 1 and self.drift == "code":
            code = {**FIXED_CODE, "commit": "d" * 40}
        if self.bind_count > 1 and self.drift == "inputs":
            inputs = {"changed": True}
        return lifecycle._RunBinding(
            profile="stage7_v1",
            data_root=self.data_root,
            run_id=run_id,
            code=code,
            inputs=inputs,
        )


class InvalidStage7TreeAdapter(Stage7Adapter):
    def __init__(self, data_root: Path, mutation: str) -> None:
        super().__init__(data_root)
        self.mutation = mutation

    def execute(
        self,
        binding: lifecycle._RunBinding,
        run_root: Path,
    ) -> dict[str, object]:
        result = super().execute(binding, run_root)
        if self.mutation == "missing":
            (run_root / lifecycle._STAGE7_V1.core_paths[0]).unlink()
        elif self.mutation == "extra":
            (run_root / "extra.txt").write_text("extra\n")
        elif self.mutation == "symlink":
            (run_root / "link").symlink_to(run_root / lifecycle._STAGE7_V1.core_paths[0])
        return result


class MismatchedStage8Adapter(Stage8Adapter):
    def __init__(self, data_root: Path, mutation: str) -> None:
        super().__init__(data_root)
        self.mutation = mutation

    def bind(self, code_root: Path, *, run_id: str) -> lifecycle._RunBinding:
        code = FIXED_CODE
        inputs = FIXED_INPUTS
        if run_id.endswith("_2") and self.mutation == "code":
            code = {**FIXED_CODE, "commit": "e" * 40}
        if run_id.endswith("_2") and self.mutation == "inputs":
            inputs = {"changed": True}
        return lifecycle._RunBinding(
            profile="stage8_v1",
            data_root=self.data_root,
            run_id=run_id,
            code=code,
            inputs=inputs,
        )

    def execute(
        self,
        binding: lifecycle._RunBinding,
        run_root: Path,
    ) -> dict[str, object]:
        result = super().execute(binding, run_root)
        if binding.run_id.endswith("_2") and self.mutation == "output":
            (run_root / "report.md").write_text("changed\n")
        return result


class DirtyStage8Adapter(Stage8Adapter):
    def bind(self, code_root: Path, *, run_id: str) -> lifecycle._RunBinding:
        return lifecycle._RunBinding(
            profile="stage8_v1",
            data_root=self.data_root,
            run_id=run_id,
            code={**FIXED_CODE, "dirty": True},
            inputs=FIXED_INPUTS,
        )


class ReleasingStage8Adapter(Stage8Adapter):
    def __init__(self, data_root: Path, *, tamper_source: bool = False) -> None:
        super().__init__(data_root)
        self.tamper_source = tamper_source
        self.prepared_source: Path | None = None
        self.prepared_certificate: dict[str, object] | None = None

    def prepare_release(
        self,
        binding: lifecycle._RunBinding,
        source: Path,
        certificate: dict[str, object],
        *,
        run_id: str,
        options: object,
    ) -> lifecycle._ReleasePreparation:
        self.prepared_source = source
        self.prepared_certificate = certificate
        staged = self.data_root / "artifacts/robustness/release_staging" / run_id
        if staged.exists():
            raise FileExistsError(staged)
        datasets = staged / "datasets"
        artifacts = staged / "artifacts"
        datasets.mkdir(parents=True)
        artifacts.mkdir()
        (datasets / "robustness_results.parquet").write_bytes(
            (source / "robustness_results.parquet").read_bytes()
        )
        for name in ("report.md", "protocol_gate.json"):
            (artifacts / name).write_bytes((source / name).read_bytes())
        if self.tamper_source:
            (source / "report.md").write_text("tampered during staging\n")
        return lifecycle._ReleasePreparation(
            staged_root=staged,
            publication_root=self.data_root / "processed/robustness",
            staged_datasets=datasets,
            staged_artifacts=artifacts,
            lineage={"certificate": certificate, "source": source.as_posix()},
            manifest_metadata={"stage": "robustness"},
        )


class ReleasingStage7Adapter(Stage7Adapter):
    def __init__(self, data_root: Path) -> None:
        super().__init__(data_root)
        self.current_code = FIXED_CODE

    def bind(self, code_root: Path, *, run_id: str) -> lifecycle._RunBinding:
        return lifecycle._RunBinding(
            profile="stage7_v1",
            data_root=self.data_root,
            run_id=run_id,
            code=self.current_code,
            inputs=FIXED_INPUTS,
        )

    def prepare_release(
        self,
        binding: lifecycle._RunBinding,
        source: Path,
        certificate: dict[str, object],
        *,
        run_id: str,
        options: object,
    ) -> lifecycle._ReleasePreparation:
        staged = (
            self.data_root
            / "artifacts/validation_evaluation/validation_runs"
            / run_id
        )
        if staged.exists():
            raise FileExistsError(staged)
        datasets = staged / "datasets"
        artifacts = staged / "artifacts"
        datasets.mkdir(parents=True)
        artifacts.mkdir()
        return lifecycle._ReleasePreparation(
            staged_root=staged,
            publication_root=self.data_root / "processed/validation_evaluation",
            staged_datasets=datasets,
            staged_artifacts=artifacts,
            lineage={"code": dict(binding.code), "certificate": certificate},
            manifest_metadata={"stage": "validation_evaluation"},
        )


def test_run_once_creates_stage7_run_and_writes_compatible_manifest(
    tmp_path: Path,
) -> None:
    adapter = Stage7Adapter(tmp_path)

    run_root = lifecycle.run_once(adapter, tmp_path / "code", run_id="fixed")

    expected_files = [
        {
            "path": relative,
            "sha256": hashlib.sha256(f"stage7:{relative}\n".encode()).hexdigest(),
            "size": len(f"stage7:{relative}\n".encode()),
        }
        for relative in lifecycle._STAGE7_V1.core_paths
    ]
    expected = {
        "run_id": "fixed",
        "code": FIXED_CODE,
        "inputs": FIXED_INPUTS,
        "core_file_count": 98,
        "files": expected_files,
        "resource_usage": {
            "elapsed_seconds": 1.25,
            "maximum_rss_bytes": 2048,
        },
    }
    assert run_root == tmp_path / "artifacts/validation_evaluation/full_runs/fixed"
    assert (run_root / "run_manifest.json").read_bytes() == _json_bytes(expected)


def test_run_once_preserves_stage8_loose_paths_and_manifest_shape(
    tmp_path: Path,
) -> None:
    adapter = Stage8Adapter(tmp_path)

    run_root = lifecycle.run_once(
        adapter,
        tmp_path / "code",
        run_id="nested/legacy run",
    )

    expected_files = [
        {
            "path": relative,
            "sha256": hashlib.sha256(f"stage8:{relative}\n".encode()).hexdigest(),
            "size": len(f"stage8:{relative}\n".encode()),
        }
        for relative in lifecycle._STAGE8_V1.core_paths
    ]
    expected = {
        "run_id": "nested/legacy run",
        "code": FIXED_CODE,
        "inputs": FIXED_INPUTS,
        "core_file_count": 3,
        "files": expected_files,
    }
    assert run_root == tmp_path / "artifacts/robustness/full_runs/nested/legacy run"
    assert (run_root / "extra.txt").is_file()
    assert (run_root / "run_manifest.json").read_bytes() == _json_bytes(expected)


def test_run_once_removes_only_its_new_directory_when_execution_fails(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "artifacts/validation_evaluation/full_runs/failed"

    with pytest.raises(RuntimeError, match="^stage execution failed$"):
        lifecycle.run_once(
            FailingStage7Adapter(tmp_path),
            tmp_path / "code",
            run_id="failed",
        )

    assert not run_root.exists()


@pytest.mark.parametrize(
    ("drift", "message"),
    [
        ("code", "code identity changed during validation run"),
        ("inputs", "frozen inputs changed during validation run"),
    ],
)
def test_run_once_rejects_binding_drift_and_cleans_the_run(
    tmp_path: Path,
    drift: str,
    message: str,
) -> None:
    run_root = tmp_path / "artifacts/validation_evaluation/full_runs/drift"

    with pytest.raises(ValueError, match=f"^{message}$"):
        lifecycle.run_once(
            DriftingAdapter(tmp_path, drift),
            tmp_path / "code",
            run_id="drift",
        )

    assert not run_root.exists()


@pytest.mark.parametrize(
    ("mutation", "error", "message"),
    [
        (
            "missing",
            FileNotFoundError,
            "validation run is incomplete: datasets/factor_features.parquet",
        ),
        (
            "extra",
            ValueError,
            "validation run contains unexpected files: extra.txt",
        ),
        ("symlink", ValueError, "validation run must not contain symlinks"),
    ],
)
def test_run_once_enforces_the_stage7_closed_tree_contract(
    tmp_path: Path,
    mutation: str,
    error: type[Exception],
    message: str,
) -> None:
    run_root = tmp_path / "artifacts/validation_evaluation/full_runs/invalid"

    with pytest.raises(error, match=f"^{message}$"):
        lifecycle.run_once(
            InvalidStage7TreeAdapter(tmp_path, mutation),
            tmp_path / "code",
            run_id="invalid",
        )

    assert not run_root.exists()


def test_run_once_preserves_stage7_run_id_and_existing_target_errors(
    tmp_path: Path,
) -> None:
    adapter = Stage7Adapter(tmp_path)
    with pytest.raises(
        ValueError,
        match="^validation run ID must be a safe slug$",
    ):
        lifecycle.run_once(adapter, tmp_path / "code", run_id="../escape")

    existing = tmp_path / "artifacts/validation_evaluation/full_runs/existing"
    existing.mkdir(parents=True)
    sentinel = existing / "owned.txt"
    sentinel.write_text("keep\n")
    with pytest.raises(FileExistsError) as error:
        lifecycle.run_once(adapter, tmp_path / "code", run_id="existing")
    assert error.value.args == (existing,)
    assert sentinel.read_text() == "keep\n"


def test_reproduce_writes_stage7_certificate_and_certifies_the_second_run(
    tmp_path: Path,
) -> None:
    adapter = Stage7Adapter(tmp_path)
    result = lifecycle.reproduce(
        adapter,
        tmp_path / "code",
        run_id_prefix="pair",
    )

    expected_manifests = [
        _expected_stage7_manifest("pair_1"),
        _expected_stage7_manifest("pair_2"),
    ]
    expected = {
        "scope": "full_pipeline",
        "run_ids": ["pair_1", "pair_2"],
        "core_file_count": 98,
        "outputs_identical": True,
        "full_pipeline_reproducible": True,
        "release_eligible": True,
        "reason": None,
        "code": FIXED_CODE,
        "inputs": FIXED_INPUTS,
        "core_hashes": {
            relative: hashlib.sha256(f"stage7:{relative}\n".encode()).hexdigest()
            for relative in lifecycle._STAGE7_V1.core_paths
        },
        "run_manifest_sha256": [
            hashlib.sha256(_json_bytes(manifest)).hexdigest()
            for manifest in expected_manifests
        ],
    }
    certificate = (
        tmp_path / "processed/validation_evaluation/full_reproducibility.json"
    )
    assert result == expected
    assert certificate.read_bytes() == _json_bytes(expected)
    assert result["run_ids"][1] == "pair_2"
    assert adapter.bind_count == 4


def test_reproduce_writes_the_distinct_stage8_certificate_shape(
    tmp_path: Path,
) -> None:
    result = lifecycle.reproduce(
        Stage8Adapter(tmp_path),
        tmp_path / "code",
        run_id_prefix="robust",
    )

    expected = {
        "outputs_identical": True,
        "core_file_count": 3,
        "core_hashes": {
            relative: hashlib.sha256(f"stage8:{relative}\n".encode()).hexdigest()
            for relative in lifecycle._STAGE8_V1.core_paths
        },
        "run_ids": ["robust_1", "robust_2"],
        "code": FIXED_CODE,
        "inputs": FIXED_INPUTS,
        "release_eligible": True,
        "reason": None,
    }
    certificate = tmp_path / "processed/robustness/reproducibility.json"
    assert result == expected
    assert certificate.read_bytes() == _json_bytes(expected)
    assert "full_pipeline_reproducible" not in result
    assert "run_manifest_sha256" not in result


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("code", "robustness runs used different code identities"),
        ("inputs", "robustness runs used different inputs"),
        ("output", "robustness runs differ: report.md"),
    ],
)
def test_reproduce_rejects_stage8_identity_input_and_core_drift(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=f"^{message}$"):
        lifecycle.reproduce(
            MismatchedStage8Adapter(tmp_path, mutation),
            tmp_path / "code",
            run_id_prefix="mismatch",
        )


def test_reproduce_records_dirty_outputs_as_ineligible_without_rejecting_them(
    tmp_path: Path,
) -> None:
    result = lifecycle.reproduce(
        DirtyStage8Adapter(tmp_path),
        tmp_path / "code",
        run_id_prefix="dirty",
    )

    assert result["outputs_identical"] is True
    assert result["release_eligible"] is False
    assert result["reason"] == "git_identity_is_dirty"


def test_release_rebuilds_a_mutable_certificate_and_returns_staging_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ReleasingStage8Adapter(tmp_path)
    expected = lifecycle.reproduce(
        adapter,
        tmp_path / "code",
        run_id_prefix="certified",
    )
    certificate_path = tmp_path / "processed/robustness/reproducibility.json"
    tampered = {**expected, "core_hashes": {"report.md": "0" * 64}}
    certificate_path.write_bytes(_json_bytes(tampered))
    monkeypatch.setattr(
        lifecycle,
        "publish_release",
        lambda *args, **kwargs: pytest.fail("publish=false must not publish"),
    )

    prepared = lifecycle.release(
        adapter,
        tmp_path / "code",
        run_id="staged",
        publish=False,
    )

    assert isinstance(prepared, lifecycle._ReleasePreparation)
    assert prepared.staged_root == tmp_path / "artifacts/robustness/release_staging/staged"
    assert adapter.prepared_source == (
        tmp_path / "artifacts/robustness/full_runs/certified_2"
    )
    assert adapter.prepared_certificate == expected
    assert certificate_path.read_bytes() == _json_bytes(expected)


def test_release_calls_the_existing_publisher_only_when_publish_is_true(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ReleasingStage8Adapter(tmp_path)
    lifecycle.reproduce(adapter, tmp_path / "code", run_id_prefix="publishable")
    captured: dict[str, object] = {}
    published = object()

    def fake_publish(root: Path, **kwargs: object) -> object:
        captured["root"] = root
        captured.update(kwargs)
        return published

    monkeypatch.setattr(lifecycle, "publish_release", fake_publish)

    result = lifecycle.release(
        adapter,
        tmp_path / "code",
        run_id="release-id",
        publish=True,
    )

    assert result is published
    assert captured["root"] == tmp_path / "processed/robustness"
    assert captured["run_id"] == "release-id"
    assert captured["staged_datasets"] == (
        tmp_path / "artifacts/robustness/release_staging/release-id/datasets"
    )
    assert captured["manifest_metadata"] == {"stage": "robustness"}


def test_release_reverifies_the_certified_source_after_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ReleasingStage8Adapter(tmp_path, tamper_source=True)
    lifecycle.reproduce(adapter, tmp_path / "code", run_id_prefix="source")
    monkeypatch.setattr(
        lifecycle,
        "publish_release",
        lambda *args, **kwargs: pytest.fail("tampered source must not publish"),
    )

    with pytest.raises(
        ValueError,
        match="^reproducible source hash changed: report.md$",
    ):
        lifecycle.release(
            adapter,
            tmp_path / "code",
            run_id="tampered",
            publish=True,
        )


def test_stage7_nonpublishing_staging_does_not_require_certified_code_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ReleasingStage7Adapter(tmp_path)
    lifecycle.reproduce(adapter, tmp_path / "code", run_id_prefix="validation")
    adapter.current_code = {**FIXED_CODE, "commit": "f" * 40, "dirty": True}
    monkeypatch.setattr(
        lifecycle,
        "publish_release",
        lambda *args, **kwargs: pytest.fail("publish=false must not publish"),
    )

    prepared = lifecycle.release(
        adapter,
        tmp_path / "code",
        run_id="staged-only",
        publish=False,
    )

    assert prepared.staged_root.name == "staged-only"
    assert prepared.lineage["code"] == adapter.current_code


def test_release_preserves_the_stage7_safe_slug_boundary(tmp_path: Path) -> None:
    adapter = ReleasingStage7Adapter(tmp_path)
    lifecycle.reproduce(adapter, tmp_path / "code", run_id_prefix="validation")

    with pytest.raises(
        ValueError,
        match="^validation run ID must be a safe slug$",
    ):
        lifecycle.release(
            adapter,
            tmp_path / "code",
            run_id="../escape",
            publish=False,
        )

    assert not (tmp_path / "artifacts/validation_evaluation/escape").exists()


def test_release_rejects_a_dirty_certificate_before_publication(
    tmp_path: Path,
) -> None:
    adapter = DirtyStage8Adapter(tmp_path)
    lifecycle.reproduce(adapter, tmp_path / "code", run_id_prefix="dirty")

    with pytest.raises(
        ValueError,
        match="^current code identity is dirty$",
    ):
        lifecycle.release(
            adapter,
            tmp_path / "code",
            run_id="must-not-publish",
            publish=True,
        )


def test_lifecycle_owns_two_profiles_and_only_three_adapter_capabilities() -> None:
    assert lifecycle._STAGE7_V1.core_paths == validation_pipeline.CORE_VALIDATION_FILES
    assert lifecycle._STAGE8_V1.core_paths == robustness_pipeline.CORE_ROBUSTNESS_FILES
    assert {
        name
        for name, value in vars(lifecycle._Adapter).items()
        if not name.startswith("_") and callable(value)
    } == {"bind", "execute", "prepare_release"}
    assert lifecycle.__all__ == ("run_once", "reproduce", "release")
