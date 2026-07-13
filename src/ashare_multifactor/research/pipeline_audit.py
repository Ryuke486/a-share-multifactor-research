"""Run-manifest logging and source-data quality publication for the MVP."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Mapping

from ashare_multifactor.config import ResearchConfig
from ashare_multifactor.research.lineage import config_snapshot, load_data_manifest
from ashare_multifactor.research.pipeline_io import read_json, read_json_value, write_json


LOGGER = logging.getLogger(__name__)
_OPERATION_STAGE = {
    "build_universe": "signals",
    "add_momentum_and_forward_return": "signals",
    "monthly_signal_panel": "signals",
    "rank_ic_by_date": "signals",
    "build_target_weights": "targets",
    "run_backtest": "backtest",
    "run_backtest_gross": "backtest",
    "write_mvp_report": "report",
}
_STAGE_ORDER = ("signals", "targets", "backtest", "report")


class PipelineRecorder:
    def __init__(
        self,
        path: Path,
        config_path: Path,
        config: ResearchConfig,
        *,
        reset: bool,
    ) -> None:
        self.path = path
        existing = None if reset or not path.exists() else read_json(path)
        frozen_config = config_snapshot(config)
        if existing is not None and existing.get("frozen_config") != frozen_config:
            raise ValueError("existing MVP manifest does not match current configuration")
        self.payload: dict[str, object] = existing or {
            "pipeline": "ashare_momentum_mvp",
            "config_path": str(config_path),
            "frozen_config": frozen_config,
            "smoke_data": {
                "start": config.smoke_data.start.isoformat(),
                "end": config.smoke_data.end.isoformat(),
            },
            "smoke_analysis": {
                "start": config.smoke_analysis.start.isoformat(),
                "end": config.smoke_analysis.end.isoformat(),
            },
            "operations": [],
        }
        self._write()

    def start(self, name: str, input_stats: Mapping[str, object]) -> None:
        LOGGER.info(
            "%s start input_rows=%s min_date=%s max_date=%s",
            name,
            input_stats.get("rows"),
            input_stats.get("min_date"),
            input_stats.get("max_date"),
        )

    def finish(
        self,
        name: str,
        input_stats: Mapping[str, object],
        output_stats: Mapping[str, object],
    ) -> None:
        record = {"name": name, "input": dict(input_stats), "output": dict(output_stats)}
        operations = self.payload["operations"]
        assert isinstance(operations, list)
        for index, operation in enumerate(operations):
            if isinstance(operation, dict) and operation.get("name") == name:
                operations[index] = record
                break
        else:
            operations.append(record)
        self._write()
        LOGGER.info(
            "%s finish output_rows=%s min_date=%s max_date=%s",
            name,
            output_stats.get("rows"),
            output_stats.get("min_date"),
            output_stats.get("max_date"),
        )

    def invalidate_stage(self, stage: str) -> None:
        stage_index = _STAGE_ORDER.index(stage)
        operations = self.payload["operations"]
        assert isinstance(operations, list)
        retained_operations = []
        for operation in operations:
            if not isinstance(operation, dict):
                retained_operations.append(operation)
                continue
            operation_stage = _OPERATION_STAGE.get(str(operation.get("name")))
            if operation_stage is None or _STAGE_ORDER.index(operation_stage) < stage_index:
                retained_operations.append(operation)
        self.payload["operations"] = retained_operations
        self._write()

    def set_data_build(self, source_manifest: Mapping[str, object]) -> None:
        self.payload["data_build"] = dict(source_manifest)
        self._write()

    def _write(self) -> None:
        write_json(self.path, self.payload)


def publish_data_audit(
    config: ResearchConfig,
    recorder: PipelineRecorder,
    source_manifest: Mapping[str, object] | None = None,
) -> None:
    panel_root = config.paths.processed / "daily_panel"
    source_manifest = source_manifest or load_data_manifest(config)
    quality_path = panel_root / "quality_issues.json"
    issues = read_json_value(quality_path)
    if not isinstance(issues, list):
        raise ValueError("quality_issues.json must contain a list")
    output_root = config.paths.artifacts / "mvp"
    write_json(
        output_root / "data_quality.json",
        {
            "issue_count": len(issues),
            "error_count": sum(issue.get("severity") == "error" for issue in issues),
            "warning_count": sum(issue.get("severity") == "warning" for issue in issues),
            "issues": issues,
        },
    )
    recorder.set_data_build(source_manifest)
