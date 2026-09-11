from __future__ import annotations

import hashlib
import inspect
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from ashare_multifactor.cli import validation as validation_cli
from ashare_multifactor.validation import pipeline

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
VALIDATION_CORE_PATHS_SHA256 = (
    "3a9d9634313088521a74b412b986810160c79d6c02a47dbb72ca586f22869f31"
)
OBSERVED_VALIDATION_CALL_SURFACE = {
    "assert_validation_outputs_sealed": ("(root: 'Path') -> 'None'", "None"),
    "assert_validation_runtime_gates": (
        "(work: 'Path', candidates: 'tuple[str, ...]') -> 'None'",
        "None",
    ),
    "assert_validation_upstream_releases": (
        "(data_root: 'Path') -> 'None'",
        "None",
    ),
    "certify_full_validation_runs": (
        (
            "(data_root: 'Path', first_run_id: 'str', second_run_id: 'str') "
            "-> 'dict[str, object]'"
        ),
        "dict[str, object]",
    ),
    "compare_validation_outputs": (
        "(first: 'Path', second: 'Path', relative_paths: 'tuple[str, ...]') -> 'None'",
        "None",
    ),
    "execute_full_validation_run": (
        "(code_root: 'Path', *, run_id: 'str | None' = None) -> 'Path'",
        "Path",
    ),
    "execute_validation_reproducibility": (
        (
            "(code_root: 'Path', *, run_id_prefix: 'str | None' = None) "
            "-> 'dict[str, object]'"
        ),
        "dict[str, object]",
    ),
    "finalize_validation_run": (
        (
            "(run_root: 'Path', *, run_id: 'str', identity: 'dict[str, object]', "
            "inputs: 'dict[str, object]', elapsed_seconds: 'float', "
            "maximum_rss_bytes: 'int') -> 'dict[str, object]'"
        ),
        "dict[str, object]",
    ),
    "resolve_validation_data_root": ("(root: 'Path') -> 'Path'", "Path"),
    "resolve_validation_predecessor": (
        "(data_root: 'Path') -> 'dict[str, str] | None'",
        "dict[str, str] | None",
    ),
    "run_validation_release": (
        (
            "(code_root: 'Path', *, publish: 'bool', run_id: 'str | None' = None) "
            "-> 'Path | PublishedRelease'"
        ),
        "Path | PublishedRelease",
    ),
    "stage_validation_release": (
        (
            "(code_root: 'Path', *, run_id: 'str | None' = None, certified: "
            "'dict[str, object] | None' = None) -> 'Path'"
        ),
        "Path",
    ),
    "validate_run_id": ("(run_id: 'str') -> 'str'", "str"),
}


def _json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()


def _core_bytes(relative: str) -> bytes:
    return f"fixed-stage7:{relative}\n".encode()


def _write_validation_core(root: Path) -> None:
    for relative in pipeline.CORE_VALIDATION_FILES:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(_core_bytes(relative))


def _expected_manifest(run_id: str, *, dirty: bool = False) -> dict[str, object]:
    identity = dict(FIXED_IDENTITY, dirty=dirty)
    return {
        "run_id": run_id,
        "code": identity,
        "inputs": FIXED_INPUTS,
        "core_file_count": 98,
        "files": [
            {
                "path": relative,
                "sha256": hashlib.sha256(_core_bytes(relative)).hexdigest(),
                "size": len(_core_bytes(relative)),
            }
            for relative in pipeline.CORE_VALIDATION_FILES
        ],
        "resource_usage": {
            "elapsed_seconds": 1.25,
            "maximum_rss_bytes": 2048,
        },
    }


