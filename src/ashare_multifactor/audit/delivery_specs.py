"""Declarative registry of the v1.0 key results cited by the delivered documents.

Each ``KeyResultSpec`` names one machine-generated value, where it is read from
(a frozen release or a tracked record) and which documents must quote it. The
checks that consume this registry live in ``ashare_multifactor.audit.delivery``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class SourceSpec:
    """Where a documented number comes from."""

    name: str
    kind: str
    root: str


@dataclass(frozen=True)
class ResultSelect:
    """How to pull one value out of a release artifact."""

    artifact: str
    kind: str = "json"
    field: str | None = None
    where: tuple[tuple[str, Any], ...] = ()
    stat: str | None = None
    denominator: str | None = None


@dataclass(frozen=True)
class KeyResultSpec:
    """One documented number, its machine source and its required citations."""

    key: str
    source: str
    select: ResultSelect
    required_documents: tuple[str, ...] = ()
    templates: tuple[str, ...] = ()


V1_DOCUMENTS = (
    "README.md",
    "reports/research_report.md",
    "docs/result_dictionary.md",
    "AGENTS.md",
)

V1_SOURCES = (
    SourceSpec("stage4_factor_research", "file", "artifacts/factor_research"),
    SourceSpec("stage5_factor_combination", "release", "processed/factor_combination"),
    SourceSpec("stage6_formal_backtest", "release", "processed/formal_backtest"),
    SourceSpec("stage7_validation_evaluation", "release", "processed/validation_evaluation"),
    SourceSpec("stage8_robustness", "release", "processed/robustness"),
    SourceSpec("v1_supplements", "file", "artifacts/v1_supplements"),
    SourceSpec("v1_robustness_subperiod", "file", "artifacts/v1_robustness_subperiod"),
)

_STAGE7_CANDIDATES = (
    "family_equal_size_stratified_buffered",
    "family_equal_top100_equal",
    "rolling_ic_family_size_stratified_buffered",
)
_STAGE7_METRICS = (
    "rank_ic",
    "icir",
    "net_annual_return",
    "maximum_drawdown",
    "turnover",
    "cost_erosion",
)
_STAGE8_COST_EXPERIMENTS = (
    ("stage8.execution_full_cost", "execution_full_cost"),
    ("stage8.execution_explicit_only", "execution_explicit_only"),
    ("stage8.execution_impact_high", "execution_impact_high"),
    ("stage8.execution_zero_cost", "execution_zero_cost"),
)


def v1_key_result_specs() -> tuple[KeyResultSpec, ...]:
    """The documented numbers that the v1.0 delivery is expected to state."""
    readme_report = ("README.md", "reports/research_report.md")
    report_dictionary = ("reports/research_report.md", "docs/result_dictionary.md")
    specs: list[KeyResultSpec] = [
        KeyResultSpec(
            "stage4.registered_factor_count",
            "stage4_factor_research",
            ResultSelect("factor_classifications.csv", kind="csv", stat="count"),
            required_documents=readme_report,
            templates=("{value} 个预注册因子",),
        ),
        KeyResultSpec(
            "stage4.candidate_count",
            "stage4_factor_research",
            ResultSelect(
                "factor_classifications.csv",
                kind="csv",
                where=(("classification", "candidate"),),
                stat="count",
            ),
            required_documents=readme_report,
            templates=("{value} 个 candidate",),
        ),
        KeyResultSpec(
            "stage4.watch_count",
            "stage4_factor_research",
            ResultSelect(
                "factor_classifications.csv",
                kind="csv",
                where=(("classification", "watch"),),
                stat="count",
            ),
            required_documents=readme_report,
            templates=("{value} 个 watch",),
        ),
        KeyResultSpec(
            "stage4.reject_count",
            "stage4_factor_research",
            ResultSelect(
                "factor_classifications.csv",
                kind="csv",
                where=(("classification", "reject"),),
                stat="count",
            ),
            required_documents=readme_report,
            templates=("{value} 个 reject",),
        ),
        KeyResultSpec(
            "stage5.family_equal.rank_ic_20",
            "stage5_factor_combination",
            ResultSelect(
                "artifacts/composite_summary.csv",
                kind="csv",
                field="mean_ic",
                where=(("method", "family_equal"), ("horizon", 20)),
            ),
            required_documents=("README.md",) + report_dictionary,
        ),
        KeyResultSpec(
            "stage5.family_equal.icir_20",
            "stage5_factor_combination",
            ResultSelect(
                "artifacts/composite_summary.csv",
                kind="csv",
                field="icir",
                where=(("method", "family_equal"), ("horizon", 20)),
            ),
            required_documents=("README.md",) + report_dictionary,
        ),
        KeyResultSpec(
            "stage5.rolling_ic.rank_ic_20",
            "stage5_factor_combination",
            ResultSelect(
                "artifacts/composite_summary.csv",
                kind="csv",
                field="mean_ic",
                where=(("method", "rolling_ic_family"), ("horizon", 20)),
            ),
            required_documents=("AGENTS.md",),
        ),
        KeyResultSpec(
            "stage5.size_stratified.turnover",
            "stage5_factor_combination",
            ResultSelect(
                "artifacts/portfolio_turnover.csv",
                kind="csv",
                field="one_way_turnover",
                where=(("portfolio_name", "size_stratified_buffered"),),
                stat="mean",
            ),
            required_documents=("AGENTS.md",) + report_dictionary,
        ),
        KeyResultSpec(
            "stage5.top100.turnover",
            "stage5_factor_combination",
            ResultSelect(
                "artifacts/portfolio_turnover.csv",
                kind="csv",
                field="one_way_turnover",
                where=(("portfolio_name", "top100_equal"),),
                stat="mean",
            ),
            required_documents=("AGENTS.md", "reports/research_report.md"),
        ),
    ]
    for field in (
        "total_return",
        "annual_return",
        "annual_volatility",
        "sharpe_zero_rate",
        "maximum_drawdown",
        "trade_count",
        "filled_order_rate",
        "annualized_traded_value_ratio",
        "implementation_shortfall_rate",
        "average_target_deviation_l1",
        "maximum_scenario_reconciliation_difference",
    ):
        required = report_dictionary
        if field in (
            "annual_return",
            "maximum_drawdown",
            "implementation_shortfall_rate",
        ):
            required = ("README.md",) + report_dictionary
        specs.append(
            KeyResultSpec(
                f"stage6.{field}",
                "stage6_formal_backtest",
                ResultSelect("artifacts/summary.json", field=field),
                required_documents=required,
            )
        )
    for side in ("buy", "sell"):
        specs.append(
            KeyResultSpec(
                f"stage6.unfilled_quantity_share.{side}",
                "stage6_formal_backtest",
                ResultSelect(
                    "datasets/orders.parquet",
                    kind="parquet",
                    field="remaining_quantity",
                    denominator="quantity",
                    where=(("side", side),),
                    stat="sum_ratio",
                ),
                required_documents=report_dictionary,
            )
        )
    specs.append(
        KeyResultSpec(
            "stage7.selected_candidate",
            "stage7_validation_evaluation",
            ResultSelect("artifacts/research/selection_decision.json", field="selected_candidate"),
            required_documents=("README.md",) + report_dictionary,
        )
    )
    for candidate in _STAGE7_CANDIDATES:
        for metric in _STAGE7_METRICS:
            required = report_dictionary
            if candidate == "rolling_ic_family_size_stratified_buffered" and metric in (
                "rank_ic",
                "net_annual_return",
                "maximum_drawdown",
            ):
                required = ("README.md",) + report_dictionary
            specs.append(
                KeyResultSpec(
                    f"stage7.{candidate}.{metric}",
                    "stage7_validation_evaluation",
                    ResultSelect(
                        "artifacts/research/validation_metrics.parquet",
                        kind="parquet",
                        field=metric,
                        where=(("candidate", candidate),),
                    ),
                    required_documents=required,
                )
            )
    for key, experiment in _STAGE8_COST_EXPERIMENTS:
        for field in ("value", "baseline_delta"):
            specs.append(
                KeyResultSpec(
                    f"{key}.{field}",
                    "stage8_robustness",
                    ResultSelect(
                        "datasets/robustness_results.parquet",
                        kind="parquet",
                        field=field,
                        where=(("experiment_id", experiment), ("metric", "annual_return")),
                    ),
                    required_documents=report_dictionary,
                )
            )
    for label, templates in (
        ("stable", ("{value} 个稳定实验",)),
        ("sensitive", ("{value} 个敏感实验",)),
        ("failed", ("{value} 个失败或未运行实验",)),
    ):
        specs.append(
            KeyResultSpec(
                f"stage8.count.{label}",
                "stage8_robustness",
                ResultSelect(
                    "datasets/robustness_results.parquet",
                    kind="parquet",
                    field="experiment_id",
                    where=(("interpretation", label),),
                    stat="n_unique",
                ),
                required_documents=readme_report,
                templates=templates,
            )
        )
    specs.append(
        KeyResultSpec(
            "stage8.main_candidate",
            "stage8_robustness",
            ResultSelect("lineage.json", field="main_candidate"),
        )
    )
    specs.extend(_supplement_specs())
    return tuple(specs)


_SUPPLEMENT_PERIODS = (
    ("research", "research_2005_2016", "research_path"),
    ("validation", "validation_2017_2021", "rolling_ic_family_size_stratified_buffered"),
)
_SUPPLEMENT_LEG_OBJECTS = (
    "family_equal",
    "rolling_ic_family",
    "amihud_20",
    "reversal_20",
    "reversal_5",
    "turnover_20",
    "volatility_20",
    "volatility_60",
)


_SUPPLEMENT_README_KEYS = frozenset(
    {
        "supplement.research.equal_weight.annual_return",
        "supplement.validation.equal_weight.annual_return",
        "supplement.validation.rolling_ic_family_size_stratified_buffered.net"
        ".equal_weight.annual_relative_return",
        "supplement.validation.rolling_ic_family_size_stratified_buffered.zero_cost"
        ".equal_weight.annual_relative_return",
        "supplement.legs.research_2005_2016.family_equal.long_leg",
        "supplement.legs.validation_2017_2021.family_equal.long_leg",
        "supplement.legs.validation_2017_2021.family_equal.short_leg",
    }
)


def _supplement_specs() -> list[KeyResultSpec]:
    """Benchmark and long/short-leg numbers from the descriptive v1.0 supplements."""
    report = ("reports/research_report.md",)
    specs: list[KeyResultSpec] = []

    def relative(key: str, where: tuple[tuple[str, Any], ...], field: str) -> None:
        specs.append(
            KeyResultSpec(
                f"supplement.{key}",
                "v1_supplements",
                ResultSelect("relative_performance.csv", kind="csv", field=field, where=where),
                required_documents=report,
            )
        )

    for label, period, portfolio in _SUPPLEMENT_PERIODS:
        for benchmark in ("equal_weight", "cap_weight"):
            relative(
                f"{label}.{benchmark}.annual_return",
                (
                    ("period", period),
                    ("portfolio", portfolio),
                    ("cost_mode", "net"),
                    ("benchmark", benchmark),
                ),
                "benchmark_annual_return",
            )
    portfolios = [(label, period, portfolio) for label, period, portfolio in _SUPPLEMENT_PERIODS]
    portfolios += [
        ("validation", "validation_2017_2021", candidate)
        for candidate in ("family_equal_size_stratified_buffered", "family_equal_top100_equal")
    ]
    for label, period, portfolio in portfolios:
        for cost_mode in ("net", "zero_cost"):
            where = (
                ("period", period),
                ("portfolio", portfolio),
                ("cost_mode", cost_mode),
                ("benchmark", "equal_weight"),
            )
            for field in ("annual_relative_return", "information_ratio", "beta"):
                relative(f"{label}.{portfolio}.{cost_mode}.equal_weight.{field}", where, field)
    for year in range(2017, 2022):
        for field in ("strategy_net", "equal_weight", "cap_weight"):
            specs.append(
                KeyResultSpec(
                    f"supplement.calendar.{year}.{field}",
                    "v1_supplements",
                    ResultSelect(
                        "calendar_year_returns.csv",
                        kind="csv",
                        field=field,
                        where=(("year", year),),
                    ),
                    required_documents=report,
                )
            )
    for _, period, _ in _SUPPLEMENT_PERIODS:
        for name in _SUPPLEMENT_LEG_OBJECTS:
            for field in ("long_leg", "long_leg_t", "short_leg", "short_leg_t"):
                specs.append(
                    KeyResultSpec(
                        f"supplement.legs.{period}.{name}.{field}",
                        "v1_supplements",
                        ResultSelect(
                            "leg_decomposition.csv",
                            kind="csv",
                            field=field,
                            where=(("period", period), ("object_name", name)),
                        ),
                        required_documents=report,
                    )
                )
    specs.extend(_factor_statistic_specs(report))
    specs.extend(_inference_specs(report))
    specs.extend(_robustness_subperiod_specs(report))
    specs.extend(_index_benchmark_specs(report))
    return [
        replace(spec, required_documents=("README.md", *spec.required_documents))
        if spec.key in _SUPPLEMENT_README_KEYS
        else spec
        for spec in specs
    ]


_REGISTERED_FACTORS = (
    "amihud_20",
    "bp",
    "downside_volatility_60",
    "ep_ttm",
    "log_market_cap",
    "momentum_120",
    "momentum_12_1",
    "momentum_60",
    "reversal_20",
    "reversal_5",
    "sp_ttm",
    "turnover_20",
    "volatility_20",
    "volatility_60",
)
_EXECUTABLE_IC_OBJECTS = (*_SUPPLEMENT_LEG_OBJECTS, "momentum_60")


def _factor_statistic_specs(report: tuple[str, ...]) -> list[KeyResultSpec]:
    """Executable-label IC, Newey-West lag sensitivity and the momentum discussion."""
    specs: list[KeyResultSpec] = []

    def add(key: str, artifact: str, field: str, where: tuple[tuple[str, Any], ...]) -> None:
        specs.append(
            KeyResultSpec(
                key,
                "v1_supplements",
                ResultSelect(artifact, kind="csv", field=field, where=where),
                required_documents=report,
            )
        )

    for _, period, _ in _SUPPLEMENT_PERIODS:
        add(
            f"supplement.executable_ic.{period}.label_coverage",
            "executable_ic.csv",
            "label_coverage",
            (("period", period), ("object_name", "family_equal"), ("horizon", 20)),
        )
        cases = [(name, 20) for name in _EXECUTABLE_IC_OBJECTS] + [("reversal_5", 5)]
        for name, horizon in cases:
            where = (("period", period), ("object_name", name), ("horizon", horizon))
            for field in ("close_mean_ic", "open_mean_ic", "mean_ic_retained"):
                add(
                    f"supplement.executable_ic.{period}.{name}.h{horizon}.{field}",
                    "executable_ic.csv",
                    field,
                    where,
                )
    research = "research_2005_2016"
    for name in (*_REGISTERED_FACTORS, "family_equal", "rolling_ic_family"):
        for field in ("t_published_lag", "t_automatic_lag"):
            add(
                f"supplement.newey_west.{research}.{name}.{field}",
                "newey_west_lags.csv",
                field,
                (("period", research), ("object_name", name)),
            )
    for name in ("family_equal", "momentum_60"):
        for field in ("t_published_lag", "t_automatic_lag"):
            add(
                f"supplement.newey_west.validation_2017_2021.{name}.{field}",
                "newey_west_lags.csv",
                field,
                (("period", "validation_2017_2021"), ("object_name", name)),
            )
    for field in ("q_published_lag", "q_automatic_lag"):
        add(
            f"supplement.newey_west.{research}.log_market_cap.{field}",
            "newey_west_lags.csv",
            field,
            (("period", research), ("object_name", "log_market_cap")),
        )
    for period in (research, "validation_2017_2021"):
        names = (
            ("momentum_60", "momentum_120", "momentum_12_1")
            if period == research
            else (
                "momentum_60",
                "log_market_cap",
            )
        )
        for name in names:
            add(
                f"supplement.newey_west.{period}.{name}.mean_ic",
                "newey_west_lags.csv",
                "mean_ic",
                (("period", period), ("object_name", name)),
            )
    for field in ("bps", "share"):
        add(
            f"supplement.cost_components.impact_cost.{field}",
            "cost_components.csv",
            field,
            (("component", "impact_cost"),),
        )
    specs.append(
        KeyResultSpec(
            "stage4.correlation.momentum_60.reversal_20",
            "stage4_factor_research",
            ResultSelect(
                "factor_correlations.csv",
                kind="csv",
                field="mean_correlation",
                where=(("factor_a", "momentum_60"), ("factor_b", "reversal_20")),
            ),
            required_documents=report,
        )
    )
    return specs


_CANDIDATE_FACTORS = (
    "amihud_20",
    "reversal_20",
    "reversal_5",
    "turnover_20",
    "volatility_20",
    "volatility_60",
)
_BOOTSTRAP_PORTFOLIOS = (
    ("research_2005_2016", "research_path"),
    ("validation_2017_2021", "family_equal_size_stratified_buffered"),
    ("validation_2017_2021", "family_equal_top100_equal"),
    ("validation_2017_2021", "rolling_ic_family_size_stratified_buffered"),
)


def _inference_specs(report: tuple[str, ...]) -> list[KeyResultSpec]:
    """Fama-MacBeth premia and block-bootstrap intervals quoted in the report."""
    specs: list[KeyResultSpec] = []

    def add(key: str, artifact: str, field: str, where: tuple[tuple[str, Any], ...]) -> None:
        specs.append(
            KeyResultSpec(
                key,
                "v1_supplements",
                ResultSelect(artifact, kind="csv", field=field, where=where),
                required_documents=report,
            )
        )

    periods = ("research_2005_2016", "validation_2017_2021")
    for period in periods:
        for specification in ("candidates", "all_registered"):
            first = "amihud_20"
            where = (("period", period), ("specification", specification), ("factor_name", first))
            for field in ("mean_n_obs", "mean_r_squared"):
                if specification == "all_registered" and field == "mean_r_squared":
                    continue
                add(
                    f"supplement.fama_macbeth.{period}.{specification}.{field}",
                    "fama_macbeth.csv",
                    field,
                    where,
                )
        for name in _CANDIDATE_FACTORS:
            where = (("period", period), ("specification", "candidates"), ("factor_name", name))
            for field in ("mean_coefficient", "t_published_lag", "t_automatic_lag"):
                add(
                    f"supplement.fama_macbeth.{period}.candidates.{name}.{field}",
                    "fama_macbeth.csv",
                    field,
                    where,
                )
        for name in ("turnover_20", "reversal_5"):
            add(
                f"supplement.fama_macbeth.{period}.all_registered.{name}.t_published_lag",
                "fama_macbeth.csv",
                "t_published_lag",
                (("period", period), ("specification", "all_registered"), ("factor_name", name)),
            )
    for field in ("mean_coefficient", "t_published_lag"):
        add(
            f"supplement.fama_macbeth.research_2005_2016.all_registered.momentum_60.{field}",
            "fama_macbeth.csv",
            field,
            (
                ("period", "research_2005_2016"),
                ("specification", "all_registered"),
                ("factor_name", "momentum_60"),
            ),
        )

    interval_fields = (
        "annual_return_low",
        "annual_return_high",
        "annual_relative_return_low",
        "annual_relative_return_high",
        "relative_non_positive_share",
    )
    for period, portfolio in _BOOTSTRAP_PORTFOLIOS:
        for cost_mode in ("net", "zero_cost"):
            where = (
                ("period", period),
                ("portfolio", portfolio),
                ("cost_mode", cost_mode),
                ("benchmark", "equal_weight"),
            )
            for field in interval_fields:
                add(
                    f"supplement.bootstrap.{period}.{portfolio}.{cost_mode}.{field}",
                    "bootstrap_intervals.csv",
                    field,
                    where,
                )
    research_net = (
        ("period", "research_2005_2016"),
        ("portfolio", "research_path"),
        ("cost_mode", "net"),
        ("benchmark", "equal_weight"),
    )
    for field in ("sharpe", "sharpe_low", "sharpe_high"):
        add(
            f"supplement.bootstrap.research_2005_2016.research_path.net.{field}",
            "bootstrap_intervals.csv",
            field,
            research_net,
        )
    return specs


_TARGET_CHANGING_EXPERIMENTS = (
    "portfolio_buffer_high",
    "portfolio_buffer_low",
    "portfolio_rebalance_bimonthly",
    "portfolio_size_high",
    "portfolio_size_low",
    "portfolio_universe_low",
    "factor_ablation_liquidity",
    "factor_ablation_low_volatility",
    "factor_ablation_reversal",
    "rolling_window_high",
    "rolling_window_low",
)


def _robustness_subperiod_specs(report: tuple[str, ...]) -> list[KeyResultSpec]:
    """Stage 8 experiments re-measured on the 2017-2021 slice (report section 9.1)."""
    specs: list[KeyResultSpec] = []

    def slice_value(experiment: str, field: str) -> None:
        specs.append(
            KeyResultSpec(
                f"robustness_subperiod.{experiment}.{field}",
                "v1_robustness_subperiod",
                ResultSelect(
                    "validation_slice.csv",
                    kind="csv",
                    field=field,
                    where=(("experiment_id", experiment),),
                ),
                required_documents=report,
            )
        )

    for field in ("annual_return", "maximum_drawdown", "turnover"):
        slice_value("portfolio_baseline", field)
    for experiment in _TARGET_CHANGING_EXPERIMENTS:
        specs.append(
            KeyResultSpec(
                f"robustness_subperiod.{experiment}.full_period_annual_return_delta",
                "stage8_robustness",
                ResultSelect(
                    "datasets/robustness_results.parquet",
                    kind="parquet",
                    field="baseline_delta",
                    where=(("experiment_id", experiment), ("metric", "annual_return")),
                ),
                required_documents=report,
            )
        )
        for field in ("annual_return_delta", "maximum_drawdown_delta", "turnover_delta"):
            slice_value(experiment, field)
    slice_value("execution_impact_high", "annual_return_delta")
    return specs


def _index_benchmark_specs(report: tuple[str, ...]) -> list[KeyResultSpec]:
    """CSI 300 / CSI 500 price-index comparison (report section 7.2)."""
    specs: list[KeyResultSpec] = []

    def add(period: str, portfolio: str, cost_mode: str, benchmark: str, field: str) -> None:
        specs.append(
            KeyResultSpec(
                f"supplement.index.{period}.{portfolio}.{cost_mode}.{benchmark}.{field}",
                "v1_supplements",
                ResultSelect(
                    "index_benchmarks.csv",
                    kind="csv",
                    field=field,
                    where=(
                        ("period", period),
                        ("portfolio", portfolio),
                        ("cost_mode", cost_mode),
                        ("benchmark", benchmark),
                    ),
                ),
                required_documents=report,
            )
        )

    portfolios = (("research_2006_2016", "research_path"), *_BOOTSTRAP_PORTFOLIOS[1:])
    for period, portfolio in portfolios:
        for cost_mode in ("net", "zero_cost"):
            add(period, portfolio, cost_mode, "csi300", "strategy_annual_return")
            for benchmark in ("csi300", "csi500"):
                for field in ("annual_relative_return", "information_ratio", "beta"):
                    add(period, portfolio, cost_mode, benchmark, field)
    for period, portfolio in (portfolios[0], portfolios[-1]):
        for benchmark in ("csi300", "csi500", "equal_weight"):
            add(period, portfolio, "net", benchmark, "benchmark_annual_return")
    for benchmark in ("csi300", "csi500"):
        add("research_2006_2016", "research_path", "net", benchmark, "correlation")
    return specs
