from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
from typing import Mapping

from ashare_multifactor.audit.reproducible_release import _ReleasePreparation
from ashare_multifactor.audit.records import sha256_file
from ashare_multifactor.robustness import pipeline
from ashare_multifactor.robustness.successor_seal import (
    Stage8Supersession,
    build_stage8_successor_lineage,
)


@dataclass(frozen=True)
class RobustnessReleaseBinding:
    profile: str
    code_root: Path
    data_root: Path
    run_id: str
    code: Mapping[str, object]
    inputs: Mapping[str, object]
    protocol: object
    validation: object
    successor_contract: Mapping[str, object] | None


class Stage8ReleaseAdapter:
    def __init__(
        self,
        *,
        successor_audit_root: Path | None = None,
        collector_readiness_root: Path | None = None,
    ) -> None:
        if (successor_audit_root is None) != (collector_readiness_root is None):
            raise ValueError("Stage-8 successor audit roots are incomplete")
        self.successor_audit_root = successor_audit_root
        self.collector_readiness_root = collector_readiness_root
        self._successor_contract: Mapping[str, object] | None = None

    def bind(self, code_root: Path, *, run_id: str) -> RobustnessReleaseBinding:
        code_root = code_root.resolve()
        data_root = pipeline.resolve_robustness_data_root(code_root)
        protocol = pipeline.load_robustness_protocol(
            code_root / "configs/robustness_protocol.yaml"
        )
        validation = pipeline.resolve_current(
            data_root / "processed/validation_evaluation"
        )
        if validation.run_id != protocol.validation_release:
            raise ValueError("authoritative validation release changed")
        decision = json.loads(
            (
                validation.artifacts / "research/selection_decision.json"
            ).read_text(encoding="utf-8")
        )
        if decision.get("selected_candidate") != protocol.main_candidate:
            raise ValueError("validation main candidate changed")
        pipeline._assert_input_period_contracts(data_root, validation)
        pipeline.assert_robustness_read_allowed(
            protocol.analysis_start,
            protocol.analysis_end,
        )
        if (
            self.successor_audit_root is not None
            and self._successor_contract is None
        ):
            assert self.collector_readiness_root is not None
            self._successor_contract = pipeline._successor_release_contract(
                code_root,
                data_root,
                self.successor_audit_root,
                self.collector_readiness_root,
            )
        return RobustnessReleaseBinding(
            profile="stage8_v1",
            code_root=code_root,
            data_root=data_root,
            run_id=run_id,
            code=pipeline.code_identity(code_root),
            inputs=pipeline._robustness_input_identity(
                code_root,
                data_root,
                validation,
            ),
            protocol=protocol,
            validation=validation,
            successor_contract=self._successor_contract,
        )

    def execute(
        self,
        binding: RobustnessReleaseBinding,
        run_root: Path,
    ) -> dict[str, object]:
        results, fatal_events = pipeline._execute_experiments(
            binding.code_root,
            binding.data_root,
            binding.validation,
            binding.protocol,
        )
        table = pipeline.build_robustness_long_table(
            results,
            binding.protocol,
        )
        gate = pipeline.assess_test_protocol_gate(
            table,
            fatal_events=fatal_events,
            protocol=binding.protocol,
        )
        table.write_parquet(run_root / "robustness_results.parquet")
        (run_root / "protocol_gate.json").write_text(
            json.dumps(gate, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (run_root / "report.md").write_text(
            pipeline.render_robustness_report(
                table,
                gate,
                binding.protocol,
            ),
            encoding="utf-8",
        )
        pipeline.assert_robustness_outputs_sealed(run_root)
        return {}

    def prepare_release(
        self,
        binding: RobustnessReleaseBinding,
        source: Path,
        certificate: dict[str, object],
        *,
        run_id: str,
        options: object,
    ) -> _ReleasePreparation:
        gate = json.loads(
            (source / "protocol_gate.json").read_text(encoding="utf-8")
        )
        if gate.get("sealed_test_protocol_allowed") is not True:
            raise ValueError("robustness gate does not allow publication")
        research_config = pipeline.load_config(
            binding.code_root / "configs/research_protocol.yaml"
        )
        pipeline._assert_validation_market_scope(
            binding.validation,
            research_config.supported_markets,
        )
        validation_pointer = json.loads(
            (
                binding.data_root
                / "processed/validation_evaluation/CURRENT.json"
            ).read_text(encoding="utf-8")
        )
        staged = (
            binding.data_root
            / "artifacts/robustness/release_staging"
            / run_id
        )
        if staged.exists():
            raise FileExistsError(staged)
        datasets = staged / "datasets"
        artifacts = staged / "artifacts"
        datasets.mkdir(parents=True)
        artifacts.mkdir()
        shutil.copy2(source / "robustness_results.parquet", datasets)
        shutil.copy2(source / "report.md", artifacts)
        shutil.copy2(source / "protocol_gate.json", artifacts)
        successor_contract = binding.successor_contract
        if successor_contract is not None:
            assert self.successor_audit_root is not None
            assert self.collector_readiness_root is not None
            shutil.copytree(
                self.successor_audit_root,
                artifacts / "action_coverage_audit",
            )
            shutil.copytree(
                self.collector_readiness_root,
                artifacts / "collector_readiness_audit",
            )
        sealed = pipeline.seal_test_protocol(
            artifacts / "sealed_test_protocol.json",
            protocol=binding.protocol,
            code_identity=dict(binding.code),
            validation_pointer=validation_pointer,
            market_rules_sha256=sha256_file(
                binding.code_root / "configs/market_rules.yaml"
            ),
            report_template_sha256=sha256_file(
                binding.code_root
                / "docs/templates/stage9-final-test-report-template.md"
            ),
            opening_ledger_root=(
                binding.data_root
                / "processed/robustness/final_test_opening_ledger"
            ),
            gate=gate,
            supported_markets=research_config.supported_markets,
            action_source_contract_sha256=(
                str(successor_contract["action_source_contract_sha256"])
                if successor_contract is not None
                else None
            ),
            action_coverage_audit_sha256=(
                str(successor_contract["action_coverage_audit_sha256"])
                if successor_contract is not None
                else None
            ),
            collector_readiness_audit_sha256=(
                str(successor_contract["collector_readiness_audit_sha256"])
                if successor_contract is not None
                else None
            ),
            predecessor=(
                successor_contract["predecessor"]
                if successor_contract is not None
                else None
            ),
        )
        lineage: dict[str, object] = {
            "stage": "robustness",
            "period": ["2005-01-01", "2021-12-31"],
            "sealed_test_start": "2022-01-01",
            "main_candidate": binding.protocol.main_candidate,
            "code": dict(binding.code),
            "inputs": certificate["inputs"],
            "reproducibility": certificate,
            "sealed_protocol_sha256": sealed["sealed_protocol_sha256"],
            "supported_markets": list(research_config.supported_markets),
        }
        if successor_contract is not None:
            lineage = build_stage8_successor_lineage(
                lineage,
                supersession=Stage8Supersession(
                    **successor_contract["predecessor"]
                ),
                action_source_contract_sha256=str(
                    successor_contract["action_source_contract_sha256"]
                ),
                action_coverage_audit_sha256=str(
                    successor_contract["action_coverage_audit_sha256"]
                ),
                collector_readiness_audit_sha256=str(
                    successor_contract["collector_readiness_audit_sha256"]
                ),
            )
            lineage["execution_protocol"]["supported_markets"] = list(
                research_config.supported_markets
            )
        return _ReleasePreparation(
            staged_root=staged,
            publication_root=binding.data_root / "processed/robustness",
            staged_datasets=datasets,
            staged_artifacts=artifacts,
            lineage=lineage,
            manifest_metadata={"stage": "robustness"},
        )
