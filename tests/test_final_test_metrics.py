from datetime import date
import hashlib
from pathlib import Path

import polars as pl
import pytest

from ashare_multifactor.final_test.metrics import (
    build_period_comparison,
    compute_final_test_metrics,
)
from ashare_multifactor.final_test.report import render_final_test_report


SEALED_METRICS = (
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


def test_final_metrics_compute_only_the_sealed_factor_and_portfolio_metrics() -> None:
    rank_ic = pl.DataFrame(
        {
            "factor_name": ["composite", "composite"],
            "rank_ic": [0.10, 0.20],
        }
    )
    groups = pl.DataFrame(
        {
            "factor_name": ["composite"] * 5,
            "quantile": [1, 2, 3, 4, 5],
            "mean_forward_return_20": [-0.02, -0.01, 0.0, 0.01, 0.02],
        }
    )
    backtest = {
        "nav": pl.DataFrame(
            {
                "date": [date(2022, 1, 3), date(2022, 1, 4)],
                "nav": [100.0, 102.01],
                "zero_cost_nav": [100.0, 104.04],
            }
        ),
        "orders": pl.DataFrame(
            {
                "order_id": ["a", "b"],
                "quantity": [100, 300],
                "remaining_quantity": [0, 100],
            }
        ),
        "trades": pl.DataFrame({"amount": [50.0, 52.01]}),
        "target_diagnostics": pl.DataFrame(
            {"target_deviation_l1": [0.10, 0.20]}
        ),
    }
    metrics = compute_final_test_metrics(
        period="test",
        factor_rank_ic=rank_ic,
        factor_groups=groups,
        backtest=backtest,
        metric_manifest=SEALED_METRICS,
    )

    values = {
        (row["scope"], row["entity"], row["metric"]): row["value"]
        for row in metrics.iter_rows(named=True)
    }
    assert set(metrics.get_column("metric")) == set(SEALED_METRICS)
    assert values[("factor", "composite", "rank_ic")] == pytest.approx(0.15)
    assert values[("factor", "composite", "group_spread")] == pytest.approx(0.04)
    assert values[("factor", "composite", "group_monotonicity")] == pytest.approx(1.0)
    expected_turnover = 102.01 / (2.0 * ((100.0 + 102.01) / 2.0))
    assert values[("portfolio", "main", "turnover")] == pytest.approx(
        expected_turnover
    )
    assert values[("portfolio", "main", "target_deviation")] == pytest.approx(0.15)
    assert values[("portfolio", "main", "unfilled_rate")] == pytest.approx(0.25)
    assert values[("portfolio", "main", "maximum_drawdown")] == pytest.approx(0.0)
    assert values[("portfolio", "main", "cost_erosion")] > 0.0


def test_final_metrics_reject_any_metric_not_in_the_sealed_allowlist() -> None:
    with pytest.raises(ValueError, match="not pre-registered"):
        compute_final_test_metrics(
            period="test",
            factor_rank_ic=pl.DataFrame(),
            factor_groups=pl.DataFrame(),
            backtest={},
            metric_manifest=("annual_return", "calmar_ratio"),
        )


def test_metric_subset_does_not_consume_inputs_for_unsealed_metrics() -> None:
    metrics = compute_final_test_metrics(
        period="test",
        factor_rank_ic=pl.DataFrame(),
        factor_groups=pl.DataFrame(),
        backtest={
            "nav": pl.DataFrame(
                {
                    "date": [date(2022, 1, 3), date(2022, 1, 4)],
                    "nav": [100.0, 101.0],
                    "zero_cost_nav": [100.0, 102.0],
                }
            )
        },
        metric_manifest=("annual_return",),
    )

    assert metrics.get_column("metric").to_list() == ["annual_return"]


def test_comparison_keeps_periods_side_by_side_without_pooling() -> None:
    rows = []
    for period, value in (("research", 0.12), ("validation", 0.08), ("test", -0.02)):
        rows.append(
            pl.DataFrame(
                {
                    "period": [period],
                    "scope": ["portfolio"],
                    "entity": ["main"],
                    "metric": ["annual_return"],
                    "value": [value],
                }
            )
        )

    comparison = build_period_comparison(rows, metric_manifest=("annual_return",))

    assert comparison.to_dicts() == [
        {
            "scope": "portfolio",
            "entity": "main",
            "metric": "annual_return",
            "research": 0.12,
            "validation": 0.08,
            "test": -0.02,
        }
    ]


def test_report_verifies_frozen_template_and_traces_words_to_machine_rows(
    tmp_path: Path,
) -> None:
    template = tmp_path / "report-template.md"
    template.write_text(
        "# 最终测试\n\n## 1. 冻结身份\n\n## 2. 一次性测试结果\n\n## 3. 预注册稳健性对照\n\n"
        "## 4. 失败运行与异常披露\n\n## 5. 结论与限制\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(template.read_bytes()).hexdigest()
    comparison = pl.DataFrame(
        {
            "scope": ["portfolio"],
            "entity": ["main"],
            "metric": ["annual_return"],
            "research": [0.12],
            "validation": [0.08],
            "test": [-0.02],
        }
    )

    report = render_final_test_report(
        template,
        expected_template_sha256=digest,
        comparison=comparison,
        identity={
            "sealed_protocol_sha256": "abc",
            "approval_id": "approval-1",
            "attempt_id": "attempt-1",
            "git_commit": "def",
            "robustness_release": "stage8",
        },
        failed_runs=(),
    )

    assert "12.00%" in report and "8.00%" in report and "-2.00%" in report
    assert "machine_result:portfolio/main/annual_return" in report
    assert "不合并期间重新选模" in report
    assert "根据最终测试结果返回修改模型" in report

    with pytest.raises(ValueError, match="template hash"):
        render_final_test_report(
            template,
            expected_template_sha256="0" * 64,
            comparison=comparison,
            identity={},
            failed_runs=(),
        )
