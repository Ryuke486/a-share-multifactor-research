from dataclasses import replace
from datetime import date
import inspect
from pathlib import Path

from matplotlib import image as mpimg
import polars as pl
import pytest

from factor_card_fixtures import (
    SUMMARY_SCHEMA,
    complete_empty_card_inputs,
    factor_settings,
    replace_keyed_rows,
    set_correlation_pair,
    set_redundancy_flag,
)
from ashare_multifactor.factors.definitions import FACTOR_DEFINITIONS, FactorDefinition
from ashare_multifactor.research.factor_cards import write_factor_cards


NEUTRAL = FactorDefinition("neutral", "test", (), 0, 1)


def test_turnover_card_uses_the_canonical_daily_panel_field(tmp_path: Path) -> None:
    turnover = next(
        definition for definition in FACTOR_DEFINITIONS if definition.name == "turnover_20"
    )
    settings = factor_settings()
    evaluation, classifications, redundancy = complete_empty_card_inputs([turnover], settings)

    write_factor_cards(
        evaluation,
        classifications,
        redundancy,
        [turnover],
        settings,
        tmp_path,
    )

    card = (tmp_path / "cards/turnover_20.md").read_text(encoding="utf-8")
    assert "mean(turnover, 20 valid observations)" in card
    assert "turnover_rate" not in card


def test_factor_card_snapshot_contains_all_sections_and_exact_machine_values(
    tmp_path: Path,
) -> None:
    trend = FactorDefinition(
        "trend_60",
        "momentum",
        ("close_adj", "volume", "amount"),
        60,
        1,
    )
    size = FactorDefinition(
        "size_proxy",
        "size",
        ("total_market_cap",),
        0,
        -1,
        size_neutralize=False,
    )
    settings = factor_settings()
    base_evaluation, base_classifications, base_redundancy = complete_empty_card_inputs(
        [trend, size], settings
    )
    summary_rows = []
    for horizon, mean_ic in ((5, 0.04), (20, 0.031234), (60, 0.01)):
        summary_rows.append(
            {
                "factor_name": "trend_60",
                "family": "momentum",
                "score_variant": "score_size_neutral",
                "horizon": horizon,
                "coverage": 0.83,
                "valid_months": 132,
                "mean_ic": mean_ic,
                "ic_std": 0.08,
                "icir": 1.25,
                "positive_ic_rate": 0.6,
                "nw_lag": 0,
                "nw_se": 0.01,
                "nw_t": 2.5,
                "nw_p": 0.0123,
                "bh_q": 0.045 if horizon == 20 else None,
                "q5_q1": 0.02 if horizon == 20 else None,
                "monotonicity": 0.9 if horizon == 20 else None,
                "avg_turnover": 0.4,
                "point_in_time_status": "ready",
                "nw_reason": None,
                "summary_reason": None,
            }
        )
    summary = pl.DataFrame(summary_rows, schema=SUMMARY_SCHEMA)
    rank_ic = pl.DataFrame(
        [
            {
                "date": date(2005, 1, 31),
                "factor_name": "trend_60",
                "family": "momentum",
                "score_variant": "score_size_neutral",
                "horizon": 20,
                "n_obs": 100,
                "rank_ic": 0.02,
                "reason": None,
            },
            {
                "date": date(2005, 2, 28),
                "factor_name": "trend_60",
                "family": "momentum",
                "score_variant": "score_size_neutral",
                "horizon": 20,
                "n_obs": 100,
                "rank_ic": 0.04,
                "reason": None,
            },
        ],
        schema=base_evaluation.rank_ic.schema,
    )
    quantiles = pl.DataFrame(
        [
            {
                "date": date(2005, 1, 31),
                "factor_name": "trend_60",
                "family": "momentum",
                "score_variant": "score_size_neutral",
                "quantile": quantile,
                "n_obs": 20,
                "mean_forward_return_20": value,
            }
            for quantile, value in enumerate((-0.01, -0.005, 0.0, 0.005, 0.01), start=1)
        ],
        schema=base_evaluation.quantile_returns.schema,
    )
    subperiods = pl.DataFrame(
        [
            {
                "factor_name": "trend_60",
                "family": "momentum",
                "score_variant": "score_size_neutral",
                "subperiod": name,
                "start": start,
                "end": end,
                "valid_months": 44,
                "mean_ic": mean_ic,
            }
            for name, start, end, mean_ic in (
                ("2005-2008", date(2005, 1, 1), date(2008, 12, 31), 0.02),
                ("2009-2012", date(2009, 1, 1), date(2012, 12, 31), -0.01),
                ("2013-2016", date(2013, 1, 1), date(2016, 12, 31), 0.03),
            )
        ],
        schema=base_evaluation.subperiod_metrics.schema,
    )
    turnover = pl.DataFrame(
        [
            {
                "date": date(2005, 1, 31),
                "factor_name": "trend_60",
                "family": "momentum",
                "score_variant": "score_size_neutral",
                "previous_date": None,
                "top_count": 20,
                "previous_top_count": None,
                "turnover": None,
            },
            {
                "date": date(2005, 2, 28),
                "factor_name": "trend_60",
                "family": "momentum",
                "score_variant": "score_size_neutral",
                "previous_date": date(2005, 1, 31),
                "top_count": 20,
                "previous_top_count": 20,
                "turnover": 0.4,
            },
        ],
        schema=base_evaluation.factor_turnover.schema,
    )
    classifications = base_classifications.with_columns(
        pl.when(pl.col("factor_name") == "trend_60")
        .then(pl.lit("candidate"))
        .otherwise(pl.col("classification"))
        .alias("classification"),
        pl.when(pl.col("factor_name") == "trend_60")
        .then(pl.lit("candidate:all_thresholds_passed"))
        .otherwise(pl.col("reason"))
        .alias("reason"),
    )
    redundancy = set_correlation_pair(
        base_redundancy,
        trend,
        size,
        mean_correlation=-0.75,
        common_months=144,
    )
    redundancy = set_redundancy_flag(
        redundancy,
        trend,
        size,
        mean_correlation=-0.75,
        common_months=144,
    )
    evaluation = replace(
        base_evaluation,
        factor_summary=replace_keyed_rows(
            base_evaluation.factor_summary,
            summary,
            ("factor_name", "score_variant", "horizon"),
        ),
        rank_ic=rank_ic,
        quantile_returns=quantiles,
        subperiod_metrics=replace_keyed_rows(
            base_evaluation.subperiod_metrics,
            subperiods,
            ("factor_name", "score_variant", "subperiod"),
        ),
        factor_turnover=turnover,
    )

    write_factor_cards(
        evaluation,
        classifications,
        redundancy,
        [trend, size],
        settings,
        tmp_path,
    )

    card = (tmp_path / "cards" / "trend_60.md").read_text(encoding="utf-8")
    assert (
        card
        == """# 因子卡片：trend_60

## 定义

- 因子族：`momentum`
- 公式：`close_adj[t] / close_adj[t-60] - 1`
- 预注册方向：`+1`（原值越大，理论预期收益越高）
- 主分数变体：`score_size_neutral`

## 经济解释

价格趋势可能因信息扩散缓慢和投资者行为延续而在中期持续。

## 数据状态与覆盖率

- Point-in-time 状态：`ready`
- 覆盖率：`83.000000%`
- 20观测期有效月份：`132`

## 主评价（20个有效交易观测）

- 平均 Rank IC：`0.031234`
- 年化 ICIR：`1.250000`
- Newey-West t / p：`2.500000` / `0.012300`
- BH q：`0.045000`

## 五分组

| 分组 | 平均未来收益 |
|---:|---:|
| Q1 | `-1.000000%` |
| Q2 | `-0.500000%` |
| Q3 | `0.000000%` |
| Q4 | `0.500000%` |
| Q5 | `1.000000%` |

- Q5-Q1：`2.000000%`
- 单调性：`0.900000`

## 衰减

| 未来观测数 | 平均 Rank IC |
|---:|---:|
| 5 | `0.040000` |
| 20 | `0.031234` |
| 60 | `0.010000` |

## Top 20% 换手

- 平均月度换手：`40.000000%`

## 固定子区间

| 子区间 | 有效月份 | 平均 Rank IC |
|---|---:|---:|
| 2005-2008 | `44` | `0.020000` |
| 2009-2012 | `44` | `-0.010000` |
| 2013-2016 | `44` | `0.030000` |

## 冗余关系

- `size_proxy`：相关系数 `-0.750000`，方向 `negative`，共同有效月份 `144`。

## 分类

- 结果：`candidate`
- 原因：`candidate:all_thresholds_passed`

## 限制

- 本卡片只复述机器可读评价产物，不重新计算因子或标签。
- 阶段四结果仅来自2005–2016研究期，不是验证期、最终测试期或投资结论。
- 高相关标记只是冗余证据，不会自动删除因子。
"""
    )