def _finalize_pair(
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    dirty: bool = False,
) -> tuple[Path, Path]:
    monkeypatch.setattr(pipeline, "assert_validation_outputs_sealed", lambda root: None)
    runs = data_root / "artifacts/validation_evaluation/full_runs"
    roots = (runs / "stage7_fixed_1", runs / "stage7_fixed_2")
    for root in roots:
        _write_validation_core(root)
        pipeline.finalize_validation_run(
            root,
            run_id=root.name,
            identity=dict(FIXED_IDENTITY, dirty=dirty),
            inputs=FIXED_INPUTS,
            elapsed_seconds=1.25,
            maximum_rss_bytes=2048,
        )
    return roots


def _staged_path(relative: str) -> str:
    if relative.startswith("datasets/"):
        return "datasets/inputs/" + relative.removeprefix("datasets/")
    if relative.startswith("artifacts/"):
        return "artifacts/research/" + relative.removeprefix("artifacts/")
    _, candidate, kind, name = relative.split("/", 3)
    destination_root = "datasets" if kind == "datasets" else "artifacts"
    return f"{destination_root}/backtests/{candidate}/{name}"


def test_validation_observed_call_surface_and_core_paths_are_frozen() -> None:
    actual = {}
    for name in OBSERVED_VALIDATION_CALL_SURFACE:
        function = getattr(pipeline, name)
        actual[name] = (
            str(inspect.signature(function)),
            str(inspect.get_annotations(function, eval_str=False)["return"]),
        )

    assert actual == OBSERVED_VALIDATION_CALL_SURFACE
    assert len(pipeline.CORE_VALIDATION_FILES) == 98
    assert len(set(pipeline.CORE_VALIDATION_FILES)) == 98
    assert hashlib.sha256(
        "\n".join(pipeline.CORE_VALIDATION_FILES).encode()
    ).hexdigest() == VALIDATION_CORE_PATHS_SHA256


def test_stage7_manifest_and_certificate_bytes_are_frozen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = _finalize_pair(tmp_path, monkeypatch)
    for root in (first, second):
        expected = _expected_manifest(root.name)
        assert (root / "run_manifest.json").read_bytes() == _json_bytes(expected)

    result = pipeline.certify_full_validation_runs(
        tmp_path, first.name, second.name
    )
    expected_manifests = [_expected_manifest(first.name), _expected_manifest(second.name)]
    expected_certificate = {
        "scope": "full_pipeline",
        "run_ids": [first.name, second.name],
        "core_file_count": 98,
        "outputs_identical": True,
        "full_pipeline_reproducible": True,
        "release_eligible": True,
        "reason": None,
        "code": FIXED_IDENTITY,
        "inputs": FIXED_INPUTS,
        "core_hashes": {
            item["path"]: item["sha256"] for item in expected_manifests[1]["files"]
        },
        "run_manifest_sha256": [
            hashlib.sha256(_json_bytes(manifest)).hexdigest()
            for manifest in expected_manifests
        ],
    }

    assert result == expected_certificate
    certificate = (
        tmp_path / "processed/validation_evaluation/full_reproducibility.json"
    )
    assert certificate.read_bytes() == _json_bytes(expected_certificate)


def test_stage7_dirty_identity_is_reproducible_but_not_release_eligible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = _finalize_pair(tmp_path, monkeypatch, dirty=True)

    result = pipeline.certify_full_validation_runs(tmp_path, first.name, second.name)

    assert result["outputs_identical"] is True
    assert result["full_pipeline_reproducible"] is False
    assert result["release_eligible"] is False
    assert result["reason"] == "git_identity_is_dirty"


