"""Thin orchestration for the isolated factor-research stages."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from typing import Final

import polars as pl

from ashare_multifactor.config import FactorResearchSettings, Period, ResearchConfig, load_config
from ashare_multifactor.data.build import build_parquet_dataset
from ashare_multifactor.data.field_audit import (
    FieldReadiness,
    audit_factor_fields,
    write_data_readiness,
)
from ashare_multifactor.data.manifest import DailyPanelSource, validate_panel_source
from ashare_multifactor.factors.definitions import FACTOR_DEFINITIONS
from ashare_multifactor.factors.panel import build_monthly_factor_panel
from ashare_multifactor.research.factor_evaluation import (
    FactorEvaluationBundle,
    evaluate_factors,
)
from ashare_multifactor.research.factor_lineage import (
    lineage_identity,
    load_and_validate_lineage,
    load_field_evidence,
    new_lineage,
    record_stage,
    stage_outputs_digest,
)
from ashare_multifactor.research.factor_outputs import (
    EVALUATION_FILE_NAMES,
    clear_stage_and_downstream,
    copy_file_atomic,
    expected_stage_outputs,
    invalidate_factor_outputs,
    publish_report,
    validate_published_report,
    write_json_atomic,
    write_monthly_raw,
    write_parquet_atomic,
)
from ashare_multifactor.research.factor_paths import validate_factor_paths
from ashare_multifactor.research.factor_preprocessing import preprocess_factor_panel
from ashare_multifactor.research.factor_redundancy import (
    FactorRedundancyBundle,
    analyze_factor_redundancy,
)
from ashare_multifactor.research.factor_selection import classify_factors
from ashare_multifactor.research.labels import add_forward_returns


STAGES: Final = ("build", "audit", "factors", "evaluate", "report", "all")
_AUDIT_INPUT_COLUMNS = tuple(
    dict.fromkeys(
        (
            "date",
            "symbol",
            *(column for definition in FACTOR_DEFINITIONS for column in definition.source_columns),
            "industry",
            "is_st",
        )
    )
)
_FACTOR_INPUT_COLUMNS = tuple(
    dict.fromkeys(
        (
            "date",
            "symbol",
            "open_raw",
            "high_raw",
            "low_raw",
            "close_raw",
            "is_st",
            *(column for definition in FACTOR_DEFINITIONS for column in definition.source_columns),
        )
    )
)


def run_factor_pipeline(config_path: Path, stage: str) -> None:
    """Run one preregistered factor-research stage."""
    if stage not in STAGES:
        raise ValueError(f"unsupported factor research stage: {stage}")
    config = load_config(config_path)
    settings = _settings(config)
    paths = validate_factor_paths(config)
    factor_root = paths.processed_root
    artifact_root = paths.artifact_root
    evidence = load_field_evidence(config_path) if stage != "build" else None
    runners = {
        "build": lambda: _run_build(config, settings, factor_root, artifact_root),
        "audit": lambda: _run_audit(settings, factor_root, artifact_root, evidence),
        "factors": lambda: _run_factors(settings, factor_root, artifact_root, evidence),
        "evaluate": lambda: _run_evaluate(settings, factor_root, artifact_root, evidence),
        "report": lambda: _run_report(settings, factor_root, artifact_root, evidence),
    }
    if stage != "all":
        runners[stage]()
        return
    if not (factor_root / "daily_panel/manifest.json").is_file():
        runners["build"]()
    for stage_name in ("audit", "factors", "evaluate", "report"):
        runners[stage_name]()


def _run_build(
    config: ResearchConfig,
    settings: FactorResearchSettings,
    factor_root: Path,
    artifact_root: Path,
) -> None:
    build_parquet_dataset(
        config,
        settings.data_start,
        settings.analysis_end,
        output_root=factor_root / "daily_panel",
    )
    invalidate_factor_outputs(factor_root, artifact_root)


def _run_audit(
    settings: FactorResearchSettings,
    factor_root: Path,
    artifact_root: Path,
    evidence: dict[str, object] | None,
) -> None:
    evidence = _require_evidence(evidence)
    source = _validate_source(factor_root, settings)
    statuses = evidence["statuses"]
    if not isinstance(statuses, dict):
        raise ValueError("invalid field evidence statuses")
    lineage = new_lineage(settings, source, factor_root, evidence)
    _prepare_stage(lineage, "audit", factor_root, artifact_root)
    try:
        readiness = audit_factor_fields(
            _read_daily(source, _AUDIT_INPUT_COLUMNS),
            FACTOR_DEFINITIONS,
            valuation_verified=statuses["valuation"] == "verified",
            industry_verified=statuses["industry"] == "verified",
            historical_st_verified=statuses["historical_st"] == "verified",
        )
        readiness_path = factor_root / "data_readiness.json"
        write_data_readiness(readiness_path, readiness)
        record_stage(
            lineage,
            "audit",
            (factor_root / "daily_panel/manifest.json", *source.files),
            expected_stage_outputs(factor_root, "audit", tuple(FACTOR_DEFINITIONS)),
            factor_root,
        )
        write_json_atomic(factor_root / "lineage.json", lineage)
    except BaseException:
        _abort_stage(lineage, "audit", factor_root, artifact_root)
        raise


def _run_factors(
    settings: FactorResearchSettings,
    factor_root: Path,
    artifact_root: Path,
    evidence: dict[str, object] | None,
) -> None:
    evidence = _require_evidence(evidence)
    source, lineage = _validated_lineage(factor_root, settings, evidence, ("audit",))
    _prepare_stage(lineage, "factors", factor_root, artifact_root)
    try:
        readiness_path = factor_root / "data_readiness.json"
        readiness = FieldReadiness(**json.loads(readiness_path.read_text(encoding="utf-8")))
        daily = _read_daily(source, _FACTOR_INPUT_COLUMNS)
        raw = build_monthly_factor_panel(daily, FACTOR_DEFINITIONS, settings)
        monthly_root = factor_root / "monthly_raw"
        write_monthly_raw(monthly_root, raw)
        features = preprocess_factor_panel(raw, readiness, settings).sort(
            "date", "symbol", "factor_name"
        )
        del raw
        feature_path = factor_root / "factor_features.parquet"
        write_parquet_atomic(feature_path, features)
        labels = _signal_labels(daily, features, settings)
        del daily
        labels_path = factor_root / "forward_returns.parquet"
        write_parquet_atomic(labels_path, labels)
        return_columns = [f"forward_return_{horizon}" for horizon in settings.forward_horizons]
        panel = features.join(
            labels.select("date", "symbol", *return_columns),
            on=["date", "symbol"],
            how="left",
            validate="m:1",
        ).sort("date", "symbol", "factor_name")
        panel_path = factor_root / "factor_panel.parquet"
        write_parquet_atomic(panel_path, panel)
        record_stage(
            lineage,
            "factors",
            (readiness_path, factor_root / "daily_panel/manifest.json", *source.files),
            expected_stage_outputs(factor_root, "factors", tuple(FACTOR_DEFINITIONS)),
            factor_root,
        )
        write_json_atomic(factor_root / "lineage.json", lineage)
    except BaseException:
        _abort_stage(lineage, "factors", factor_root, artifact_root)
        raise


def _run_evaluate(
    settings: FactorResearchSettings,
    factor_root: Path,
    artifact_root: Path,
    evidence: dict[str, object] | None,
) -> None:
    evidence = _require_evidence(evidence)
    _, lineage = _validated_lineage(factor_root, settings, evidence, ("audit", "factors"))
    _prepare_stage(lineage, "evaluate", factor_root, artifact_root)
    try:
        panel_path = factor_root / "factor_panel.parquet"
        features_path = factor_root / "factor_features.parquet"
        evaluation = evaluate_factors(pl.read_parquet(panel_path), FACTOR_DEFINITIONS, settings)
        classifications = classify_factors(
            evaluation.factor_summary,
            evaluation.subperiod_metrics,
            FACTOR_DEFINITIONS,
            settings,
        )
        redundancy = analyze_factor_redundancy(
            pl.read_parquet(features_path), FACTOR_DEFINITIONS, settings
        )
        _write_evaluation(factor_root, evaluation, classifications, redundancy)
        record_stage(
            lineage,
            "evaluate",
            (panel_path, features_path),
            expected_stage_outputs(factor_root, "evaluate", tuple(FACTOR_DEFINITIONS)),
            factor_root,
        )
        write_json_atomic(factor_root / "lineage.json", lineage)
    except BaseException:
        _abort_stage(lineage, "evaluate", factor_root, artifact_root)
        raise


def _run_report(
    settings: FactorResearchSettings,
    factor_root: Path,
    artifact_root: Path,
    evidence: dict[str, object] | None,
) -> None:
    evidence = _require_evidence(evidence)
    required_stages = ("audit", "factors", "evaluate")
    _, lineage = _validated_lineage(
        factor_root,
        settings,
        evidence,
        required_stages,
    )
    if _has_stage(lineage, "report"):
        _, lineage = _validated_lineage(
            factor_root,
            settings,
            evidence,
            (*required_stages, "report"),
        )
        validate_published_report(lineage, factor_root, artifact_root)
    _prepare_stage(lineage, "report", factor_root, artifact_root)
    try:
        evaluation = _read_evaluation(factor_root)
        classifications = pl.read_parquet(factor_root / "factor_classifications.parquet")
        redundancy = FactorRedundancyBundle(
            factor_correlations=pl.read_parquet(factor_root / "factor_correlations.parquet"),
            redundancy_flags=pl.read_parquet(factor_root / "redundancy_flags.parquet"),
        )
        readiness_path = factor_root / "data_readiness.json"
        report_manifest = factor_root / "report_manifest.json"
        artifact_manifest = publish_report(
            artifact_root,
            json.loads(readiness_path.read_text(encoding="utf-8")),
            evaluation,
            classifications,
            redundancy,
            tuple(FACTOR_DEFINITIONS),
            settings,
            research_identity=lineage_identity(lineage),
            evaluate_outputs_sha256=stage_outputs_digest(lineage, "evaluate"),
        )
        copy_file_atomic(artifact_manifest, report_manifest)
        inputs = tuple(factor_root / name for name in EVALUATION_FILE_NAMES) + (
            factor_root / "factor_classifications.parquet",
            factor_root / "factor_correlations.parquet",
            factor_root / "redundancy_flags.parquet",
            readiness_path,
        )
        record_stage(
            lineage,
            "report",
            inputs,
            expected_stage_outputs(factor_root, "report", tuple(FACTOR_DEFINITIONS)),
            factor_root,
        )
        validate_published_report(lineage, factor_root, artifact_root)
        write_json_atomic(factor_root / "lineage.json", lineage)
    except BaseException:
        _abort_stage(lineage, "report", factor_root, artifact_root)
        raise


def _validate_source(factor_root: Path, settings: FactorResearchSettings) -> DailyPanelSource:
    allowed = Period(
        max(settings.data_start, date(2003, 1, 1)),
        min(settings.analysis_end, date(2016, 12, 31)),
    )
    return validate_panel_source(factor_root / "daily_panel", allowed)


def _validated_lineage(
    factor_root: Path,
    settings: FactorResearchSettings,
    evidence: dict[str, object],
    stages: tuple[str, ...],
) -> tuple[DailyPanelSource, dict[str, object]]:
    source = _validate_source(factor_root, settings)
    lineage = load_and_validate_lineage(
        factor_root / "lineage.json",
        settings,
        source,
        factor_root,
        evidence,
        stages,
        {
            stage: expected_stage_outputs(
                factor_root,
                stage,
                tuple(FACTOR_DEFINITIONS),
            )
            for stage in stages
        },
    )
    return source, lineage


def _read_daily(source: DailyPanelSource, columns: tuple[str, ...]) -> pl.DataFrame:
    return pl.scan_parquet(source.files).select(columns).collect().sort("date", "symbol")


def _signal_labels(
    daily: pl.DataFrame,
    features: pl.DataFrame,
    settings: FactorResearchSettings,
) -> pl.DataFrame:
    keys = features.select("date", "symbol").unique().sort("date", "symbol")
    labelled = add_forward_returns(
        daily.select("date", "symbol", "close_adj", "volume", "amount"),
        settings.forward_horizons,
    )
    forward_columns = [column for column in labelled.columns if column.startswith("forward_")]
    return keys.join(
        labelled.select("date", "symbol", *forward_columns),
        on=["date", "symbol"],
        how="left",
        validate="1:1",
    ).sort("date", "symbol")


def _write_evaluation(
    root: Path,
    evaluation: FactorEvaluationBundle,
    classifications: pl.DataFrame,
    redundancy: FactorRedundancyBundle,
) -> tuple[Path, ...]:
    tables = {
        "rank_ic.parquet": evaluation.rank_ic,
        "quantile_returns.parquet": evaluation.quantile_returns,
        "subperiod_metrics.parquet": evaluation.subperiod_metrics,
        "factor_turnover.parquet": evaluation.factor_turnover,
        "factor_summary.parquet": evaluation.factor_summary,
        "factor_classifications.parquet": classifications,
        "factor_correlations.parquet": redundancy.factor_correlations,
        "redundancy_flags.parquet": redundancy.redundancy_flags,
    }
    outputs = []
    for name, frame in tables.items():
        path = root / name
        write_parquet_atomic(path, frame)
        outputs.append(path)
    return tuple(outputs)


def _read_evaluation(root: Path) -> FactorEvaluationBundle:
    return FactorEvaluationBundle(
        rank_ic=pl.read_parquet(root / "rank_ic.parquet"),
        quantile_returns=pl.read_parquet(root / "quantile_returns.parquet"),
        subperiod_metrics=pl.read_parquet(root / "subperiod_metrics.parquet"),
        factor_turnover=pl.read_parquet(root / "factor_turnover.parquet"),
        factor_summary=pl.read_parquet(root / "factor_summary.parquet"),
    )


def _settings(config: ResearchConfig) -> FactorResearchSettings:
    if config.factor_research is None:
        raise ValueError("factor_research settings are required")
    return config.factor_research


def _require_evidence(evidence: dict[str, object] | None) -> dict[str, object]:
    if evidence is None:
        raise ValueError("field evidence is required outside the build stage")
    return evidence


def _drop_stages(lineage: dict[str, object], names: tuple[str, ...]) -> None:
    stages = lineage.get("stages")
    if not isinstance(stages, dict):
        raise ValueError("invalid factor research lineage stages")
    for name in names:
        stages.pop(name, None)


def _has_stage(lineage: dict[str, object], name: str) -> bool:
    stages = lineage.get("stages")
    if not isinstance(stages, dict):
        raise ValueError("invalid factor research lineage stages")
    return name in stages


_STAGE_TAILS = {
    "audit": ("audit", "factors", "evaluate", "report"),
    "factors": ("factors", "evaluate", "report"),
    "evaluate": ("evaluate", "report"),
    "report": ("report",),
}


def _prepare_stage(
    lineage: dict[str, object],
    stage: str,
    factor_root: Path,
    artifact_root: Path,
) -> None:
    _drop_stages(lineage, _STAGE_TAILS[stage])
    write_json_atomic(factor_root / "lineage.json", lineage)
    clear_stage_and_downstream(factor_root, artifact_root, stage)


def _abort_stage(
    lineage: dict[str, object],
    stage: str,
    factor_root: Path,
    artifact_root: Path,
) -> None:
    _drop_stages(lineage, _STAGE_TAILS[stage])
    write_json_atomic(factor_root / "lineage.json", lineage)
    clear_stage_and_downstream(factor_root, artifact_root, stage)
