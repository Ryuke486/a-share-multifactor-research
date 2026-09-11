from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from ashare_multifactor.audit import reproducible_release as lifecycle
from ashare_multifactor.robustness import pipeline, release_adapter


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


def _protocol() -> SimpleNamespace:
    return SimpleNamespace(
        validation_release="validation-fixed",
        main_candidate="main",
        analysis_start=date(2005, 1, 1),
        analysis_end=date(2021, 12, 31),
    )


def _write_core(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    root.mkdir(parents=True, exist_ok=True)
    for relative in pipeline.CORE_ROBUSTNESS_FILES:
        payload = (
            b'{"sealed_test_protocol_allowed": true}\n'
            if relative == "protocol_gate.json"
            else f"stage8:{relative}\n".encode()
        )
        (root / relative).write_bytes(payload)
        hashes[relative] = hashlib.sha256(payload).hexdigest()
    return hashes


def _mock_binding_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, SimpleNamespace]:
    code_root = tmp_path / "code"
    data_root = tmp_path / "data"
    artifacts = data_root / "validation-artifacts"
    decision = artifacts / "research/selection_decision.json"
    decision.parent.mkdir(parents=True)
    decision.write_text('{"selected_candidate":"main"}\n')
    validation = SimpleNamespace(
        run_id="validation-fixed",
        artifacts=artifacts,
    )
    monkeypatch.setattr(
        pipeline,
        "resolve_robustness_data_root",
        lambda root: data_root,
    )
    monkeypatch.setattr(pipeline, "load_robustness_protocol", lambda path: _protocol())
    monkeypatch.setattr(pipeline, "resolve_current", lambda root: validation)
    monkeypatch.setattr(pipeline, "_assert_input_period_contracts", lambda *args: None)
    monkeypatch.setattr(pipeline, "assert_robustness_read_allowed", lambda *args: None)
    monkeypatch.setattr(pipeline, "code_identity", lambda root: FIXED_CODE)
    monkeypatch.setattr(
        pipeline,
        "_robustness_input_identity",
        lambda code, data, release: FIXED_INPUTS,
    )
    return code_root, validation


def test_stage8_adapter_bind_captures_the_legacy_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code_root, validation = _mock_binding_dependencies(tmp_path, monkeypatch)

    binding = release_adapter.Stage8ReleaseAdapter().bind(
        code_root,
        run_id="stage8-fixed",
    )

    assert binding.profile == "stage8_v1"
    assert binding.code_root == code_root.resolve()
    assert binding.data_root == tmp_path / "data"
    assert binding.run_id == "stage8-fixed"
    assert binding.code == FIXED_CODE
    assert binding.inputs == FIXED_INPUTS
    assert binding.protocol.main_candidate == "main"
    assert binding.validation is validation
    assert binding.successor_contract is None


def test_stage8_adapter_rejects_unpaired_successor_audit_roots(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ValueError,
        match="^Stage-8 successor audit roots are incomplete$",
    ):
        release_adapter.Stage8ReleaseAdapter(
            successor_audit_root=tmp_path / "action",
        )


def test_stage8_adapter_binds_paired_successor_audits_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code_root, _validation = _mock_binding_dependencies(tmp_path, monkeypatch)
    action = tmp_path / "action"
    collector = tmp_path / "collector"
    contract = {"predecessor": {"run_id": "previous"}}
    calls: list[tuple[Path, Path, Path, Path]] = []

    def successor(
        code: Path,
        data: Path,
        action_root: Path,
        collector_root: Path,
    ) -> dict[str, object]:
        calls.append((code, data, action_root, collector_root))
        return contract

    monkeypatch.setattr(pipeline, "_successor_release_contract", successor)
    adapter = release_adapter.Stage8ReleaseAdapter(
        successor_audit_root=action,
        collector_readiness_root=collector,
    )

    first = adapter.bind(code_root, run_id="first")
    second = adapter.bind(code_root, run_id="second")

    assert first.successor_contract == contract
    assert second.successor_contract == contract
    assert calls == [(code_root.resolve(), tmp_path / "data", action, collector)]


