from __future__ import annotations

import hashlib
import inspect
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from ashare_multifactor.audit import reproducible_release as lifecycle
from ashare_multifactor.cli import robustness as robustness_cli
from ashare_multifactor.robustness import pipeline

FIXED_IDENTITY: dict[str, object] = {
    "commit": "a" * 40,
    "dependencies": {"numpy": "2.5.1"},
    "diff_sha256": "c" * 64,
    "dirty": False,
    "python": "3.14.6",
    "sources": [],
    "tree": "b" * 40,
}
FIXED_INPUTS: dict[str, object] = {
    "frozen_input": {"sha256": "d" * 64, "size": 123}
}
ROBUSTNESS_CORE_PATHS_SHA256 = (
    "8eea1cb33977da93c0c0927f3024caf9bd5f8982d99172f62c06f29e3b18b8e7"
)
OBSERVED_ROBUSTNESS_CALL_SURFACE = {
    "_assert_validation_market_scope": (
        "(validation: 'object', supported_markets: 'tuple[str, ...]') -> 'None'",
        "None",
    ),
    "_successor_release_contract": (
        (
            "(code_root: 'Path', data_root: 'Path', action_audit_root: 'Path', "
            "collector_readiness_root: 'Path') -> 'dict[str, object]'"
        ),
        "dict[str, object]",
    ),
    "assert_robustness_outputs_sealed": ("(root: 'Path') -> 'None'", "None"),
    "compare_robustness_runs": (
        "(first: 'Path', second: 'Path') -> 'dict[str, Any]'",
        "dict[str, Any]",
    ),
    "execute_robustness_reproducibility": (
        (
            "(code_root: 'Path', *, run_id_prefix: 'str | None' = None) "
            "-> 'dict[str, Any]'"
        ),
        "dict[str, Any]",
    ),
    "execute_robustness_run": (
        "(code_root: 'Path', *, run_id: 'str | None' = None) -> 'Path'",
        "Path",
    ),
    "finalize_robustness_run": (
        (
            "(run_root: 'Path', *, run_id: 'str', identity: 'dict[str, object]', "
            "inputs: 'dict[str, object] | None' = None) -> 'dict[str, Any]'"
        ),
        "dict[str, Any]",
    ),
    "publish_robustness_release": (
        (
            "(code_root: 'Path', *, run_id: 'str', successor_audit_root: "
            "'Path | None' = None, collector_readiness_root: 'Path | None' = None) "
            "-> 'object'"
        ),
        "object",
    ),
    "verify_reproducible_source": (
        "(source: 'Path', reproducibility: 'dict[str, Any]') -> 'None'",
        "None",
    ),
}


def _json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()


def _core_bytes(relative: str) -> bytes:
    return f"fixed-stage8:{relative}\n".encode()


def _write_robustness_core(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for relative in pipeline.CORE_ROBUSTNESS_FILES:
        (root / relative).write_bytes(_core_bytes(relative))


def _expected_manifest(run_id: str, *, dirty: bool = False) -> dict[str, object]:
    return {
        "run_id": run_id,
        "code": dict(FIXED_IDENTITY, dirty=dirty),
        "inputs": FIXED_INPUTS,
        "core_file_count": 3,
        "files": [
            {
                "path": relative,
                "sha256": hashlib.sha256(_core_bytes(relative)).hexdigest(),
                "size": len(_core_bytes(relative)),
            }
            for relative in pipeline.CORE_ROBUSTNESS_FILES
        ],
    }


def _expected_comparison(*, dirty: bool = False) -> dict[str, object]:
    return {
        "outputs_identical": True,
        "core_file_count": 3,
        "core_hashes": {
            relative: hashlib.sha256(_core_bytes(relative)).hexdigest()
            for relative in pipeline.CORE_ROBUSTNESS_FILES
        },
        "run_ids": ["stage8_fixed_1", "stage8_fixed_2"],
        "code": dict(FIXED_IDENTITY, dirty=dirty),
        "inputs": FIXED_INPUTS,
    }


def _finalize_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    dirty: bool = False,
) -> tuple[Path, Path]:
    monkeypatch.setattr(pipeline, "assert_robustness_outputs_sealed", lambda root: None)
    roots = (
        tmp_path / "stage8_fixed_1",
        tmp_path / "stage8_fixed_2",
    )
    for root in roots:
        _write_robustness_core(root)
        pipeline.finalize_robustness_run(
            root,
            run_id=root.name,
            identity=dict(FIXED_IDENTITY, dirty=dirty),
            inputs=FIXED_INPUTS,
        )
    return roots


