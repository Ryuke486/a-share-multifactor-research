from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import hashlib
import json

import pytest

from ashare_multifactor.audit import reproducible_release as lifecycle
from ashare_multifactor.validation import pipeline
from ashare_multifactor.validation import release_adapter


FIXED_CODE: dict[str, object] = {
    "commit": "a" * 40,
    "dirty": False,
    "tree": "b" * 40,
}
FIXED_INPUTS: dict[str, object] = {
    "frozen_input": {"sha256": "c" * 64, "size": 123}
}


def _json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()


def _write_stage7_core(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative in lifecycle._STAGE7_V1.core_paths:
        payload = f"stage7:{relative}\n".encode()
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        hashes[relative] = hashlib.sha256(payload).hexdigest()
    return hashes


def test_stage7_adapter_bind_captures_the_compatible_release_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    code_root = tmp_path / "code"
    data_root = tmp_path / "data"
    calls: list[Path] = []
    monkeypatch.setattr(
        release_adapter.pipeline,
        "resolve_validation_data_root",
        lambda root: data_root,
    )
    monkeypatch.setattr(
        release_adapter.pipeline,
        "assert_validation_upstream_releases",
        lambda root: calls.append(root),
    )
    monkeypatch.setattr(
        release_adapter.pipeline,
        "code_identity",
        lambda root: FIXED_CODE,
    )
    monkeypatch.setattr(
        release_adapter.pipeline,
        "_validation_input_identity",
        lambda root, data: FIXED_INPUTS,
    )
    monkeypatch.setattr(
        release_adapter.pipeline,
        "load_config",
        lambda path: pytest.fail("run binding must not move calculation config reads"),
    )

    binding = release_adapter.Stage7ReleaseAdapter().bind(
        code_root,
        run_id="stage7-fixed",
    )

    assert binding.profile == "stage7_v1"
    assert binding.data_root == data_root
    assert binding.run_id == "stage7-fixed"
    assert binding.code == FIXED_CODE
    assert binding.inputs == FIXED_INPUTS
    assert binding.candidates == pipeline.FROZEN_CANDIDATES
    assert calls == [data_root]


def test_stage7_adapter_execute_keeps_calculation_and_resource_contracts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    code_root = tmp_path / "code"
    binding = release_adapter.ValidationReleaseBinding(
        profile="stage7_v1",
        code_root=code_root,
        data_root=tmp_path,
        run_id="stage7-fixed",
        code=FIXED_CODE,
        inputs=FIXED_INPUTS,
        candidates=("candidate-a",),
    )
    run_root = tmp_path / "run"
    calls: list[object] = []

    def execute(code: Path, data: Path, run: Path) -> None:
        calls.append((code, data, run))

    monkeypatch.setattr(release_adapter.full_run, "execute_validation_stages", execute)
    monkeypatch.setattr(
        release_adapter.pipeline,
        "code_identity",
        lambda root: FIXED_CODE,
    )
    monkeypatch.setattr(
        release_adapter.pipeline,
        "_validation_input_identity",
        lambda root, data: FIXED_INPUTS,
    )
    monkeypatch.setattr(
        release_adapter.pipeline,
        "assert_validation_outputs_sealed",
        lambda root: calls.append(("sealed", root)),
    )
    elapsed = iter((10.0, 11.25))
    monkeypatch.setattr(release_adapter.time, "monotonic", lambda: next(elapsed))
    monkeypatch.setattr(
        release_adapter.resource,
        "getrusage",
        lambda kind: SimpleNamespace(ru_maxrss=2048),
    )

    result = release_adapter.Stage7ReleaseAdapter().execute(binding, run_root)

    assert result == {
        "resource_usage": {
            "elapsed_seconds": 1.25,
            "maximum_rss_bytes": 2048,
        }
    }
    assert calls == [
        (code_root, tmp_path, run_root),
        ("sealed", run_root),
    ]


def test_stage7_adapter_prepares_the_legacy_mapping_and_lineage_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    hashes = _write_stage7_core(source)
    binding = release_adapter.ValidationReleaseBinding(
        profile="stage7_v1",
        code_root=tmp_path / "code",
        data_root=tmp_path,
        run_id="release-fixed",
        code=FIXED_CODE,
        inputs=FIXED_INPUTS,
        candidates=("candidate-a",),
    )
    certificate = {
        "run_ids": ["pair_1", "pair_2"],
        "core_hashes": hashes,
        "inputs": FIXED_INPUTS,
    }
    predecessor = {
        "run_id": "previous",
        "manifest_sha256": "d" * 64,
        "lineage_sha256": "e" * 64,
    }
    lineage = {
        "created_at": "2026-08-23T12:00:00+00:00",
        "reproducibility": certificate,
        "supersedes": predecessor,
    }
    calls: list[object] = []
    monkeypatch.setattr(
        release_adapter.pipeline,
        "assert_validation_outputs_sealed",
        lambda root: calls.append(("sealed", root)),
    )
    monkeypatch.setattr(
        release_adapter.pipeline,
        "assert_validation_runtime_gates",
        lambda root, candidates: calls.append(("runtime", root, candidates)),
    )
    monkeypatch.setattr(
        release_adapter.pipeline,
        "assert_validation_upstream_releases",
        lambda root: calls.append(("upstream", root)),
    )
    monkeypatch.setattr(
        release_adapter.pipeline,
        "_validation_input_identity",
        lambda code, data: FIXED_INPUTS,
    )
    monkeypatch.setattr(
        release_adapter.pipeline,
        "resolve_validation_predecessor",
        lambda data: predecessor,
    )
    monkeypatch.setattr(
        release_adapter.pipeline,
        "_validation_lineage",
        lambda code, data, candidates, *, reproducibility, predecessor: lineage,
    )
    monkeypatch.setattr(
        release_adapter.pipeline,
        "load_config",
        lambda path: SimpleNamespace(
            validation_evaluation=SimpleNamespace(candidates=("candidate-a",))
        ),
    )

    adapter = release_adapter.Stage7ReleaseAdapter()
    prepared = adapter.prepare_release(
        binding,
        source,
        certificate,
        run_id="release-fixed",
        options=release_adapter.ValidationReleaseOptions(publish=True),
    )

    staged = tmp_path / "artifacts/validation_evaluation/validation_runs/release-fixed"
    assert isinstance(prepared, lifecycle._ReleasePreparation)
    assert prepared.staged_root == staged
    assert prepared.publication_root == tmp_path / "processed/validation_evaluation"
    assert prepared.staged_datasets == staged / "datasets"
    assert prepared.staged_artifacts == staged / "artifacts"
    assert dict(prepared.lineage) == lineage
    assert prepared.manifest_metadata == {"stage": "validation_evaluation"}
    assert (staged / "lineage.json").read_bytes() == _json_bytes(lineage)
    staged_files = {
        path.relative_to(staged).as_posix()
        for path in staged.rglob("*")
        if path.is_file()
    }
    assert len(staged_files) == 99
    assert calls == [
        ("upstream", tmp_path),
        ("sealed", source),
        ("runtime", source, ("candidate-a",)),
    ]

    monkeypatch.setattr(
        release_adapter.pipeline,
        "resolve_validation_predecessor",
        lambda data: predecessor,
    )
    (staged / "lineage.json").write_bytes(
        _json_bytes({**lineage, "supersedes": {**predecessor, "run_id": "tampered"}})
    )
    with pytest.raises(
        ValueError,
        match="^validation predecessor changed during publication$",
    ):
        dict(prepared.lineage)

    (staged / "lineage.json").write_bytes(_json_bytes(lineage))
    monkeypatch.setattr(
        release_adapter.pipeline,
        "resolve_validation_predecessor",
        lambda data: {**predecessor, "run_id": "changed"},
    )
    with pytest.raises(
        ValueError,
        match="^validation predecessor changed during publication$",
    ):
        dict(prepared.lineage)


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        (
            ({"changed": True},),
            "validation inputs changed during staging",
        ),
        (
            (FIXED_INPUTS, {"changed": True}),
            "validation inputs changed while writing lineage",
        ),
    ],
)
def test_stage7_adapter_cleans_failed_staging_on_input_drift(
    tmp_path: Path,
    monkeypatch,
    inputs: tuple[dict[str, object], ...],
    message: str,
) -> None:
    source = tmp_path / "source"
    hashes = _write_stage7_core(source)
    binding = release_adapter.ValidationReleaseBinding(
        profile="stage7_v1",
        code_root=tmp_path / "code",
        data_root=tmp_path,
        run_id="release-fixed",
        code=FIXED_CODE,
        inputs=FIXED_INPUTS,
        candidates=pipeline.FROZEN_CANDIDATES,
    )
    values = iter(inputs)
    monkeypatch.setattr(
        release_adapter.pipeline,
        "load_config",
        lambda path: SimpleNamespace(
            validation_evaluation=SimpleNamespace(candidates=("candidate-a",))
        ),
    )
    monkeypatch.setattr(
        release_adapter.pipeline,
        "assert_validation_upstream_releases",
        lambda root: None,
    )
    monkeypatch.setattr(
        release_adapter.pipeline,
        "assert_validation_outputs_sealed",
        lambda root: None,
    )
    monkeypatch.setattr(
        release_adapter.pipeline,
        "assert_validation_runtime_gates",
        lambda root, candidates: None,
    )
    monkeypatch.setattr(
        release_adapter.pipeline,
        "_validation_input_identity",
        lambda code, data: next(values),
    )
    monkeypatch.setattr(
        release_adapter.pipeline,
        "resolve_validation_predecessor",
        lambda data: None,
    )
    monkeypatch.setattr(
        release_adapter.pipeline,
        "_validation_lineage",
        lambda *args, **kwargs: {"fixed": True},
    )
    staged = (
        tmp_path
        / "artifacts/validation_evaluation/validation_runs/release-fixed"
    )

    with pytest.raises(ValueError, match=f"^{message}$"):
        release_adapter.Stage7ReleaseAdapter().prepare_release(
            binding,
            source,
            {
                "run_ids": ["pair_1", "pair_2"],
                "core_hashes": hashes,
                "inputs": FIXED_INPUTS,
            },
            run_id="release-fixed",
            options=release_adapter.ValidationReleaseOptions(publish=False),
        )

    assert not staged.exists()