@pytest.mark.parametrize(
    ("field", "changed", "message"),
    [
        ("inputs", {"changed": True}, "validation runs used different frozen inputs"),
        ("code", {"dirty": False}, "validation runs used different code identities"),
    ],
)
def test_stage7_certification_drift_errors_are_frozen(
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
        pipeline.certify_full_validation_runs(tmp_path, first.name, second.name)


def test_stage7_source_drift_missing_file_and_distinct_run_errors_are_frozen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = _finalize_pair(tmp_path, monkeypatch)
    changed = pipeline.CORE_VALIDATION_FILES[0]
    (second / changed).write_bytes(b"changed\n")
    with pytest.raises(
        ValueError,
        match=rf"^validation run manifest hash mismatch: {second.name}/{changed}$",
    ):
        pipeline.certify_full_validation_runs(tmp_path, first.name, second.name)

    (second / changed).unlink()
    with pytest.raises(FileNotFoundError, match=f"^{changed}$"):
        pipeline.compare_validation_outputs(first, second, (changed,))

    with pytest.raises(
        ValueError,
        match="^full validation reproducibility requires distinct runs$",
    ):
        pipeline.certify_full_validation_runs(tmp_path, first.name, first.name)


def test_stage7_closed_tree_symlinks_extra_files_and_missing_files_are_frozen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(
        FileNotFoundError,
        match="^validation run is incomplete: datasets/factor_features.parquet",
    ):
        pipeline.finalize_validation_run(
            empty,
            run_id="empty",
            identity=FIXED_IDENTITY,
            inputs=FIXED_INPUTS,
            elapsed_seconds=1.25,
            maximum_rss_bytes=2048,
        )

    target = tmp_path / "target"
    target.mkdir()
    root_link = tmp_path / "root-link"
    root_link.symlink_to(target, target_is_directory=True)
    with pytest.raises(
        ValueError, match="^validation run root must not be a symlink$"
    ):
        pipeline.finalize_validation_run(
            root_link,
            run_id="root-link",
            identity=FIXED_IDENTITY,
            inputs=FIXED_INPUTS,
            elapsed_seconds=1.25,
            maximum_rss_bytes=2048,
        )

    nested_link = target / "linked-file"
    nested_link.symlink_to(tmp_path / "outside")
    with pytest.raises(
        ValueError, match="^validation run must not contain symlinks$"
    ):
        pipeline.finalize_validation_run(
            target,
            run_id="target",
            identity=FIXED_IDENTITY,
            inputs=FIXED_INPUTS,
            elapsed_seconds=1.25,
            maximum_rss_bytes=2048,
        )

    nested_link.unlink()
    extra = target / "extra.txt"
    extra.write_text("extra\n")
    with pytest.raises(
        ValueError,
        match="^validation run contains unexpected files: extra.txt$",
    ):
        pipeline.finalize_validation_run(
            target,
            run_id="target",
            identity=FIXED_IDENTITY,
            inputs=FIXED_INPUTS,
            elapsed_seconds=1.25,
            maximum_rss_bytes=2048,
        )

    full = tmp_path / "full"
    _write_validation_core(full)
    (full / "extra.txt").write_text("extra\n")
    monkeypatch.setattr(pipeline, "assert_validation_outputs_sealed", lambda root: None)
    with pytest.raises(ValueError, match="unexpected files: extra.txt"):
        pipeline.finalize_validation_run(
            full,
            run_id="full",
            identity=FIXED_IDENTITY,
            inputs=FIXED_INPUTS,
            elapsed_seconds=1.25,
            maximum_rss_bytes=2048,
        )


def test_stage7_safe_slug_and_duplicate_run_directory_errors_are_frozen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert pipeline.validate_run_id("Stage7_safe-1") == "Stage7_safe-1"
    assert pipeline.validate_run_id("a" * 128) == "a" * 128
    for invalid in ("", "../escape", "with space", "a/b", "a" * 129):
        with pytest.raises(
            ValueError, match="^validation run ID must be a safe slug$"
        ):
            pipeline.validate_run_id(invalid)

    monkeypatch.setattr(pipeline, "resolve_validation_data_root", lambda root: tmp_path)
    monkeypatch.setattr(pipeline, "assert_validation_upstream_releases", lambda root: None)
    monkeypatch.setattr(pipeline, "code_identity", lambda root: FIXED_IDENTITY)
    monkeypatch.setattr(
        pipeline,
        "_validation_input_identity",
        lambda code, data: FIXED_INPUTS,
    )
    duplicate = tmp_path / "artifacts/validation_evaluation/full_runs/duplicate"
    duplicate.mkdir(parents=True)
    with pytest.raises(FileExistsError) as error:
        pipeline.execute_full_validation_run(tmp_path, run_id="duplicate")
    assert error.value.args == (duplicate,)


def test_stage7_dirty_release_fails_before_adapter_binding(
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
        "_stage7_release_adapter",
        lambda: pytest.fail("dirty identity must fail before adapter binding"),
    )

    with pytest.raises(
        ValueError,
        match="^validation release requires a clean Git identity$",
    ):
        pipeline.run_validation_release(
            tmp_path,
            publish=True,
            run_id="release-fixed",
        )


@pytest.mark.parametrize(
    ("drift", "message"),
    [
        ("code", "code identity changed during validation run"),
        ("input", "frozen inputs changed during validation run"),
    ],
)
def test_stage7_execution_drift_cleans_new_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
    message: str,
) -> None:
    monkeypatch.setattr(pipeline, "resolve_validation_data_root", lambda root: tmp_path)
    monkeypatch.setattr(pipeline, "assert_validation_upstream_releases", lambda root: None)
    monkeypatch.setattr(
        "ashare_multifactor.validation.full_run.execute_validation_stages",
        lambda code_root, data_root, run_root: None,
    )
    identities = iter(
        [FIXED_IDENTITY, {**FIXED_IDENTITY, "commit": "e" * 40}]
        if drift == "code"
        else [FIXED_IDENTITY, FIXED_IDENTITY]
    )
    inputs = iter(
        [FIXED_INPUTS, FIXED_INPUTS]
        if drift == "code"
        else [FIXED_INPUTS, {"changed": True}]
    )
    monkeypatch.setattr(pipeline, "code_identity", lambda root: next(identities))
    monkeypatch.setattr(
        pipeline,
        "_validation_input_identity",
        lambda code_root, data_root: next(inputs),
    )

    run_root = tmp_path / "artifacts/validation_evaluation/full_runs/drift"
    with pytest.raises(ValueError, match=f"^{message}$"):
        pipeline.execute_full_validation_run(tmp_path, run_id="drift")
    assert not run_root.exists()


