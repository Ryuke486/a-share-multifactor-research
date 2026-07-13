from dataclasses import replace
from datetime import date
import inspect
from pathlib import Path

import polars as pl
import pytest

from ashare_multifactor.config import FactorResearchSettings
from ashare_multifactor.factors.definitions import FACTOR_DEFINITIONS, FactorDefinition
from ashare_multifactor.research.factor_cards import write_factor_cards
from ashare_multifactor.research.factor_evaluation import FactorEvaluationBundle
from ashare_multifactor.research.factor_redundancy import FactorRedundancyBundle


NEUTRAL = FactorDefinition("neutral", "test", (), 0, 1)


def _settings(**overrides: object) -> FactorResearchSettings:
    settings = FactorResearchSettings(
        data_start=date(2003, 1, 1),
        analysis_start=date(2005, 1, 1),
        analysis_end=date(2016, 12, 31),
        universe_size=1_000,
        minimum_history=252,
        liquidity_lookback=20,
        require_valid_trade_observation=True,
        signal_frequency="month_end",
        forward_horizons=(5, 20, 60),
        primary_horizon=20,
        winsor_lower=0.01,
        winsor_upper=0.99,
        quantile_count=5,
        minimum_coverage=0.8,
        minimum_valid_months=120,
        fdr_q_threshold=0.1,
        redundancy_threshold=0.7,
    )
    return replace(settings, **overrides)


def _panel(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema={
            "date": pl.Date,
            "symbol": pl.String,
            "factor_name": pl.String,
            "family": pl.String,
            "score": pl.Float64,
            "score_size_neutral": pl.Float64,
        },
    )


def _evaluation_bundle(
    *,
    factor_summary: pl.DataFrame | None = None,
    rank_ic: pl.DataFrame | None = None,
    quantile_returns: pl.DataFrame | None = None,
    subperiod_metrics: pl.DataFrame | None = None,
    factor_turnover: pl.DataFrame | None = None,
) -> FactorEvaluationBundle:
    return FactorEvaluationBundle(
        rank_ic=rank_ic if rank_ic is not None else _empty_rank_ic(),
        quantile_returns=(
            quantile_returns if quantile_returns is not None else _empty_quantile_returns()
        ),
        subperiod_metrics=(
            subperiod_metrics if subperiod_metrics is not None else _empty_subperiod_metrics()
        ),
        factor_turnover=(
            factor_turnover if factor_turnover is not None else _empty_factor_turnover()
        ),
        factor_summary=factor_summary if factor_summary is not None else _empty_factor_summary(),
    )


def _empty_factor_summary() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "factor_name": pl.String,
            "family": pl.String,
            "score_variant": pl.String,
            "horizon": pl.Int64,
            "coverage": pl.Float64,
            "valid_months": pl.Int64,
            "mean_ic": pl.Float64,
            "ic_std": pl.Float64,
            "icir": pl.Float64,
            "positive_ic_rate": pl.Float64,
            "nw_lag": pl.Int64,
            "nw_se": pl.Float64,
            "nw_t": pl.Float64,
            "nw_p": pl.Float64,
            "bh_q": pl.Float64,
            "q5_q1": pl.Float64,
            "monotonicity": pl.Float64,
            "avg_turnover": pl.Float64,
            "point_in_time_status": pl.String,
            "nw_reason": pl.String,
            "summary_reason": pl.String,
        }
    )


def _missing_factor_summary() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "factor_name": definition.name,
                "family": definition.family,
                "score_variant": ("score_size_neutral" if definition.size_neutralize else "score"),
                "horizon": 20,
                "coverage": None,
                "valid_months": 0,
                "point_in_time_status": "unverified"
                if definition.requires_verified_pit
                else "ready",
                "summary_reason": "no_eligible_rows",
            }
            for definition in FACTOR_DEFINITIONS
        ],
        schema=_empty_factor_summary().schema,
        strict=False,
    )


def _empty_rank_ic() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "date": pl.Date,
            "factor_name": pl.String,
            "family": pl.String,
            "score_variant": pl.String,
            "horizon": pl.Int64,
            "n_obs": pl.Int64,
            "rank_ic": pl.Float64,
            "reason": pl.String,
        }
    )


def _empty_quantile_returns() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "date": pl.Date,
            "factor_name": pl.String,
            "family": pl.String,
            "score_variant": pl.String,
            "quantile": pl.Int64,
            "n_obs": pl.Int64,
            "mean_forward_return_20": pl.Float64,
        }
    )