def test_stage8_adapter_execute_keeps_domain_calculation_and_manifest_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = release_adapter.RobustnessReleaseBinding(
        profile="stage8_v1",
        code_root=tmp_path / "code",
        data_root=tmp_path,
        run_id="stage8-fixed",
        code=FIXED_CODE,
        inputs=FIXED_INPUTS,
        protocol=_protocol(),
        validation=SimpleNamespace(),
        successor_contract=None,
    )
    calls: list[object] = []
    monkeypatch.setattr(
        pipeline,
        "_execute_experiments",
        lambda code, data, validation, protocol: ([], []),
    )
    monkeypatch.setattr(
        pipeline,
        "build_robustness_long_table",
        lambda results, protocol: pl.DataFrame({"value": [1]}),
    )
    monkeypatch.setattr(
        pipeline,
        "assess_test_protocol_gate",
        lambda table, **kwargs: {"sealed_test_protocol_allowed": True},
    )
    monkeypatch.setattr(
        pipeline,
        "render_robustness_report",
        lambda table, gate, protocol: "fixed report\n",
    )
    monkeypatch.setattr(
        pipeline,
        "assert_robustness_outputs_sealed",
        lambda root: calls.append(("sealed", root)),
    )
    run_root = tmp_path / "run"
    run_root.mkdir()

    result = release_adapter.Stage8ReleaseAdapter().execute(binding, run_root)

    assert result == {}
    assert calls == [("sealed", run_root)]
    assert (run_root / "robustness_results.parquet").is_file()
    assert json.loads((run_root / "protocol_gate.json").read_text()) == {
        "sealed_test_protocol_allowed": True
    }
    assert (run_root / "report.md").read_text() == "fixed report\n"


def test_stage8_adapter_execute_preserves_output_sealing_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = release_adapter.RobustnessReleaseBinding(
        profile="stage8_v1",
        code_root=tmp_path / "code",
        data_root=tmp_path,
        run_id="stage8-fixed",
        code=FIXED_CODE,
        inputs=FIXED_INPUTS,
        protocol=_protocol(),
        validation=SimpleNamespace(),
        successor_contract=None,
    )
    monkeypatch.setattr(pipeline, "_execute_experiments", lambda *args: ([], []))
    monkeypatch.setattr(
        pipeline,
        "build_robustness_long_table",
        lambda *args: pl.DataFrame({"value": [1]}),
    )
    monkeypatch.setattr(
        pipeline,
        "assess_test_protocol_gate",
        lambda *args, **kwargs: {"sealed_test_protocol_allowed": True},
    )
    monkeypatch.setattr(pipeline, "render_robustness_report", lambda *args: "report\n")
    monkeypatch.setattr(
        pipeline,
        "assert_robustness_outputs_sealed",
        lambda root: (_ for _ in ()).throw(ValueError("sealed output changed")),
    )
    run_root = tmp_path / "run"
    run_root.mkdir()

    with pytest.raises(ValueError, match="^sealed output changed$"):
        release_adapter.Stage8ReleaseAdapter().execute(binding, run_root)