def _mock_execution_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    identities: list[dict[str, object]],
    inputs: list[dict[str, object]],
) -> None:
    validation_artifacts = tmp_path / "validation-artifacts"
    decision = validation_artifacts / "research/selection_decision.json"
    decision.parent.mkdir(parents=True, exist_ok=True)
    decision.write_text('{"selected_candidate":"main"}\n')
    validation = SimpleNamespace(
        run_id="validation-fixed",
        artifacts=validation_artifacts,
    )
    protocol = SimpleNamespace(
        validation_release="validation-fixed",
        main_candidate="main",
        analysis_start=date(2005, 1, 1),
        analysis_end=date(2021, 12, 31),
    )
    identity_values = iter(identities)
    input_values = iter(inputs)
    monkeypatch.setattr(pipeline, "resolve_robustness_data_root", lambda root: tmp_path)
    monkeypatch.setattr(pipeline, "load_robustness_protocol", lambda path: protocol)
    monkeypatch.setattr(pipeline, "resolve_current", lambda root: validation)
    monkeypatch.setattr(pipeline, "_assert_input_period_contracts", lambda *args: None)
    monkeypatch.setattr(pipeline, "assert_robustness_read_allowed", lambda *args: None)
    monkeypatch.setattr(pipeline, "code_identity", lambda root: next(identity_values))
    monkeypatch.setattr(
        pipeline,
        "_robustness_input_identity",
        lambda code_root, data_root, release: next(input_values),
    )
    monkeypatch.setattr(pipeline, "_execute_experiments", lambda *args: ([], []))
    monkeypatch.setattr(
        pipeline,
        "build_robustness_long_table",
        lambda results, active_protocol: pl.DataFrame({"value": [1]}),
    )
    monkeypatch.setattr(
        pipeline,
        "assess_test_protocol_gate",
        lambda table, **kwargs: {"sealed_test_protocol_allowed": True},
    )
    monkeypatch.setattr(
        pipeline,
        "render_robustness_report",
        lambda table, gate, active_protocol: "fixed report\n",
    )


def test_robustness_observed_call_surface_and_core_paths_are_frozen() -> None:
    actual = {}
    for name in OBSERVED_ROBUSTNESS_CALL_SURFACE:
        function = getattr(pipeline, name)
        actual[name] = (
            str(inspect.signature(function)),
            str(inspect.get_annotations(function, eval_str=False)["return"]),
        )

    assert actual == OBSERVED_ROBUSTNESS_CALL_SURFACE
    assert pipeline.CORE_ROBUSTNESS_FILES == (
        "robustness_results.parquet",
        "report.md",
        "protocol_gate.json",
    )
    assert hashlib.sha256(
        "\n".join(pipeline.CORE_ROBUSTNESS_FILES).encode()
    ).hexdigest() == ROBUSTNESS_CORE_PATHS_SHA256