def _empty_subperiod_metrics() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "factor_name": pl.String,
            "family": pl.String,
            "score_variant": pl.String,
            "subperiod": pl.String,
            "start": pl.Date,
            "end": pl.Date,
            "valid_months": pl.Int64,
            "mean_ic": pl.Float64,
        }
    )


def _empty_factor_turnover() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "date": pl.Date,
            "factor_name": pl.String,
            "family": pl.String,
            "score_variant": pl.String,
            "previous_date": pl.Date,
            "top_count": pl.Int64,
            "previous_top_count": pl.Int64,
            "turnover": pl.Float64,
        }
    )


def _empty_classifications() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "factor_name": pl.String,
            "primary_score_variant": pl.String,
            "classification": pl.String,
            "reason": pl.String,
        }
    )


def _empty_redundancy_bundle() -> FactorRedundancyBundle:
    return FactorRedundancyBundle(
        factor_correlations=pl.DataFrame(
            schema={
                "factor_a": pl.String,
                "factor_b": pl.String,
                "primary_variant_a": pl.String,
                "primary_variant_b": pl.String,
                "mean_correlation": pl.Float64,
                "common_months": pl.Int64,
                "reason": pl.String,
            }
        ),
        redundancy_flags=pl.DataFrame(
            schema={
                "factor_a": pl.String,
                "factor_b": pl.String,
                "primary_variant_a": pl.String,
                "primary_variant_b": pl.String,
                "mean_correlation": pl.Float64,
                "absolute_correlation": pl.Float64,
                "correlation_direction": pl.String,
                "common_months": pl.Int64,
            }
        ),
    )


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
    summary = pl.DataFrame(summary_rows, schema=_empty_factor_summary().schema)
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
        schema=_empty_rank_ic().schema,
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
        schema=_empty_quantile_returns().schema,
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
        schema=_empty_subperiod_metrics().schema,
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
        schema=_empty_factor_turnover().schema,
    )
    classifications = pl.DataFrame(
        [
            {
                "factor_name": "trend_60",
                "primary_score_variant": "score_size_neutral",
                "classification": "candidate",
                "reason": "candidate:all_thresholds_passed",
            }
        ],
        schema=_empty_classifications().schema,
    )
    redundancy = FactorRedundancyBundle(
        factor_correlations=_empty_redundancy_bundle().factor_correlations,
        redundancy_flags=pl.DataFrame(
            [
                {
                    "factor_a": "trend_60",
                    "factor_b": "size_proxy",
                    "primary_variant_a": "score_size_neutral",
                    "primary_variant_b": "score",
                    "mean_correlation": -0.75,
                    "absolute_correlation": 0.75,
                    "correlation_direction": "negative",
                    "common_months": 144,
                }
            ],
            schema=_empty_redundancy_bundle().redundancy_flags.schema,
        ),
    )

    write_factor_cards(
        _evaluation_bundle(
            factor_summary=summary,
            rank_ic=rank_ic,
            quantile_returns=quantiles,
            subperiod_metrics=subperiods,
            factor_turnover=turnover,
        ),
        classifications,
        redundancy,
        [trend, size],
        _settings(),
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
    paths = write_factor_cards(
        _evaluation_bundle(factor_summary=_missing_factor_summary()),
        _empty_classifications(),
        _empty_redundancy_bundle(),
        FACTOR_DEFINITIONS,
        _settings(),
        tmp_path,
    )

    assert len(paths) == 14
    assert [path.name for path in paths] == [
        f"{definition.name}.md" for definition in FACTOR_DEFINITIONS
    ]
    for definition in FACTOR_DEFINITIONS:
        card = tmp_path / "cards" / f"{definition.name}.md"
        assert card.is_file()
        card_text = card.read_text(encoding="utf-8")
        assert "机器评价缺失原因：`no_eligible_rows`" in card_text
        for filename in ("ic.png", "quantiles.png", "decay.png", "subperiods.png"):
            figure = tmp_path / "figures" / definition.name / filename
            assert figure.is_file()
            assert figure.stat().st_size > 100


def test_card_writer_has_no_raw_panel_or_file_reader_interface(tmp_path: Path) -> None:
    assert "panel" not in inspect.signature(write_factor_cards).parameters
    with pytest.raises(TypeError, match="panel"):
        write_factor_cards(
            _evaluation_bundle(),
            _empty_classifications(),
            _empty_redundancy_bundle(),
            [NEUTRAL],
            _settings(),
            tmp_path,
            panel=_panel([]),
        )