def test_execute_full_validation_run_delegates_to_the_shared_lifecycle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    expected = tmp_path / "full-run"
    calls: list[object] = []

    def run_once(adapter, code_root: Path, *, run_id: str | None) -> Path:
        calls.append((adapter, code_root, run_id))
        return expected

    monkeypatch.setattr(
        pipeline,
        "resolve_validation_data_root",
        lambda root: tmp_path,
    )
    monkeypatch.setattr(
        pipeline,
        "assert_validation_upstream_releases",
        lambda root: None,
    )
    monkeypatch.setattr(lifecycle, "run_once", run_once)

    result = pipeline.execute_full_validation_run(tmp_path, run_id="fixed")

    assert result == expected
    assert len(calls) == 1
    adapter, code_root, run_id = calls[0]
    assert isinstance(adapter, release_adapter.Stage7ReleaseAdapter)
    assert code_root == tmp_path
    assert run_id == "fixed"


def test_stage7_run_facade_preserves_the_frozen_manifest_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        pipeline,
        "resolve_validation_data_root",
        lambda root: tmp_path,
    )
    monkeypatch.setattr(
        pipeline,
        "assert_validation_upstream_releases",
        lambda root: None,
    )
    monkeypatch.setattr(pipeline, "code_identity", lambda root: FIXED_CODE)
    monkeypatch.setattr(
        pipeline,
        "_validation_input_identity",
        lambda code, data: FIXED_INPUTS,
    )
    monkeypatch.setattr(
        release_adapter.full_run,
        "execute_validation_stages",
        lambda code, data, run: _write_stage7_core(run),
    )
    monkeypatch.setattr(
        pipeline,
        "assert_validation_outputs_sealed",
        lambda root: None,
    )
    elapsed = iter((10.0, 11.25))
    monkeypatch.setattr(release_adapter.time, "monotonic", lambda: next(elapsed))
    monkeypatch.setattr(
        release_adapter.resource,
        "getrusage",
        lambda kind: SimpleNamespace(ru_maxrss=2048),
    )

    run_root = pipeline.execute_full_validation_run(tmp_path, run_id="fixed")

    expected = {
        "run_id": "fixed",
        "code": FIXED_CODE,
        "inputs": FIXED_INPUTS,
        "core_file_count": 98,
        "files": [
            {
                "path": relative,
                "sha256": hashlib.sha256(
                    f"stage7:{relative}\n".encode()
                ).hexdigest(),
                "size": len(f"stage7:{relative}\n".encode()),
            }
            for relative in lifecycle._STAGE7_V1.core_paths
        ],
        "resource_usage": {
            "elapsed_seconds": 1.25,
            "maximum_rss_bytes": 2048,
        },
    }
    assert (run_root / "run_manifest.json").read_bytes() == _json_bytes(expected)


