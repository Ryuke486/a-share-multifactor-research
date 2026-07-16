from datetime import date
import hashlib
import json
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


def _sealed_protocol(metrics: tuple[str, ...] = SEALED_METRICS) -> dict[str, object]:
    payload: dict[str, object] = {"status": "sealed", "metrics": list(metrics)}
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    payload["sealed_protocol_sha256"] = hashlib.sha256(canonical).hexdigest()
    return payload


def _complete_comparison() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "scope": "factor" if metric in SEALED_METRICS[:3] else "portfolio",
                "entity": "composite" if metric in SEALED_METRICS[:3] else "main",
                "metric": metric,
                "research": 0.12,
                "validation": 0.08,
                "test": -0.02,
            }
            for metric in SEALED_METRICS
        ]
    )


def test_final_metrics_compute_only_the_sealed_factor_and_portfolio_metrics() -> None:
    rank_ic = _twenty_day_rank_ic()
    groups = _complete_factor_groups()
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
        sealed_protocol=_sealed_protocol(),
    )

    values = {
        (row["scope"], row["entity"], row["metric"]): row["value"]
        for row in metrics.iter_rows(named=True)
    }
    assert set(metrics.get_column("metric")) == set(SEALED_METRICS)
    assert values[("factor", "composite", "rank_ic")] == pytest.approx(0.15)
    assert values[("factor", "composite", "group_spread")] == pytest.approx(0.12)
    assert values[("factor", "composite", "group_monotonicity")] == pytest.approx(1.0)
    expected_turnover = 102.01 / (2.0 * ((100.0 + 102.01) / 2.0))
    assert values[("portfolio", "main", "turnover")] == pytest.approx(
        expected_turnover
    )
    assert values[("portfolio", "main", "target_deviation")] == pytest.approx(0.15)
    assert values[("portfolio", "main", "unfilled_rate")] == pytest.approx(0.25)
    assert values[("portfolio", "main", "maximum_drawdown")] == pytest.approx(0.0)
    assert values[("portfolio", "main", "cost_erosion")] > 0.0


@pytest.mark.parametrize("horizon", [5, 60])
def test_final_factor_metrics_require_frozen_twenty_day_horizon(
    horizon: int,
) -> None:
    rank_ic = pl.DataFrame(
        {
            "date": [date(2022, 1, 31)],
            "factor_name": ["composite"],
            "score_variant": ["score"],
            "horizon": [horizon],
            "rank_ic": [0.1],
        }
    )

    with pytest.raises(ValueError, match="frozen 20-day horizon"):
        compute_final_test_metrics(
            period="test",
            factor_rank_ic=rank_ic,
            factor_groups=_complete_factor_groups(),
            backtest={},
            metric_manifest=SEALED_METRICS,
            sealed_protocol=_sealed_protocol(),
        )


def test_final_factor_metrics_reject_groups_without_q5() -> None:
    groups = _complete_factor_groups().filter(pl.col("quantile") != 5)

    with pytest.raises(ValueError, match="complete frozen five groups"):
        compute_final_test_metrics(
            period="test",
            factor_rank_ic=_twenty_day_rank_ic(),
            factor_groups=groups,
            backtest={},
            metric_manifest=SEALED_METRICS,
            sealed_protocol=_sealed_protocol(),
        )


def test_final_factor_metrics_reject_one_month_with_a_missing_group() -> None:
    groups = _complete_factor_groups().filter(
        ~(
            (pl.col("date") == date(2022, 2, 28))
            & (pl.col("quantile") == 3)
        )
    )

    with pytest.raises(ValueError, match="complete frozen five groups"):
        compute_final_test_metrics(
            period="test",
            factor_rank_ic=_twenty_day_rank_ic(),
            factor_groups=groups,
            backtest={},
            metric_manifest=SEALED_METRICS,
            sealed_protocol=_sealed_protocol(),
        )


def _twenty_day_rank_ic() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [date(2022, 1, 31), date(2022, 2, 28)],
            "factor_name": ["composite", "composite"],
            "score_variant": ["score", "score"],
            "horizon": [20, 20],
            "rank_ic": [0.10, 0.20],
        }
    )