def test_stage8_manifest_and_certificate_bytes_are_frozen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = _finalize_pair(tmp_path, monkeypatch)
    for root in (first, second):
        expected = _expected_manifest(root.name)
        assert (root / "run_manifest.json").read_bytes() == _json_bytes(expected)
        assert "resource_usage" not in expected
    run_base = tmp_path / "artifacts/robustness/full_runs"
    run_base.mkdir(parents=True)
    moved = (run_base / first.name, run_base / second.name)
    first.rename(moved[0])
    second.rename(moved[1])
    first, second = moved

    runs = iter([first, second])
    adapter = SimpleNamespace(
        bind=lambda code_root, *, run_id: SimpleNamespace(
            profile="stage8_v1",
            data_root=tmp_path,
        )
    )
    monkeypatch.setattr(
        lifecycle, "run_once", lambda adapter, code_root, *, run_id: next(runs)
    )
    monkeypatch.setattr(pipeline, "_stage8_release_adapter", lambda: adapter)
    result = pipeline.execute_robustness_reproducibility(
        tmp_path, run_id_prefix="stage8_fixed"
    )
    expected = {
        **_expected_comparison(),
        "release_eligible": True,
        "reason": None,
    }

    assert result == expected
    assert (
        tmp_path / "processed/robustness/reproducibility.json"
    ).read_bytes() == _json_bytes(expected)


def test_stage8_dirty_identity_is_identical_but_not_release_eligible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = _finalize_pair(tmp_path, monkeypatch, dirty=True)
    run_base = tmp_path / "artifacts/robustness/full_runs"
    run_base.mkdir(parents=True)
    moved = (run_base / first.name, run_base / second.name)
    first.rename(moved[0])
    second.rename(moved[1])
    first, second = moved
    runs = iter([first, second])
    adapter = SimpleNamespace(
        bind=lambda code_root, *, run_id: SimpleNamespace(
            profile="stage8_v1",
            data_root=tmp_path,
        )
    )
    monkeypatch.setattr(
        lifecycle, "run_once", lambda adapter, code_root, *, run_id: next(runs)
    )
    monkeypatch.setattr(pipeline, "_stage8_release_adapter", lambda: adapter)

    result = pipeline.execute_robustness_reproducibility(
        tmp_path, run_id_prefix="stage8_fixed"
    )

    assert result["outputs_identical"] is True
    assert result["release_eligible"] is False
    assert result["reason"] == "git_identity_is_dirty"


@pytest.mark.parametrize(
    ("field", "changed", "message"),
    [
        ("code", {"dirty": False}, "robustness runs used different code identities"),
        ("inputs", {"changed": True}, "robustness runs used different inputs"),
    ],
)
def test_stage8_comparison_drift_errors_are_frozen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    changed: dict[str, object],
    message: str,
) -> None:
    first, second = _finalize_pair(tmp_path, monkeypatch)
    manifest_path = second / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest[field] = changed
    manifest_path.write_bytes(_json_bytes(manifest))

    with pytest.raises(ValueError, match=f"^{message}$"):
        pipeline.compare_robustness_runs(first, second)


def test_stage8_output_drift_and_missing_file_errors_are_frozen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = _finalize_pair(tmp_path, monkeypatch)
    (second / "report.md").write_text("changed\n")
    with pytest.raises(ValueError, match="^robustness runs differ: report.md$"):
        pipeline.compare_robustness_runs(first, second)

    missing = tmp_path / "missing"
    missing.mkdir()
    with pytest.raises(
        FileNotFoundError,
        match=(
            "^robustness run is incomplete: robustness_results.parquet, "
            "report.md, protocol_gate.json$"
        ),
    ):
        pipeline.finalize_robustness_run(
            missing,
            run_id="missing",
            identity=FIXED_IDENTITY,
            inputs=FIXED_INPUTS,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("run_id", "reproducible source run identity changed"),
        ("code", "reproducible source code identity changed"),
        ("inputs", "reproducible source input identity changed"),
        ("hash", "reproducible source hash changed: report.md"),
    ],
)
def test_stage8_certified_source_drift_errors_are_frozen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    _first, second = _finalize_pair(tmp_path, monkeypatch)
    reproducibility = _expected_comparison()
    manifest_path = second / "run_manifest.json"
    if mutation == "hash":
        (second / "report.md").write_text("changed\n")
    else:
        manifest = json.loads(manifest_path.read_text())
        manifest[mutation] = "changed" if mutation == "run_id" else {"changed": True}
        manifest_path.write_bytes(_json_bytes(manifest))

    with pytest.raises(ValueError, match=f"^{message}$"):
        pipeline.verify_reproducible_source(second, reproducibility)


