from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

import polars as pl

from ashare_multifactor.audit.identity import code_identity
from ashare_multifactor.audit.publication import PublishedRelease, publish_release
from ashare_multifactor.audit.records import file_record, sha256_file
from ashare_multifactor.config import load_config
from ashare_multifactor.execution.broker import BacktestSettings, run_backtest
from ashare_multifactor.execution.fees import load_market_rules
from ashare_multifactor.execution.shadow_nav import shadow_nav_audit
from ashare_multifactor.execution.stale_audit import audit_stale_positions
from ashare_multifactor.research.formal_backtest_inputs import load_formal_backtest_inputs
from ashare_multifactor.research.formal_backtest_metrics import backtest_summary
from ashare_multifactor.research.formal_backtest_paths import formal_backtest_paths


def _validate_source_coverage(
    source_cache: Path, *, expected_symbols: int
) -> tuple[dict[str, object], list[str]]:
    root = source_cache / "cninfo_dividend"
    batch_path = root / "batch_metadata.json"
    payment_path = root / "normalized_payment_dates.parquet"
    payment_metadata_path = root / "normalized_payment_dates.metadata.json"
    blockers: list[str] = []
    if not batch_path.is_file() or not payment_path.is_file() or not payment_metadata_path.is_file():
        return {"status": "blocked"}, ["free corporate action cache is incomplete"]
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    payment = json.loads(payment_metadata_path.read_text(encoding="utf-8"))
    cached = int(batch.get("cached_count", 0))
    errors = batch.get("errors", {})
    missing = int(payment.get("missing_count", -1))
    actual_hash = sha256_file(payment_path)
    if cached != expected_symbols:
        blockers.append("free corporate action cache does not cover every target symbol")
    if errors:
        blockers.append("free corporate action cache contains request errors")
    if missing != 0:
        blockers.append("cash dividend payment dates remain missing")
    if payment.get("sha256") != actual_hash:
        blockers.append("normalized payment date hash does not match metadata")
    coverage = {
        "status": "ready" if not blockers else "blocked",
        "expected_symbols": expected_symbols,
        "cached_symbols": cached,
        "request_errors": len(errors),
        "payment_dates_missing": missing,
        "payment_dates_sha256": actual_hash,
    }
    return coverage, blockers


def _load_stale_evidence(config_path: Path, cache: Path) -> pl.DataFrame:
    evidence = pl.read_csv(
        config_path,
        schema_overrides={"symbol": pl.String},
        try_parse_dates=True,
    )
    for row in evidence.iter_rows(named=True):
        path = cache / row["cache_file"]
        if not path.is_file() or sha256_file(path) != row["sha256"]:
            raise ValueError(f"stale evidence hash mismatch: {row['symbol']}")
        if not str(row["source_url"]).startswith("https://"):
            raise ValueError(f"invalid stale evidence URL: {row['symbol']}")
    return evidence


def _readiness_markdown(audit: dict[str, object]) -> str:
    coverage = audit.get("source_coverage", {})
    blockers = audit.get("blockers", [])
    blocker_text = "\n".join(f"- {item}" for item in blockers) or "- 无"
    return f"""# 阶段六数据准备度审计

**结论：** `{audit['status']}`

## 结构化门禁

- 阶段五目标行：{int(audit.get('target_rows', 0)):,}
- 执行面板行：{int(audit.get('execution_rows', 0)):,}
- 公司行动行：{int(audit.get('corporate_action_rows', 0)):,}
- 巨潮免费缓存证券：{int(coverage.get('cached_symbols', 0)):,}
- 现金派息日缺失：{int(coverage.get('payment_dates_missing', -1))}

## 阻断项

{blocker_text}

审计仅覆盖2005–2016研究期；2017年及以后数据继续封存。
"""