def test_execute_validation_reproducibility_delegates_to_the_shared_lifecycle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    expected = {"run_ids": ["pair_1", "pair_2"]}
    calls: list[object] = []

    def reproduce(
        adapter,
        code_root: Path,
        *,
        run_id_prefix: str | None,
    ) -> dict[str, object]:
        calls.append((adapter, code_root, run_id_prefix))
        return expected

    monkeypatch.setattr(lifecycle, "reproduce", reproduce)

    result = pipeline.execute_validation_reproducibility(
        tmp_path,
        run_id_prefix="pair",
    )

    assert result == expected
    assert len(calls) == 1
    adapter, code_root, prefix = calls[0]
    assert isinstance(adapter, release_adapter.Stage7ReleaseAdapter)
    assert code_root == tmp_path
    assert prefix == "pair"


def test_stage7_reproduce_facade_preserves_the_frozen_certificate_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        pipeline,
        "resolve_validation_data_root",
        lambda root: tmp_path,
    )
    monkeypatch.setattr(
        pipeline,
        "assert_validation_upstream_releases",
        lambda root: None,
    )
    monkeypatch.setattr(pipeline, "code_identity", lambda root: FIXED_CODE)
    monkeypatch.setattr(
        pipeline,
        "_validation_input_identity",
        lambda code, data: FIXED_INPUTS,
    )
    monkeypatch.setattr(
        release_adapter.full_run,
        "execute_validation_stages",
        lambda code, data, run: _write_stage7_core(run),
    )
    monkeypatch.setattr(
        pipeline,
        "assert_validation_outputs_sealed",
        lambda root: None,
    )
    elapsed = iter((10.0, 11.25, 20.0, 21.25))
    monkeypatch.setattr(release_adapter.time, "monotonic", lambda: next(elapsed))
    monkeypatch.setattr(
        release_adapter.resource,
        "getrusage",
        lambda kind: SimpleNamespace(ru_maxrss=2048),
    )

    result = pipeline.execute_validation_reproducibility(
        tmp_path,
        run_id_prefix="pair",
    )

    core_hashes = {
        relative: hashlib.sha256(f"stage7:{relative}\n".encode()).hexdigest()
        for relative in lifecycle._STAGE7_V1.core_paths
    }
    manifests = []
    for run_id in ("pair_1", "pair_2"):
        manifests.append(
            {
                "run_id": run_id,
                "code": FIXED_CODE,
                "inputs": FIXED_INPUTS,
                "core_file_count": 98,
                "files": [
                    {
                        "path": relative,
                        "sha256": core_hashes[relative],
                        "size": len(f"stage7:{relative}\n".encode()),
                    }
                    for relative in lifecycle._STAGE7_V1.core_paths
                ],
                "resource_usage": {
                    "elapsed_seconds": 1.25,
                    "maximum_rss_bytes": 2048,
                },
            }
        )
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
        "core_hashes": core_hashes,
        "run_manifest_sha256": [
            hashlib.sha256(_json_bytes(manifest)).hexdigest()
            for manifest in manifests
        ],
    }
    certificate = (
        tmp_path / "processed/validation_evaluation/full_reproducibility.json"
    )
    assert result == expected
    assert certificate.read_bytes() == _json_bytes(expected)