def test_stage8_extra_files_and_loose_run_ids_remain_allowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loose = tmp_path / "loose"
    _write_robustness_core(loose)
    (loose / "extra.txt").write_text("extra\n")
    (loose / "nested").mkdir()
    monkeypatch.setattr(pipeline, "assert_robustness_outputs_sealed", lambda root: None)
    manifest = pipeline.finalize_robustness_run(
        loose,
        run_id="../legacy run/id",
        identity=FIXED_IDENTITY,
        inputs=FIXED_INPUTS,
    )
    assert manifest["run_id"] == "../legacy run/id"
    assert (loose / "extra.txt").is_file()

    _mock_execution_dependencies(
        tmp_path,
        monkeypatch,
        identities=[FIXED_IDENTITY, FIXED_IDENTITY],
        inputs=[FIXED_INPUTS, FIXED_INPUTS],
    )
    run = pipeline.execute_robustness_run(tmp_path, run_id="nested/legacy run")
    assert run == tmp_path / "artifacts/robustness/full_runs/nested/legacy run"
    assert json.loads((run / "run_manifest.json").read_text())["run_id"] == (
        "nested/legacy run"
    )


def test_stage8_duplicate_run_directory_error_is_frozen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_execution_dependencies(
        tmp_path,
        monkeypatch,
        identities=[FIXED_IDENTITY],
        inputs=[FIXED_INPUTS],
    )
    duplicate = tmp_path / "artifacts/robustness/full_runs/duplicate"
    duplicate.mkdir(parents=True)

    with pytest.raises(FileExistsError) as error:
        pipeline.execute_robustness_run(tmp_path, run_id="duplicate")
    assert error.value.args == (duplicate,)


def test_stage8_dirty_release_fails_before_adapter_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pipeline,
        "code_identity",
        lambda root: {**FIXED_IDENTITY, "dirty": True},
    )
    monkeypatch.setattr(
        pipeline,
        "_stage8_release_adapter",
        lambda **kwargs: pytest.fail(
            "dirty identity must fail before adapter binding"
        ),
    )

    with pytest.raises(
        ValueError,
        match="^robustness release requires a clean Git identity$",
    ):
        pipeline.publish_robustness_release(
            tmp_path,
            run_id="release-fixed",
        )


@pytest.mark.parametrize(
    ("drift", "message"),
    [
        ("code", "code identity changed during robustness run"),
        ("input", "robustness inputs changed during run"),
    ],
)
def test_stage8_execution_drift_cleans_new_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
    message: str,
) -> None:
    _mock_execution_dependencies(
        tmp_path,
        monkeypatch,
        identities=(
            [FIXED_IDENTITY, {**FIXED_IDENTITY, "commit": "e" * 40}]
            if drift == "code"
            else [FIXED_IDENTITY, FIXED_IDENTITY]
        ),
        inputs=(
            [FIXED_INPUTS, FIXED_INPUTS]
            if drift == "code"
            else [FIXED_INPUTS, {"changed": True}]
        ),
    )
    run = tmp_path / "artifacts/robustness/full_runs/drift"

    with pytest.raises(ValueError, match=f"^{message}$"):
        pipeline.execute_robustness_run(tmp_path, run_id="drift")
    assert not run.exists()