def test_stage7_publish_false_mapping_and_lineage_shape_are_frozen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    staged = tmp_path / "staged"
    _write_validation_core(source)
    hashes = {
        relative: hashlib.sha256(_core_bytes(relative)).hexdigest()
        for relative in pipeline.CORE_VALIDATION_FILES
    }
    pipeline._copy_certified_core(source, staged, hashes)

    actual = {
        path.relative_to(staged).as_posix()
        for path in staged.rglob("*")
        if path.is_file()
    }
    assert actual == {_staged_path(path) for path in pipeline.CORE_VALIDATION_FILES}
    for relative in pipeline.CORE_VALIDATION_FILES:
        assert (staged / _staged_path(relative)).read_bytes() == _core_bytes(relative)

    class FixedDateTime:
        @classmethod
        def now(cls, tz: object) -> datetime:
            return datetime(2026, 8, 23, 12, 0, tzinfo=UTC)

    def fake_record(path: Path, *, root: Path, role: str) -> SimpleNamespace:
        return SimpleNamespace(
            to_dict=lambda: {
                "path": path.relative_to(root).as_posix(),
                "role": role,
                "sha256": "f" * 64,
                "size_bytes": 1,
            }
        )

    stage_five = SimpleNamespace(
        manifest=tmp_path / "processed/factor_combination/release/manifest.json",
        lineage=tmp_path / "processed/factor_combination/release/lineage.json",
    )
    stage_six = SimpleNamespace(
        manifest=tmp_path / "processed/formal_backtest/release/manifest.json",
        lineage=tmp_path / "processed/formal_backtest/release/lineage.json",
    )
    resolved = iter([stage_five, stage_six])
    monkeypatch.setattr(pipeline, "datetime", FixedDateTime)
    monkeypatch.setattr(pipeline, "file_record", fake_record)
    monkeypatch.setattr(pipeline, "resolve_current", lambda root: next(resolved))
    monkeypatch.setattr(pipeline, "code_identity", lambda root: FIXED_IDENTITY)
    code_root = tmp_path / "code"
    data_root = tmp_path
    lineage = pipeline._validation_lineage(
        code_root,
        data_root,
        ("candidate",),
        reproducibility={"release_eligible": True},
        predecessor={
            "run_id": "old",
            "manifest_sha256": "1" * 64,
            "lineage_sha256": "2" * 64,
        },
    )
    assert list(lineage) == [
        "created_at",
        "period",
        "sealed_final_test_start",
        "candidates",
        "upstream",
        "configuration",
        "corporate_action_sources",
        "code",
        "reproducibility",
        "supersedes",
        "correction_reason",
    ]
    assert lineage["created_at"] == "2026-08-23T12:00:00+00:00"
    assert lineage["period"] == ["2017-01-01", "2021-12-31"]
    assert lineage["sealed_final_test_start"] == "2022-01-01"
    assert len(lineage["upstream"]) == 7
    assert len(lineage["configuration"]) == 2
    assert len(lineage["corporate_action_sources"]) == 8

    monkeypatch.setattr(pipeline, "stage_validation_release", lambda *args, **kwargs: staged)
    monkeypatch.setattr(
        pipeline,
        "publish_release",
        lambda *args, **kwargs: pytest.fail("publish=false must not publish"),
    )
    assert pipeline.run_validation_release(
        code_root, publish=False, run_id="staged"
    ) == staged