def test_stage_validation_release_delegates_to_nonpublishing_release(
    tmp_path: Path,
    monkeypatch,
) -> None:
    staged = tmp_path / "staged"
    prepared = lifecycle._ReleasePreparation(
        staged_root=staged,
        publication_root=tmp_path / "processed/validation_evaluation",
        staged_datasets=staged / "datasets",
        staged_artifacts=staged / "artifacts",
        lineage={"fixed": True},
    )
    calls: list[object] = []

    def release(adapter, code_root: Path, **kwargs):
        calls.append((adapter, code_root, kwargs))
        return prepared

    monkeypatch.setattr(lifecycle, "release", release)

    result = pipeline.stage_validation_release(
        tmp_path,
        run_id="release-fixed",
    )

    assert result == staged
    assert len(calls) == 1
    adapter, code_root, kwargs = calls[0]
    assert isinstance(adapter, release_adapter.Stage7ReleaseAdapter)
    assert code_root == tmp_path.resolve()
    assert kwargs == {
        "run_id": "release-fixed",
        "publish": False,
        "options": release_adapter.ValidationReleaseOptions(publish=False),
        "certificate": None,
    }


def test_run_validation_release_delegates_publication_to_the_shared_lifecycle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published = object()
    calls: list[object] = []
    monkeypatch.setattr(pipeline, "code_identity", lambda root: FIXED_CODE)

    def release(adapter, code_root: Path, **kwargs):
        calls.append((adapter, code_root, kwargs))
        return published

    monkeypatch.setattr(lifecycle, "release", release)

    result = pipeline.run_validation_release(
        tmp_path,
        publish=True,
        run_id="release-fixed",
    )

    assert result is published
    assert len(calls) == 1
    adapter, code_root, kwargs = calls[0]
    assert isinstance(adapter, release_adapter.Stage7ReleaseAdapter)
    assert code_root == tmp_path.resolve()
    assert kwargs == {
        "run_id": "release-fixed",
        "publish": True,
        "options": release_adapter.ValidationReleaseOptions(publish=True),
    }