def audit_formal_backtest(root: Path) -> dict[str, object]:
    root = root.resolve()
    paths = formal_backtest_paths(root)
    upstream_root = _upstream_data_root(root)
    configured = load_config(root / "configs" / "research_protocol.yaml").formal_backtest
    if configured is None:
        return {"status": "blocked", "reason": "formal backtest config missing"}
    inputs = load_formal_backtest_inputs(
        upstream_root,
        paths.artifacts / "source_cache",
        adv_lookback=configured.adv_lookback,
    )
    rules = load_market_rules(root / "configs" / "market_rules.yaml")
    required_security_events = {"000618", "000515", "000569"}
    event_symbols = set(inputs.security_events["source_symbol"].to_list())
    blockers = []
    if not required_security_events <= event_symbols:
        blockers.append("required delisting and stock-merger events are incomplete")
    source_coverage, source_blockers = _validate_source_coverage(
        paths.artifacts / "source_cache",
        expected_symbols=len(set(inputs.execution_panel["symbol"].to_list())),
    )
    blockers.extend(source_blockers)
    try:
        stale_evidence = _load_stale_evidence(
            root / "configs/stale_valuation_evidence.csv",
            paths.artifacts / "source_cache/stale_evidence",
        )
    except (FileNotFoundError, ValueError) as error:
        stale_evidence = pl.DataFrame()
        blockers.append(str(error))
    status = "ready" if not blockers else "conditionally_ready"
    return {
        "status": status,
        "blockers": blockers,
        "stage_five_run_id": inputs.stage_five.run_id,
        "target_rows": inputs.target_weights.height,
        "target_dates": inputs.target_weights["date"].n_unique(),
        "execution_rows": inputs.execution_panel.height,
        "corporate_action_rows": inputs.corporate_actions.height,
        "source_coverage": source_coverage,
        "stale_evidence_rows": stale_evidence.height,
        "research_end_stamp_duty_sell": rules.stamp_duty_rate(
            inputs.execution_panel["date"].max(), "sell"
        ),
    }


