from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import resource
import shutil
import time

from ashare_multifactor.audit.reproducible_release import _ReleasePreparation
from ashare_multifactor.validation import full_run, pipeline


@dataclass(frozen=True)
class ValidationReleaseBinding:
    profile: str
    code_root: Path
    data_root: Path
    run_id: str
    code: Mapping[str, object]
    inputs: Mapping[str, object]
    candidates: tuple[str, ...]


@dataclass(frozen=True)
class ValidationReleaseOptions:
    publish: bool


@dataclass(frozen=True)
class _VerifiedValidationLineage(Mapping[str, object]):
    payload: Mapping[str, object]
    staged: Path
    data_root: Path

    def _verified(self) -> Mapping[str, object]:
        staged_lineage = json.loads(
            (self.staged / "lineage.json").read_text(encoding="utf-8")
        )
        if pipeline.resolve_validation_predecessor(
            self.data_root
        ) != staged_lineage.get("supersedes"):
            raise ValueError("validation predecessor changed during publication")
        return self.payload

    def __getitem__(self, key: str) -> object:
        return self._verified()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._verified())

    def __len__(self) -> int:
        return len(self._verified())


class Stage7ReleaseAdapter:
    def bind(self, code_root: Path, *, run_id: str) -> ValidationReleaseBinding:
        code_root = code_root.resolve()
        data_root = pipeline.resolve_validation_data_root(code_root)
        pipeline.assert_validation_upstream_releases(data_root)
        identity = pipeline.code_identity(code_root)
        inputs = pipeline._validation_input_identity(code_root, data_root)
        return ValidationReleaseBinding(
            profile="stage7_v1",
            code_root=code_root,
            data_root=data_root,
            run_id=run_id,
            code=identity,
            inputs=inputs,
            candidates=pipeline.FROZEN_CANDIDATES,
        )

    def execute(
        self,
        binding: ValidationReleaseBinding,
        run_root: Path,
    ) -> dict[str, object]:
        started = time.monotonic()
        full_run.execute_validation_stages(
            binding.code_root,
            binding.data_root,
            run_root,
        )
        elapsed = time.monotonic() - started
        maximum_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        pipeline.assert_validation_outputs_sealed(run_root)
        return {
            "resource_usage": {
                "elapsed_seconds": elapsed,
                "maximum_rss_bytes": maximum_rss,
            }
        }

    def prepare_release(
        self,
        binding: ValidationReleaseBinding,
        source: Path,
        certificate: dict[str, object],
        *,
        run_id: str,
        options: object,
    ) -> _ReleasePreparation:
        settings = pipeline.load_config(
            binding.code_root / "configs/research_protocol.yaml"
        ).validation_evaluation
        if settings is None:
            raise ValueError("validation evaluation configuration is missing")
        pipeline.assert_validation_upstream_releases(binding.data_root)
        pipeline.assert_validation_outputs_sealed(source)
        pipeline.assert_validation_runtime_gates(source, settings.candidates)
        staged = (
            binding.data_root
            / "artifacts/validation_evaluation/validation_runs"
            / run_id
        )
        if staged.exists():
            raise FileExistsError(staged)
        pipeline._copy_certified_core(
            source,
            staged,
            certificate["core_hashes"],
        )
        if (
            pipeline._validation_input_identity(
                binding.code_root,
                binding.data_root,
            )
            != binding.inputs
        ):
            shutil.rmtree(staged, ignore_errors=True)
            raise ValueError("validation inputs changed during staging")

        predecessor = pipeline.resolve_validation_predecessor(binding.data_root)
        lineage = pipeline._validation_lineage(
            binding.code_root,
            binding.data_root,
            settings.candidates,
            reproducibility=certificate,
            predecessor=predecessor,
        )
        (staged / "lineage.json").write_text(
            json.dumps(lineage, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        if (
            pipeline._validation_input_identity(
                binding.code_root,
                binding.data_root,
            )
            != binding.inputs
        ):
            shutil.rmtree(staged, ignore_errors=True)
            raise ValueError("validation inputs changed while writing lineage")
        release_lineage: Mapping[str, object] = lineage
        if isinstance(options, ValidationReleaseOptions) and options.publish:
            release_lineage = _VerifiedValidationLineage(
                payload=lineage,
                staged=staged,
                data_root=binding.data_root,
            )
        return _ReleasePreparation(
            staged_root=staged,
            publication_root=(
                binding.data_root / "processed/validation_evaluation"
            ),
            staged_datasets=staged / "datasets",
            staged_artifacts=staged / "artifacts",
            lineage=release_lineage,
            manifest_metadata={"stage": "validation_evaluation"},
        )