@pytest.mark.parametrize(
    ("command", "expected_publish", "printed"),
    [
        ("stage", False, "/fixed/stage\n"),
        ("publish", True, "published\n"),
    ],
)
def test_validation_cli_stage_and_publish_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    expected_publish: bool,
    printed: str,
) -> None:
    calls = []
    result: object = Path("/fixed/stage") if command == "stage" else "published"
    monkeypatch.setattr(
        validation_cli,
        "run_validation_release",
        lambda root, *, publish, run_id: calls.append((root, publish, run_id)) or result,
    )
    monkeypatch.setattr(
        "sys.argv",
        ["validation", command, "--root", str(tmp_path), "--run-id", "fixed"],
    )

    validation_cli.main()

    assert calls == [(tmp_path, expected_publish, "fixed")]
    assert capsys.readouterr().out == printed


def test_validation_cli_run_reproduce_and_required_command_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = []
    monkeypatch.setattr(
        validation_cli,
        "execute_full_validation_run",
        lambda root, *, run_id: calls.append(("run", root, run_id))
        or Path("/fixed/run"),
    )
    monkeypatch.setattr(
        validation_cli,
        "execute_validation_reproducibility",
        lambda root, *, run_id_prefix: calls.append(
            ("reproduce", root, run_id_prefix)
        )
        or {"status": "fixed"},
    )

    monkeypatch.setattr(
        "sys.argv",
        ["validation", "run", "--root", str(tmp_path), "--run-id", "run-id"],
    )
    validation_cli.main()
    assert capsys.readouterr().out == "/fixed/run\n"

    monkeypatch.setattr(
        "sys.argv",
        ["validation", "reproduce", "--root", str(tmp_path), "--run-id", "pair"],
    )
    validation_cli.main()
    assert capsys.readouterr().out == "{'status': 'fixed'}\n"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["validation", "run"])
    validation_cli.main()
    assert capsys.readouterr().out == "/fixed/run\n"
    assert calls == [
        ("run", tmp_path, "run-id"),
        ("reproduce", tmp_path, "pair"),
        ("run", tmp_path, None),
    ]

    monkeypatch.setattr("sys.argv", ["validation"])
    with pytest.raises(SystemExit) as error:
        validation_cli.main()
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert "the following arguments are required: command" in captured.err
    assert "{run,reproduce,stage,publish}" in captured.err