def _complete_factor_groups() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [date(2022, 1, 31)] * 5 + [date(2022, 2, 28)] * 5,
            "factor_name": ["composite"] * 10,
            "score_variant": ["score"] * 10,
            "quantile": [1, 2, 3, 4, 5] * 2,
            "mean_forward_return_20": [
                -0.02,
                -0.01,
                0.0,
                0.01,
                0.02,
                -0.10,
                -0.05,
                0.0,
                0.05,
                0.10,
            ],
        }
    )


def test_final_metrics_require_exact_verified_seal_metric_order() -> None:
    with pytest.raises(ValueError, match="exactly match"):
        compute_final_test_metrics(
            period="test",
            factor_rank_ic=pl.DataFrame(),
            factor_groups=pl.DataFrame(),
            backtest={},
            metric_manifest=SEALED_METRICS[:-1],
            sealed_protocol=_sealed_protocol(),
        )


    with pytest.raises(ValueError, match="seal hash"):
        compute_final_test_metrics(
            period="test",
            factor_rank_ic=pl.DataFrame(),
            factor_groups=pl.DataFrame(),
            backtest={},
            metric_manifest=SEALED_METRICS,
            sealed_protocol=_sealed_protocol() | {"metrics": list(reversed(SEALED_METRICS))},
        )


def test_comparison_keeps_periods_side_by_side_without_pooling() -> None:
    rows = []
    for period, offset in (("research", 0.0), ("validation", 0.1), ("test", 0.2)):
        period_rows = []
        for index, metric in enumerate(SEALED_METRICS):
            period_rows.append(
                {
                    "period": period,
                    "scope": "factor" if metric in SEALED_METRICS[:3] else "portfolio",
                    "entity": "composite" if metric in SEALED_METRICS[:3] else "main",
                    "metric": metric,
                    "value": float(index) + offset,
                }
            )
        rows.append(
            pl.DataFrame(period_rows)
        )

    comparison = build_period_comparison(
        rows,
        metric_manifest=SEALED_METRICS,
        sealed_protocol=_sealed_protocol(),
    )

    assert comparison.height == len(SEALED_METRICS)
    annual = comparison.filter(pl.col("metric") == "annual_return").row(
        0, named=True
    )
    assert annual["research"] == 3.0
    assert annual["validation"] == 3.1
    assert annual["test"] == 3.2

    incomplete = rows.copy()
    incomplete[-1] = incomplete[-1].filter(pl.col("metric") != "unfilled_rate")
    with pytest.raises(ValueError, match="every sealed metric"):
        build_period_comparison(
            incomplete,
            metric_manifest=SEALED_METRICS,
            sealed_protocol=_sealed_protocol(),
        )


def test_report_verifies_frozen_template_and_traces_words_to_machine_rows(
    tmp_path: Path,
) -> None:
    template = tmp_path / "report-template.md"
    template.write_text(
        "# 最终测试\n\n## 1. 冻结身份\n\n{{identity}}\n\n"
        "## 2. 一次性测试结果\n\n{{comparison_table}}\n\n"
        "## 3. 预注册稳健性对照\n\n"
        "固定结论不重新选模。`machine_policy:no_reselection`\n\n"
        "## 4. 失败运行与异常披露\n\n{{failed_runs}}\n\n"
        "## 5. 结论与限制\n\n"
        "不根据测试结果修改模型。`machine_policy:no_test_tuning`\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(template.read_bytes()).hexdigest()
    comparison = _complete_comparison()

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
    assert "固定结论不重新选模" in report
    assert "machine_policy:no_test_tuning" in report
    assert "{{" not in report

    with pytest.raises(ValueError, match="template hash"):
        render_final_test_report(
            template,
            expected_template_sha256="0" * 64,
            comparison=comparison,
            identity={},
            failed_runs=(),
        )

    with pytest.raises(ValueError, match="identity"):
        render_final_test_report(
            template,
            expected_template_sha256=digest,
            comparison=comparison,
            identity={},
            failed_runs=(),
        )

    with pytest.raises(ValueError, match="every sealed metric"):
        render_final_test_report(
            template,
            expected_template_sha256=digest,
            comparison=comparison.head(1),
            identity={
                "sealed_protocol_sha256": "abc",
                "approval_id": "approval-1",
                "attempt_id": "attempt-1",
                "git_commit": "def",
                "robustness_release": "stage8",
            },
            failed_runs=(),
        )
