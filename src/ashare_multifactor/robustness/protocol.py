from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
import polars as pl


ANALYSIS_START = date(2005, 1, 1)
ANALYSIS_END = date(2021, 12, 31)
FINAL_TEST_START = date(2022, 1, 1)
FROZEN_MAIN_CANDIDATE = "rolling_ic_family_size_stratified_buffered"
FINAL_TEST_METRICS = (
    "rank_ic",
    "group_spread",
    "group_monotonicity",
    "annual_return",
    "annual_volatility",
    "sharpe_zero_rate",
    "maximum_drawdown",
    "turnover",
    "cost_erosion",
    "target_deviation",
    "unfilled_rate",
)


@dataclass(frozen=True)
class RobustnessExperiment:
    experiment_id: str
    category: str
    change: str
    parameters: Mapping[str, object]
    metrics: tuple[str, ...]
    baseline: bool = False


@dataclass(frozen=True)
class RobustnessProtocol:
    protocol_version: int
    random_seed: int
    analysis_start: date
    analysis_end: date
    sealed_test_start: date
    sealed_test_end: date
    validation_release: str
    main_candidate: str
    baseline_experiment: str
    required_metrics: tuple[str, ...]
    final_test_metrics: tuple[str, ...]
    interpretation: Mapping[str, object]
    experiments: tuple[RobustnessExperiment, ...]
    protocol_sha256: str


def assert_robustness_read_allowed(
    start: date,
    end: date,
    *,
    before_read: Callable[[], object] | None = None,
) -> None:
    """Reject any non-registered or final-test read before scanning."""
    if end < start:
        raise ValueError("read range end precedes start")
    if start < ANALYSIS_START:
        raise ValueError("read range precedes robustness sample")
    if end >= FINAL_TEST_START:
        raise ValueError("final test period is sealed")
    if end > ANALYSIS_END:
        raise ValueError("read range exceeds robustness sample")
    if before_read is not None:
        before_read()


def read_bounded_parquet(
    path: Path,
    *,
    start: date = ANALYSIS_START,
    end: date = ANALYSIS_END,
    date_column: str = "date",
) -> pl.DataFrame:
    """Materialize only registered rows through a predicate-pushed lazy scan."""
    assert_robustness_read_allowed(start, end)
    schema = pl.read_parquet_schema(path)
    if date_column not in schema:
        raise ValueError(f"bounded parquet is missing {date_column}: {path}")
    frame = (
        pl.scan_parquet(path)
        .filter(pl.col(date_column).is_between(start, end, closed="both"))
        .collect()
    )
    if frame.filter(
        (pl.col(date_column) < start) | (pl.col(date_column) > end)
    ).height:
        raise ValueError(f"bounded parquet returned an out-of-range row: {path}")
    return frame


def assert_authoritative_period_contracts(
    validation_lineage: Mapping[str, object],
    stage5_lineage: Mapping[str, object],
    factor_lineage: Mapping[str, object],
) -> None:
    """Reject an upstream contract that reaches the sealed final-test period."""
    validation_period = validation_lineage.get("period")
    if (
        not isinstance(validation_period, list)
        or len(validation_period) != 2
        or _as_date(validation_period[1]) > ANALYSIS_END
        or validation_lineage.get("sealed_final_test_start")
        != FINAL_TEST_START.isoformat()
    ):
        raise ValueError("validation input contract reaches the sealed test period")
    try:
        stage5_end = stage5_lineage["config"]["factor_combination"]["analysis_end"]  # type: ignore[index]
        factor_end = factor_lineage["factor_config"]["payload"]["analysis_end"]  # type: ignore[index]
    except (KeyError, TypeError) as exc:
        raise ValueError("upstream input period contract is incomplete") from exc
    if _as_date(stage5_end) > date(2016, 12, 31):
        raise ValueError("Stage-5 input contract exceeds the research period")
    if _as_date(factor_end) > date(2016, 12, 31):
        raise ValueError("factor input contract exceeds the research period")


def load_robustness_protocol(path: Path) -> RobustnessProtocol:
    raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    if not isinstance(raw, dict):
        raise ValueError("robustness protocol must be a mapping")
    start, end = map(_as_date, raw["analysis_period"])
    sealed_start, sealed_end = map(_as_date, raw["sealed_test_period"])
    if (start, end) != (ANALYSIS_START, ANALYSIS_END):
        raise ValueError("robustness analysis period changed")
    if sealed_start != FINAL_TEST_START or sealed_end != date(2025, 12, 31):
        raise ValueError("sealed final-test period changed")
    if raw["main_candidate"] != FROZEN_MAIN_CANDIDATE:
        raise ValueError("frozen validation main candidate changed")
    metrics = tuple(str(item) for item in raw["required_metrics"])
    final_test_metrics = tuple(str(item) for item in raw["final_test_metrics"])
    if final_test_metrics != FINAL_TEST_METRICS:
        raise ValueError("frozen final-test metrics changed")
    items = raw.get("experiments", {})
    if not isinstance(items, dict):
        raise ValueError("experiments must be a mapping")
    ids = list(items)
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate experiment ID")
    experiments = tuple(
        _experiment(experiment_id, definition, metrics)
        for experiment_id, definition in sorted(items.items())
    )
    if len({item.experiment_id for item in experiments}) != len(experiments):
        raise ValueError("duplicate experiment ID")
    baseline_id = str(raw["baseline_experiment"])
    if baseline_id not in {item.experiment_id for item in experiments}:
        raise ValueError("baseline experiment is missing")
    canonical = json.dumps(
        _jsonable(raw), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return RobustnessProtocol(
        protocol_version=int(raw["protocol_version"]),
        random_seed=int(raw["random_seed"]),
        analysis_start=start,
        analysis_end=end,
        sealed_test_start=sealed_start,
        sealed_test_end=sealed_end,
        validation_release=str(raw["validation_release"]),
        main_candidate=str(raw["main_candidate"]),
        baseline_experiment=baseline_id,
        required_metrics=metrics,
        final_test_metrics=final_test_metrics,
        interpretation=MappingProxyType(dict(raw["interpretation"])),
        experiments=experiments,
        protocol_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _experiment(
    experiment_id: str,
    definition: object,
    metrics: tuple[str, ...],
) -> RobustnessExperiment:
    if not isinstance(definition, dict):
        raise ValueError(f"experiment must be a mapping: {experiment_id}")
    parameters = definition.get("parameters", {})
    if not isinstance(parameters, dict):
        raise ValueError(f"experiment parameters must be a mapping: {experiment_id}")
    category = str(definition.get("category", ""))
    change = str(definition.get("change", ""))
    if not category or not change:
        raise ValueError(f"experiment category/change is required: {experiment_id}")
    return RobustnessExperiment(
        experiment_id=str(experiment_id),
        category=category,
        change=change,
        parameters=MappingProxyType(dict(parameters)),
        metrics=metrics,
        baseline=bool(definition.get("baseline", False)),
    )


def _as_date(value: object) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _jsonable(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f"duplicate experiment ID or YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)