def test_stage8_publication_preparation_mapping_and_lineage_are_frozen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "data"
    code_root = tmp_path / "code"
    source = data_root / "artifacts/robustness/full_runs/source_2"
    source.mkdir(parents=True)
    source_payloads = {
        "robustness_results.parquet": b"fixed results\n",
        "report.md": b"fixed report\n",
        "protocol_gate.json": b'{"sealed_test_protocol_allowed": true}\n',
    }
    for name, payload in source_payloads.items():
        (source / name).write_bytes(payload)
    monkeypatch.setattr(pipeline, "assert_robustness_outputs_sealed", lambda root: None)
    first_source = source.parent / "source_1"
    first_source.mkdir()
    for name, payload in source_payloads.items():
        (first_source / name).write_bytes(payload)
    pipeline.finalize_robustness_run(
        first_source,
        run_id=first_source.name,
        identity=FIXED_IDENTITY,
        inputs=FIXED_INPUTS,
    )
    manifest = pipeline.finalize_robustness_run(
        source,
        run_id=source.name,
        identity=FIXED_IDENTITY,
        inputs=FIXED_INPUTS,
    )
    core_hashes = {item["path"]: item["sha256"] for item in manifest["files"]}
    reproducibility = {
        "outputs_identical": True,
        "core_file_count": 3,
        "core_hashes": core_hashes,
        "run_ids": ["source_1", "source_2"],
        "code": FIXED_IDENTITY,
        "inputs": FIXED_INPUTS,
        "release_eligible": True,
        "reason": None,
    }
    reproduction_path = data_root / "processed/robustness/reproducibility.json"
    reproduction_path.parent.mkdir(parents=True)
    reproduction_path.write_bytes(_json_bytes(reproducibility))
    validation_current = data_root / "processed/validation_evaluation/CURRENT.json"
    validation_current.parent.mkdir(parents=True)
    validation_pointer = {
        "run_id": "validation-fixed",
        "manifest_sha256": "e" * 64,
    }
    validation_current.write_bytes(_json_bytes(validation_pointer))
    (code_root / "configs").mkdir(parents=True)
    (code_root / "configs/market_rules.yaml").write_text("fixed: true\n")
    template = code_root / "docs/templates/stage9-final-test-report-template.md"
    template.parent.mkdir(parents=True)
    template.write_text("fixed template\n")

    validation_artifacts = data_root / "validation-artifacts"
    decision = validation_artifacts / "research/selection_decision.json"
    decision.parent.mkdir(parents=True)
    decision.write_text('{"selected_candidate":"main"}\n')
    protocol = SimpleNamespace(
        validation_release="validation-fixed",
        main_candidate="main",
        analysis_start=date(2005, 1, 1),
        analysis_end=date(2021, 12, 31),
    )
    config = SimpleNamespace(supported_markets=("sh", "sz"))
    validation_release = SimpleNamespace(
        run_id="validation-fixed",
        artifacts=validation_artifacts,
    )
    captured = {}

    def fake_seal(destination: Path, **kwargs: object) -> dict[str, object]:
        sealed = {"sealed_protocol_sha256": "f" * 64}
        destination.write_bytes(_json_bytes(sealed))
        captured["seal_kwargs"] = kwargs
        return sealed

    def fake_publish(root: Path, **kwargs: object) -> SimpleNamespace:
        datasets = kwargs["staged_datasets"]
        artifacts = kwargs["staged_artifacts"]
        captured.update(root=root, **kwargs)
        captured["dataset_files"] = {
            path.relative_to(datasets).as_posix(): path.read_bytes()
            for path in datasets.rglob("*")
            if path.is_file()
        }
        captured["artifact_files"] = {
            path.relative_to(artifacts).as_posix(): path.read_bytes()
            for path in artifacts.rglob("*")
            if path.is_file()
        }
        return SimpleNamespace(root=tmp_path / "published")

    monkeypatch.setattr(pipeline, "resolve_robustness_data_root", lambda root: data_root)
    monkeypatch.setattr(pipeline, "code_identity", lambda root: FIXED_IDENTITY)
    monkeypatch.setattr(pipeline, "load_robustness_protocol", lambda path: protocol)
    monkeypatch.setattr(pipeline, "load_config", lambda path: config)
    monkeypatch.setattr(pipeline, "resolve_current", lambda root: validation_release)
    monkeypatch.setattr(pipeline, "_assert_input_period_contracts", lambda *args: None)
    monkeypatch.setattr(pipeline, "assert_robustness_read_allowed", lambda *args: None)
    monkeypatch.setattr(
        pipeline,
        "_robustness_input_identity",
        lambda code, data, validation: FIXED_INPUTS,
    )
    monkeypatch.setattr(pipeline, "_assert_validation_market_scope", lambda *args: None)
    monkeypatch.setattr(pipeline, "seal_test_protocol", fake_seal)
    monkeypatch.setattr(lifecycle, "publish_release", fake_publish)

    published = pipeline.publish_robustness_release(
        code_root,
        run_id="release-fixed",
    )

    assert published.root == tmp_path / "published"
    assert captured["root"] == data_root / "processed/robustness"
    assert captured["dataset_files"] == {
        "robustness_results.parquet": source_payloads["robustness_results.parquet"]
    }
    assert captured["artifact_files"] == {
        "protocol_gate.json": source_payloads["protocol_gate.json"],
        "report.md": source_payloads["report.md"],
        "sealed_test_protocol.json": _json_bytes(
            {"sealed_protocol_sha256": "f" * 64}
        ),
    }
    assert captured["manifest_metadata"] == {"stage": "robustness"}
    lineage = captured["lineage"]
    assert list(lineage) == [
        "stage",
        "period",
        "sealed_test_start",
        "main_candidate",
        "code",
        "inputs",
        "reproducibility",
        "sealed_protocol_sha256",
        "supported_markets",
    ]
    assert lineage["period"] == ["2005-01-01", "2021-12-31"]
    assert lineage["sealed_test_start"] == "2022-01-01"
    assert lineage["supported_markets"] == ["sh", "sz"]
    assert captured["seal_kwargs"]["validation_pointer"] == validation_pointer
    assert not (data_root / "processed/robustness/CURRENT.json").exists()


