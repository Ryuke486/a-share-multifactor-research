"""Render auditable factor cards from machine-readable evaluation tables."""

from __future__ import annotations

from collections.abc import Sequence
import math
from pathlib import Path
import re

import polars as pl

from ashare_multifactor.config import FactorResearchSettings
from ashare_multifactor.factors.definitions import FactorDefinition
from ashare_multifactor.research.factor_card_plots import write_factor_figures
from ashare_multifactor.research.factor_card_validation import validate_factor_card_inputs
from ashare_multifactor.research.factor_evaluation import FactorEvaluationBundle
from ashare_multifactor.research.factor_metrics import SUBPERIODS
from ashare_multifactor.research.factor_redundancy import FactorRedundancyBundle


def write_factor_cards(
    evaluation: FactorEvaluationBundle,
    classifications: pl.DataFrame,
    redundancy: FactorRedundancyBundle,
    definitions: Sequence[FactorDefinition],
    settings: FactorResearchSettings,
    output_dir: Path,
) -> tuple[Path, ...]:
    """Write one Markdown card and four figures per registered definition."""
    definitions = tuple(definitions)
    _validate_definitions(definitions)
    validate_factor_card_inputs(
        evaluation,
        classifications,
        redundancy,
        definitions,
        settings,
    )
    cards_dir = output_dir / "cards"
    figures_dir = output_dir / "figures"
    cards_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for definition in definitions:
        card_path = cards_dir / f"{definition.name}.md"
        card_path.write_text(
            _render_card(evaluation, classifications, redundancy, definition, settings),
            encoding="utf-8",
        )
        write_factor_figures(
            evaluation,
            definition,
            settings,
            figures_dir / definition.name,
        )
        written.append(card_path)
    return tuple(written)


def _render_card(
    evaluation: FactorEvaluationBundle,
    classifications: pl.DataFrame,
    redundancy: FactorRedundancyBundle,
    definition: FactorDefinition,
    settings: FactorResearchSettings,
) -> str:
    variant = _primary_variant(definition)
    summaries = evaluation.factor_summary.filter(
        (pl.col("factor_name") == definition.name) & (pl.col("score_variant") == variant)
    )
    primary = _single_row(
        summaries.filter(pl.col("horizon") == settings.primary_horizon),
        f"{definition.name} primary summary",
    )
    classification = _single_row(
        classifications.filter(pl.col("factor_name") == definition.name),
        f"{definition.name} classification",
    )
    quantile_means = _quantile_means(evaluation.quantile_returns, definition.name, variant)
    subperiods = evaluation.subperiod_metrics.filter(
        (pl.col("factor_name") == definition.name) & (pl.col("score_variant") == variant)
    )
    flags = redundancy.redundancy_flags.filter(
        (pl.col("factor_a") == definition.name) | (pl.col("factor_b") == definition.name)
    )
    return f"""# 因子卡片：{definition.name}

## 定义

- 因子族：`{definition.family}`
- 公式：`{_formula(definition)}`
- 预注册方向：`{definition.direction:+d}`（{_direction_meaning(definition.direction)}）
- 主分数变体：`{variant}`

## 经济解释

{_economic_rationale(definition)}

## 数据状态与覆盖率

- Point-in-time 状态：`{_value(primary, "point_in_time_status")}`
- 覆盖率：`{_format_percent(_value(primary, "coverage"))}`
- {settings.primary_horizon}观测期有效月份：`{_format_integer(_value(primary, "valid_months"))}`

## 主评价（{settings.primary_horizon}个有效交易观测）

- 平均 Rank IC：`{_format_number(_value(primary, "mean_ic"))}`
- 年化 ICIR：`{_format_number(_value(primary, "icir"))}`
- Newey-West t / p：`{_format_number(_value(primary, "nw_t"))}` / `{_format_number(_value(primary, "nw_p"))}`
- BH q：`{_format_number(_value(primary, "bh_q"))}`

## 五分组

| 分组 | 平均未来收益 |
|---:|---:|
{_quantile_table(quantile_means, settings.quantile_count)}

- Q{settings.quantile_count}-Q1：`{_format_percent(_value(primary, "q5_q1"))}`
- 单调性：`{_format_number(_value(primary, "monotonicity"))}`

## 衰减

| 未来观测数 | 平均 Rank IC |
|---:|---:|
{_decay_table(summaries, settings.forward_horizons)}

## Top 20% 换手

- 平均月度换手：`{_format_percent(_value(primary, "avg_turnover"))}`

## 固定子区间

| 子区间 | 有效月份 | 平均 Rank IC |
|---|---:|---:|
{_subperiod_table(subperiods)}

## 冗余关系

{_redundancy_lines(flags, definition.name, settings.redundancy_threshold)}

## 分类

- 结果：`{_value(classification, "classification")}`
- 原因：`{_value(classification, "reason")}`

## 限制

{_limitations(definition, primary)}
"""