def test_stage8_adapter_prepares_legacy_mapping_seal_and_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    hashes = _write_core(source)
    validation_current = tmp_path / "processed/validation_evaluation/CURRENT.json"
    validation_current.parent.mkdir(parents=True)
    validation_pointer = {
        "run_id": "validation-fixed",
        "manifest_sha256": "d" * 64,
    }
    validation_current.write_bytes(_json_bytes(validation_pointer))
    code_root = tmp_path / "code"
    (code_root / "configs").mkdir(parents=True)
    (code_root / "configs/market_rules.yaml").write_text("fixed: true\n")
    template = code_root / "docs/templates/stage9-final-test-report-template.md"
    template.parent.mkdir(parents=True)
    template.write_text("fixed template\n")
    binding = release_adapter.RobustnessReleaseBinding(
        profile="stage8_v1",
        code_root=code_root,
        data_root=tmp_path,
        run_id="release-fixed",
        code=FIXED_CODE,
        inputs=FIXED_INPUTS,
        protocol=_protocol(),
        validation=SimpleNamespace(run_id="validation-fixed"),
        successor_contract=None,
    )
    certificate = {
        "run_ids": ["pair_1", "pair_2"],
        "core_hashes": hashes,
        "code": FIXED_CODE,
        "inputs": FIXED_INPUTS,
    }
    config = SimpleNamespace(supported_markets=("sh", "sz"))
    captured: dict[str, object] = {}

    def fake_seal(destination: Path, **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        sealed = {"sealed_protocol_sha256": "e" * 64}
        destination.write_bytes(_json_bytes(sealed))
        return sealed

    monkeypatch.setattr(pipeline, "load_config", lambda path: config)
    monkeypatch.setattr(pipeline, "_assert_validation_market_scope", lambda *args: None)
    monkeypatch.setattr(pipeline, "seal_test_protocol", fake_seal)

    prepared = release_adapter.Stage8ReleaseAdapter().prepare_release(
        binding,
        source,
        certificate,
        run_id="release-fixed",
        options=None,
    )

    staged = tmp_path / "artifacts/robustness/release_staging/release-fixed"
    assert isinstance(prepared, lifecycle._ReleasePreparation)
    assert prepared.staged_root == staged
    assert prepared.publication_root == tmp_path / "processed/robustness"
    assert prepared.manifest_metadata == {"stage": "robustness"}
    assert (staged / "datasets/robustness_results.parquet").read_bytes() == (
        source / "robustness_results.parquet"
    ).read_bytes()
    assert (staged / "artifacts/report.md").read_bytes() == (
        source / "report.md"
    ).read_bytes()
    assert captured["validation_pointer"] == validation_pointer
    assert prepared.lineage == {
        "stage": "robustness",
        "period": ["2005-01-01", "2021-12-31"],
        "sealed_test_start": "2022-01-01",
        "main_candidate": "main",
        "code": FIXED_CODE,
        "inputs": FIXED_INPUTS,
        "reproducibility": certificate,
        "sealed_protocol_sha256": "e" * 64,
        "supported_markets": ["sh", "sz"],
    }

    action = tmp_path / "action-audit"
    collector = tmp_path / "collector-readiness"
    action.mkdir()
    collector.mkdir()
    (action / "manifest.json").write_text("action\n")
    (collector / "manifest.json").write_text("collector\n")
    successor_contract = {
        "predecessor": {
            "run_id": "previous",
            "manifest_sha256": "a" * 64,
            "reason": "fixed successor",
            "status": "superseded_for_final_execution",
        },
        "action_source_contract_sha256": "b" * 64,
        "action_coverage_audit_sha256": "c" * 64,
        "collector_readiness_audit_sha256": "d" * 64,
    }
    successor_binding = release_adapter.RobustnessReleaseBinding(
        profile=binding.profile,
        code_root=binding.code_root,
        data_root=binding.data_root,
        run_id="successor-fixed",
        code=binding.code,
        inputs=binding.inputs,
        protocol=binding.protocol,
        validation=binding.validation,
        successor_contract=successor_contract,
    )
    successor = release_adapter.Stage8ReleaseAdapter(
        successor_audit_root=action,
        collector_readiness_root=collector,
    ).prepare_release(
        successor_binding,
        source,
        certificate,
        run_id="successor-fixed",
        options=None,
    )

    assert (
        successor.staged_artifacts / "action_coverage_audit/manifest.json"
    ).read_text() == "action\n"
    assert (
        successor.staged_artifacts / "collector_readiness_audit/manifest.json"
    ).read_text() == "collector\n"
    assert successor.lineage["predecessor"]["run_id"] == "previous"
    assert successor.lineage["execution_protocol"] == {
        "action_source_contract_sha256": "b" * 64,
        "action_coverage_audit_sha256": "c" * 64,
        "collector_readiness_audit_sha256": "d" * 64,
        "status": "ready_for_new_final_test_authorization",
        "supported_markets": ["sh", "sz"],
    }


def test_stage8_adapter_prepare_release_preserves_protocol_gate_failure(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "protocol_gate.json").write_text(
        '{"sealed_test_protocol_allowed": false}\n'
    )
    binding = release_adapter.RobustnessReleaseBinding(
        profile="stage8_v1",
        code_root=tmp_path / "code",
        data_root=tmp_path,
        run_id="release-fixed",
        code=FIXED_CODE,
        inputs=FIXED_INPUTS,
        protocol=_protocol(),
        validation=SimpleNamespace(),
        successor_contract=None,
    )

    with pytest.raises(
        ValueError,
        match="^robustness gate does not allow publication$",
    ):
        release_adapter.Stage8ReleaseAdapter().prepare_release(
            binding,
            source,
            {"inputs": FIXED_INPUTS},
            run_id="release-fixed",
            options=None,
        )


def test_stage8_facades_delegate_to_the_shared_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object, Path, dict[str, object]]] = []

    def run_once(adapter, code_root: Path, **kwargs: object) -> Path:
        calls.append(("run", adapter, code_root, kwargs))
        return tmp_path / "run"

    def reproduce(adapter, code_root: Path, **kwargs: object) -> dict[str, object]:
        calls.append(("reproduce", adapter, code_root, kwargs))
        return {"fixed": True}

    monkeypatch.setattr(lifecycle, "run_once", run_once)
    monkeypatch.setattr(lifecycle, "reproduce", reproduce)

    assert pipeline.execute_robustness_run(tmp_path, run_id="fixed") == tmp_path / "run"
    assert pipeline.execute_robustness_reproducibility(
        tmp_path,
        run_id_prefix="pair",
    ) == {"fixed": True}
    assert [item[0] for item in calls] == ["run", "reproduce"]
    assert all(isinstance(item[1], release_adapter.Stage8ReleaseAdapter) for item in calls)
    assert calls[0][2:] == (tmp_path.resolve(), {"run_id": "fixed"})
    assert calls[1][2:] == (tmp_path, {"run_id_prefix": "pair"})