def test_robustness_cli_run_reproduce_and_publish_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = []
    monkeypatch.setattr(
        robustness_cli,
        "execute_robustness_run",
        lambda root, *, run_id: calls.append(("run", root, run_id))
        or Path("/fixed/run"),
    )
    monkeypatch.setattr(
        robustness_cli,
        "execute_robustness_reproducibility",
        lambda root, *, run_id_prefix: calls.append(
            ("reproduce", root, run_id_prefix)
        )
        or {"z": 1, "a": 2},
    )
    monkeypatch.setattr(
        robustness_cli,
        "publish_robustness_release",
        lambda root, *, run_id: calls.append(("publish", root, run_id))
        or SimpleNamespace(root=Path("/fixed/release")),
    )

    monkeypatch.setattr(
        "sys.argv",
        ["robustness", "run", "--root", str(tmp_path), "--run-id", "run-id"],
    )
    robustness_cli.main()
    assert capsys.readouterr().out == "/fixed/run\n"

    monkeypatch.setattr(
        "sys.argv",
        ["robustness", "reproduce", "--root", str(tmp_path), "--run-id", "pair"],
    )
    robustness_cli.main()
    assert capsys.readouterr().out == '{\n  "a": 2,\n  "z": 1\n}\n'

    monkeypatch.setattr(
        "sys.argv",
        ["robustness", "publish", "--root", str(tmp_path), "--run-id", "release"],
    )
    robustness_cli.main()
    assert capsys.readouterr().out == "/fixed/release\n"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["robustness", "run"])
    robustness_cli.main()
    assert capsys.readouterr().out == "/fixed/run\n"
    assert calls == [
        ("run", tmp_path, "run-id"),
        ("reproduce", tmp_path, "pair"),
        ("publish", tmp_path, "release"),
        ("run", tmp_path, None),
    ]

    monkeypatch.setattr("sys.argv", ["robustness", "publish"])
    with pytest.raises(SystemExit) as error:
        robustness_cli.main()
    assert error.value.code == 2
    assert "publish requires --run-id" in capsys.readouterr().err