def _formula(definition: FactorDefinition) -> str:
    source = definition.source_columns[0] if definition.source_columns else "registered_input"
    if definition.family == "value":
        return f"1 / {source} (only finite positive denominator)"
    if definition.family == "momentum":
        if definition.name.endswith("12_1"):
            return f"close_adj[t-21] / close_adj[t-{definition.lookback}] - 1"
        return f"close_adj[t] / close_adj[t-{definition.lookback}] - 1"
    if definition.family == "reversal":
        return f"close_adj[t] / close_adj[t-{definition.lookback}] - 1"
    if definition.family == "liquidity" and "turnover_rate" in definition.source_columns:
        return f"mean(turnover_rate, {definition.lookback} valid observations)"
    if definition.family == "liquidity":
        return f"mean(abs(adjusted_return) / amount, {definition.lookback} valid observations)"
    if definition.family == "low_volatility" and definition.name.startswith("downside_"):
        return f"sqrt(mean(min(adjusted_return, 0)^2, {definition.lookback})) * sqrt(252)"
    if definition.family == "low_volatility":
        return f"std(adjusted_return, {definition.lookback}) * sqrt(252)"
    if definition.family == "size":
        return f"ln({source}) (finite positive values only)"
    return f"registered {source} transformation with lookback {definition.lookback}"


def _economic_rationale(definition: FactorDefinition) -> str:
    if definition.family == "value":
        return "更高的盈利或账面/销售额相对价格可能补偿估值风险或市场错价。"
    if definition.family == "momentum":
        return "价格趋势可能因信息扩散缓慢和投资者行为延续而在中期持续。"
    if definition.family == "reversal":
        return "短期价格压力或过度反应可能在后续交易中部分修正。"
    if definition.family == "liquidity":
        return "交易摩擦和流动性风险可能形成价格补偿，也可能暴露不可交易性。"
    if definition.family == "low_volatility":
        return "低风险证券可能因投资者偏好和约束获得更高的风险调整后收益。"
    if definition.family == "size":
        return "小市值公司可能承担更高的信息、融资和流动性风险。"
    return "经济解释取决于注册因子族与数据语义。"


def _quantile_means(frame: pl.DataFrame, factor_name: str, variant: str) -> dict[int, float]:
    selected = frame.filter(
        (pl.col("factor_name") == factor_name) & (pl.col("score_variant") == variant)
    )
    if selected.is_empty():
        return {}
    grouped = selected.group_by("quantile").agg(pl.col("mean_forward_return_20").mean())
    return {
        int(row["quantile"]): float(row["mean_forward_return_20"])
        for row in grouped.iter_rows(named=True)
        if _finite(row["mean_forward_return_20"])
    }


def _quantile_table(means: dict[int, float], count: int) -> str:
    return "\n".join(
        f"| Q{quantile} | `{_format_percent(means.get(quantile))}` |"
        for quantile in range(1, count + 1)
    )