def run_formal_pipeline(
    root: Path,
    *,
    publish: bool,
    run_id: str | None = None,
    superseded_run_id: str | None = None,
    supersession_reason: str | None = None,
) -> Path | PublishedRelease:
    root = root.resolve()
    if bool(superseded_run_id) != bool(supersession_reason):
        raise ValueError("superseded run and reason must be supplied together")
    paths = formal_backtest_paths(root)
    upstream_root = _upstream_data_root(root)
    audit = audit_formal_backtest(root)
    if audit["status"] != "ready":
        raise ValueError(f"formal backtest readiness gate failed: {audit}")
    configured = load_config(root / "configs" / "research_protocol.yaml").formal_backtest
    if configured is None:
        raise ValueError("formal backtest configuration is missing")
    inputs = load_formal_backtest_inputs(
        upstream_root,
        paths.artifacts / "source_cache",
        adv_lookback=configured.adv_lookback,
    )
    rules_path = root / "configs" / "market_rules.yaml"
    rules = load_market_rules(rules_path)
    if (
        rules.commission_rate != configured.commission_rate
        or rules.minimum_commission != configured.minimum_commission
    ):
        raise ValueError("commission assumptions disagree across frozen configs")
    identity = code_identity(root)
    if publish and identity["dirty"]:
        raise ValueError("formal release requires a clean Git identity")
    settings = BacktestSettings(
        initial_cash=configured.initial_cash,
        maximum_participation=configured.maximum_participation,
        fixed_slippage_bps=configured.fixed_slippage_bps,
        reference_impact_bps=configured.reference_impact_bps,
        reference_participation=configured.reference_participation,
        maximum_impact_bps=configured.maximum_impact_bps,
        buy_lot_size=configured.buy_lot_size,
    )
    result = run_backtest(
        inputs.execution_panel,
        inputs.target_weights,
        inputs.corporate_actions,
        rules,
        settings,
        inputs.security_events,
    )
    summary = backtest_summary(result, initial_cash=settings.initial_cash)
    if summary["maximum_reconciliation_difference"] > 0.01:
        raise ValueError("reconciliation gate failed")
    actual_run_id = run_id or uuid.uuid4().hex
    work = paths.artifacts / "validation_runs" / actual_run_id
    if work.exists():
        raise FileExistsError(work)
    datasets = work / "datasets"
    artifacts = work / "artifacts"
    datasets.mkdir(parents=True)
    artifacts.mkdir()
    shadow_summary, shadow_daily = shadow_nav_audit(
        inputs.execution_panel,
        inputs.corporate_actions,
        result["positions"],
        result["nav"],
        threshold=configured.shadow_divergence_threshold,
    )
    if shadow_summary["status"] != "ready":
        raise ValueError(f"adjusted-price shadow NAV gate failed: {shadow_summary}")
    stale_summary, stale_intervals = audit_stale_positions(
        result["positions"],
        result["nav"],
        inputs.execution_panel,
        inputs.security_events,
        _load_stale_evidence(
            root / "configs/stale_valuation_evidence.csv",
            paths.artifacts / "source_cache/stale_evidence",
        ),
        review_days=configured.stale_review_days,
    )
    if stale_summary["status"] != "ready":
        raise ValueError(f"unexplained stale valuation gate failed: {stale_summary}")
    runtime_readiness = {
        **audit,
        "status": "ready",
        "shadow_nav": shadow_summary,
        "stale_valuation": stale_summary,
        "maximum_reconciliation_difference": summary[
            "maximum_scenario_reconciliation_difference"
        ],
    }
    inputs.execution_panel.write_parquet(datasets / "execution_panel.parquet")
    inputs.corporate_actions.write_parquet(datasets / "corporate_actions.parquet")
    inputs.security_events.write_parquet(datasets / "security_events.parquet")
    for name, frame in result.items():
        frame.write_parquet(datasets / f"{name}.parquet")
    (artifacts / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (artifacts / "shadow_nav_audit.json").write_text(
        json.dumps(shadow_summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    shadow_daily.write_parquet(artifacts / "shadow_nav_daily.parquet")
    (artifacts / "stale_audit.json").write_text(
        json.dumps(stale_summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    stale_intervals.write_csv(artifacts / "stale_intervals.csv")
    (artifacts / "readiness.json").write_text(
        json.dumps(runtime_readiness, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _cost_breakdown(result["trades"]).write_csv(artifacts / "cost_breakdown.csv")
    _fill_statistics(result).write_csv(artifacts / "fill_statistics.csv")
    blocked = (
        result["order_events"]
        .filter(pl.col("reason").is_not_null())
        .group_by("reason")
        .len()
        .sort("len", descending=True)
    )
    blocked.write_csv(artifacts / "blocked_orders.csv")
    (artifacts / "report.md").write_text(_report(summary, blocked), encoding="utf-8")
    (artifacts / "readiness_report.md").write_text(
        _readiness_markdown(runtime_readiness), encoding="utf-8"
    )
    lineage = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "stage_five": {
            "run_id": inputs.stage_five.run_id,
            "manifest_sha256": inputs.stage_five.manifest_sha256,
        },
        "rules": file_record(rules_path, root=root, role="market_rules").to_dict(),
        "research_protocol": file_record(
            root / "configs/research_protocol.yaml",
            root=root,
            role="research_protocol",
        ).to_dict(),
        "daily_panel": [
            file_record(path, root=upstream_root, role="daily_panel").to_dict()
            for path in (
                upstream_root / "processed/factor_research/daily_panel/manifest.json",
                *(
                    upstream_root
                    / f"processed/factor_research/daily_panel/year={year}/part-000.parquet"
                    for year in range(2005, 2017)
                ),
            )
        ],
        "corporate_action_sources": [
            file_record(path, root=root, role="corporate_action_source").to_dict()
            for path in (
                paths.artifacts / "source_cache/eastmoney_fhps/normalized_actions.parquet",
                paths.artifacts
                / "source_cache/sina_corporate_actions/normalized_dividends.parquet",
                paths.artifacts
                / "source_cache/cninfo_dividend/normalized_payment_dates.parquet",
                paths.artifacts / "source_cache/cninfo_dividend/batch_metadata.json",
                paths.artifacts
                / "source_cache/cninfo_dividend/normalized_payment_dates.metadata.json",
                paths.artifacts
                / "source_cache/cninfo_official_pdfs/official_special_actions.csv",
            )
        ],
        "security_events": file_record(
            root / "configs/corporate_action_exceptions.csv",
            root=root,
            role="security_events",
        ).to_dict(),
        "stale_valuation_evidence": file_record(
            root / "configs/stale_valuation_evidence.csv",
            root=root,
            role="stale_valuation_evidence",
        ).to_dict(),
        "stale_evidence_sources": [
            file_record(
                paths.artifacts / "source_cache/stale_evidence" / row["cache_file"],
                root=root,
                role="stale_evidence_source",
            ).to_dict()
            for row in _load_stale_evidence(
                root / "configs/stale_valuation_evidence.csv",
                paths.artifacts / "source_cache/stale_evidence",
            ).iter_rows(named=True)
        ],
        "code": identity,
        "runtime_gates": {
            "readiness": runtime_readiness,
            "shadow_nav": shadow_summary,
            "stale_valuation": stale_summary,
            "artifacts": [
                file_record(path, root=work, role="runtime_gate").to_dict()
                for path in (
                    artifacts / "readiness.json",
                    artifacts / "shadow_nav_audit.json",
                    artifacts / "shadow_nav_daily.parquet",
                    artifacts / "stale_audit.json",
                    artifacts / "stale_intervals.csv",
                )
            ],
        },
        "research_period": ["2005-01-01", "2016-12-31"],
        "supersedes": (
            {
                "run_id": superseded_run_id,
                "reason": supersession_reason,
            }
            if superseded_run_id is not None
            else None
        ),
    }
    (work / "lineage.json").write_text(
        json.dumps(lineage, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    content = [
        file_record(path, root=work, role="validation_output").to_dict()
        for path in sorted(work.rglob("*"))
        if path.is_file()
    ]
    (work / "content_manifest.json").write_text(
        json.dumps({"files": content}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not publish:
        return work
    release = publish_release(
        paths.processed,
        run_id=actual_run_id,
        staged_datasets=datasets,
        staged_artifacts=artifacts,
        lineage=lineage,
        manifest_metadata={"stage": "formal_backtest"},
    )
    return release


def compare_validation_runs(first: Path, second: Path) -> None:
    names = {
        "orders.parquet",
        "order_events.parquet",
        "trades.parquet",
        "cash_ledger.parquet",
        "receivable_ledger.parquet",
        "positions.parquet",
        "nav.parquet",
        "reconciliation.parquet",
        "scenario_reconciliation.parquet",
        "target_diagnostics.parquet",
        "summary.json",
        "shadow_nav_audit.json",
        "shadow_nav_daily.parquet",
        "stale_audit.json",
        "stale_intervals.csv",
        "cost_breakdown.csv",
        "fill_statistics.csv",
        "blocked_orders.csv",
        "report.md",
    }
    for name in names:
        left = next(first.rglob(name))
        right = next(second.rglob(name))
        if sha256_file(left) != sha256_file(right):
            raise ValueError(f"validation runs differ: {name}")


def _cost_breakdown(trades: pl.DataFrame) -> pl.DataFrame:
    return trades.select(
        pl.col("commission").sum(),
        pl.col("stamp_duty").sum(),
        pl.col("transfer_fee").sum(),
        pl.col("slippage_cost").sum(),
        pl.col("impact_cost").sum(),
        pl.col("total_cost").sum(),
    )


def _fill_statistics(result: dict[str, pl.DataFrame]) -> pl.DataFrame:
    orders = result["orders"]
    return pl.DataFrame(
        {
            "order_count": [orders.height],
            "filled": [orders.filter(pl.col("status") == "filled").height],
            "partially_filled": [
                orders.filter(pl.col("status") == "partially_filled").height
            ],
            "expired": [orders.filter(pl.col("status") == "expired").height],
            "trade_count": [result["trades"].height],
        }
    )


def _report(summary: dict[str, float | int], blocked: pl.DataFrame) -> str:
    blocked_text = "\n".join(
        f"- {row['reason']}: {row['len']}次" for row in blocked.to_dicts()
    ) or "- 无"
    return f"""# 2005–2016正式A股执行回测

本报告只覆盖研究期，不包含2017年及以后数据。阶段五组合与参数未因回测结果修改。

## 核心结果

- 完整成本总收益：{summary['total_return']:.4%}
- 仅显性费用总收益：{summary['explicit_fee_only_total_return']:.4%}
- 零成本同成交约束总收益：{summary['zero_cost_total_return']:.4%}
- 年化收益：{summary['annual_return']:.4%}
- 年化波动：{summary['annual_volatility']:.4%}
- Sharpe（零无风险利率）：{summary['sharpe_zero_rate']:.4f}
- 最大回撤：{summary['maximum_drawdown']:.4%}
- 成交笔数：{summary['trade_count']}
- 订单最终成交率：{summary['filled_order_rate']:.2%}
- 曾部分成交订单率：{summary['partially_filled_order_rate']:.2%}
- 平均订单等待：{summary['average_order_wait_days']:.2f}天
- 实际年化成交额/资产：{summary['annualized_traded_value_ratio']:.2f}倍
- 平均目标L1偏离：{summary['average_target_deviation_l1']:.2%}
- 实现短缺率（总成本/成交额）：{summary['implementation_shortfall_rate']:.4%}
- 月度胜率：{summary['monthly_win_rate']:.2%}
- 平均现金权重：{summary['average_cash_weight']:.2%}
- 总成本：{summary['total_cost']:.2f}元
- 最大逐日对账差：{summary['maximum_reconciliation_difference']:.8f}元
- 三账本最大逐日对账差：{summary['maximum_scenario_reconciliation_difference']:.8f}元
- 最长陈旧估值：{summary['maximum_stale_days']}天（单独审计并绑定停牌证据）

## 订单阻断

{blocked_text}

## 解释边界

这是日线级、开盘成交的保守执行模拟，不是盘口排队或实盘成交保证。佣金、滑点和冲击为冻结模型假设；公司行动来自免费公开源并用巨潮官方实施公告补齐特殊股改事件。后复权序列仅用于异常核验，不直接乘原始股数。
"""


def _upstream_data_root(root: Path) -> Path:
    if (root / "processed/factor_combination/CURRENT.json").is_file():
        return root
    if root.parent.name == ".worktrees":
        candidate = root.parent.parent
        if (candidate / "processed/factor_combination/CURRENT.json").is_file():
            return candidate
    raise FileNotFoundError("cannot locate authoritative upstream processed data")