@pytest.mark.parametrize(
    ("publish", "shared_message", "legacy_message"),
    [
        (
            False,
            "current inputs differ from certified runs",
            "current validation inputs differ from certified runs",
        ),
        (
            False,
            "inputs changed during release preparation",
            "validation inputs changed while writing lineage",
        ),
        (
            True,
            "current code identity differs from certified runs",
            "code identity changed during validation publication",
        ),
        (
            True,
            "code identity changed during release preparation",
            "code identity changed during validation publication",
        ),
        (
            True,
            "current inputs differ from certified runs",
            "current validation inputs differ from certified runs",
        ),
        (
            True,
            "inputs changed during release preparation",
            "validation inputs changed during validation publication",
        ),
        (
            True,
            "validation predecessor changed during publication",
            "validation predecessor changed during publication",
        ),
    ],
)
def test_stage7_release_facades_preserve_shared_failure_messages(
    tmp_path: Path,
    monkeypatch,
    publish: bool,
    shared_message: str,
    legacy_message: str,
) -> None:
    monkeypatch.setattr(pipeline, "code_identity", lambda root: FIXED_CODE)

    def fail_release(*args, **kwargs):
        raise ValueError(shared_message)

    monkeypatch.setattr(lifecycle, "release", fail_release)

    with pytest.raises(ValueError, match=f"^{legacy_message}$"):
        pipeline.run_validation_release(
            tmp_path,
            publish=publish,
            run_id="release-fixed",
        )