def _decay_table(summaries: pl.DataFrame, horizons: Sequence[int]) -> str:
    values = {
        int(row["horizon"]): row["mean_ic"]
        for row in summaries.iter_rows(named=True)
        if row["horizon"] is not None
    }
    return "\n".join(
        f"| {horizon} | `{_format_number(values.get(horizon))}` |" for horizon in horizons
    )


def _subperiod_table(subperiods: pl.DataFrame) -> str:
    rows = {row["subperiod"]: row for row in subperiods.iter_rows(named=True)}
    return "\n".join(
        f"| {name} | `{_format_integer(_value(rows.get(name), 'valid_months'))}` | "
        f"`{_format_number(_value(rows.get(name), 'mean_ic'))}` |"
        for name, _, _ in SUBPERIODS
    )


def _redundancy_lines(
    flags: pl.DataFrame,
    factor_name: str,
    threshold: float,
) -> str:
    if flags.is_empty():
        return f"- 无绝对月度平均相关达到 `{threshold:.2f}` 的冗余标记。"
    relations = []
    for row in flags.iter_rows(named=True):
        other = row["factor_b"] if row["factor_a"] == factor_name else row["factor_a"]
        relations.append((str(other), row))
    return "\n".join(
        f"- `{other}`：相关系数 `{_format_number(row['mean_correlation'])}`，"
        f"方向 `{row['correlation_direction']}`，共同有效月份 "
        f"`{_format_integer(row['common_months'])}`。"
        for other, row in sorted(relations)
    )


def _limitations(definition: FactorDefinition, primary: dict[str, object] | None) -> str:
    lines = []
    if primary is None:
        lines.append("- 无可用的机器评价行；所有统计和图表明确标记为 N/A。")
    elif primary.get("summary_reason") is not None:
        lines.append(f"- 机器评价缺失原因：`{primary['summary_reason']}`。")
    if primary is not None and primary.get("nw_reason") is not None:
        lines.append(f"- Newey-West 缺失原因：`{primary['nw_reason']}`。")
    if definition.requires_verified_pit:
        lines.append("- 估值字段的历史 point-in-time 口径未核验前，该因子最多标记为 watch。")
    lines.extend(
        (
            "- 本卡片只复述机器可读评价产物，不重新计算因子或标签。",
            "- 阶段四结果仅来自2005–2016研究期，不是验证期、最终测试期或投资结论。",
            "- 高相关标记只是冗余证据，不会自动删除因子。",
        )
    )
    return "\n".join(lines)


def _single_row(frame: pl.DataFrame, label: str) -> dict[str, object] | None:
    if frame.height > 1:
        raise ValueError(f"{label} must have at most one row")
    return frame.row(0, named=True) if frame.height else None


def _value(row: dict[str, object] | None, key: str) -> object:
    if row is None or row.get(key) is None:
        return "N/A"
    return row[key]


def _format_number(value: object) -> str:
    return f"{float(value):.6f}" if _finite(value) else "N/A"


def _format_percent(value: object) -> str:
    return f"{float(value):.6%}" if _finite(value) else "N/A"


def _format_integer(value: object) -> str:
    return str(int(value)) if _finite(value) else "N/A"


def _finite(value: object) -> bool:
    return value is not None and not isinstance(value, (bool, str)) and math.isfinite(float(value))


def _primary_variant(definition: FactorDefinition) -> str:
    return "score_size_neutral" if definition.size_neutralize else "score"


def _direction_meaning(direction: int) -> str:
    return "原值越大，理论预期收益越高" if direction > 0 else "原值越小，理论预期收益越高"


def _validate_definitions(definitions: Sequence[FactorDefinition]) -> None:
    names = [definition.name for definition in definitions]
    if len(names) != len(set(names)):
        raise ValueError("duplicate factor definitions are not allowed")
    invalid = [name for name in names if not re.fullmatch(r"[a-z0-9_]+", name)]
    if invalid:
        raise ValueError("factor names are not safe output paths: " + ", ".join(invalid))