def test_cards_and_four_nonempty_figures_are_written_for_all_fourteen_missing_factors(
    tmp_path: Path,
) -> None:
    settings = factor_settings()
    evaluation, classifications, redundancy = complete_empty_card_inputs(
        FACTOR_DEFINITIONS, settings
    )
    paths = write_factor_cards(
        evaluation,
        classifications,
        redundancy,
        FACTOR_DEFINITIONS,
        settings,
        tmp_path,
    )

    assert len(paths) == 14
    assert [path.name for path in paths] == [
        f"{definition.name}.md" for definition in FACTOR_DEFINITIONS
    ]
    expected_figures = {
        tmp_path / "figures" / definition.name / filename
        for definition in FACTOR_DEFINITIONS
        for filename in ("ic.png", "quantiles.png", "decay.png", "subperiods.png")
    }
    assert set((tmp_path / "figures").glob("*/*.png")) == expected_figures
    for definition in FACTOR_DEFINITIONS:
        card = tmp_path / "cards" / f"{definition.name}.md"
        assert card.is_file()
        card_text = card.read_text(encoding="utf-8")
        assert "机器评价缺失原因：`no_eligible_rows`" in card_text
    for figure in sorted(expected_figures):
        assert figure.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        pixels = mpimg.imread(figure)
        assert pixels.shape[:2] == (480, 840)
        assert pixels.size > 0


def test_card_writer_has_no_raw_panel_or_file_reader_interface(tmp_path: Path) -> None:
    assert "panel" not in inspect.signature(write_factor_cards).parameters
    settings = factor_settings()
    evaluation, classifications, redundancy = complete_empty_card_inputs([NEUTRAL], settings)
    with pytest.raises(TypeError, match="panel"):
        write_factor_cards(
            evaluation,
            classifications,
            redundancy,
            [NEUTRAL],
            settings,
            tmp_path,
            panel=pl.DataFrame(),
        )
