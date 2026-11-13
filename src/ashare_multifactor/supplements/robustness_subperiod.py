"""Stage 8 experiments re-measured on the 2017-2021 validation slice only.

Portfolio, factor-ablation and rolling-window experiments only rebuild targets
from 2017 on, yet Stage 8 summarises them over 2005-2021, where twelve identical
years dilute any difference. This step reruns the pre-registered experiments
through the unchanged Stage 8 code, requires every full-period metric to
reproduce the published release, and then reports the same metrics on the
validation slice with the frozen tolerances. It is descriptive: nothing is
re-selected, no release is written, and no date after 2021 is read.
"""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from ashare_multifactor.audit.publication import resolve_current
from ashare_multifactor.audit.records import sha256_file
from ashare_multifactor.protocol_dates import RESEARCH_END, VALIDATION_END, VALIDATION_START
from ashare_multifactor.robustness import pipeline as stage8
from ashare_multifactor.robustness.protocol import load_robustness_protocol

OUTPUT_ROOT = Path("artifacts/v1_robustness_subperiod")
JUDGED_METRICS = ("annual_return", "maximum_drawdown", "turnover")
_REPRODUCED_METRICS = ("annual_return", "maximum_drawdown", "turnover", "cost_erosion")
_TOLERANCE = 1e-12


def validation_slice_metrics(
    result: dict[str, pl.DataFrame], *, boundary: date, start: date, end: date
) -> dict[str, Any]:
    """Return, drawdown and turnover from the boundary NAV to ``end``.

    Years run from ``start`` to ``end`` on the calendar, the Stage 7 convention, so
    the baseline reproduces the published validation return.
    """
    nav = result["nav"].sort("date")
    if nav.get_column("date").max() > end:
        raise ValueError("NAV path crosses the sealed final-test boundary")
    path = nav.filter(pl.col("date") >= boundary)
    if path.height < 3 or path.item(0, "date") != boundary:
        raise ValueError("NAV path has no observation on the research/validation boundary")
    levels = path.get_column("nav").to_numpy()
    zero = path.get_column("zero_cost_nav" if "zero_cost_nav" in path.columns else "nav")
    years = (end - start).days / 365.25
    returns = levels[1:] / levels[:-1] - 1.0
    trades = result["trades"].filter(pl.col("date") > boundary)
    traded = float(trades.get_column("amount").sum()) if trades.height else 0.0
    annual = float(levels[-1] / levels[0]) ** (1.0 / years) - 1.0
    zero_annual = float(zero.item(-1) / zero.item(0)) ** (1.0 / years) - 1.0
    return {
        "start_date": boundary,
        "end_date": path.item(-1, "date"),
        "years": years,
        "annual_return": annual,
        "zero_cost_annual_return": zero_annual,
        "cost_erosion": zero_annual - annual,
        "annual_volatility": float(np.std(returns, ddof=1) * np.sqrt(252.0)),
        "maximum_drawdown": float((levels / np.maximum.accumulate(levels) - 1.0).min()),
        "turnover": traded / (2.0 * float(levels.mean()) * years),
    }


def judge_against_baseline(
    metrics: dict[str, float], baseline: dict[str, float], tolerances: dict[str, float]
) -> dict[str, str]:
    """Stage 8's rule: a deeper drawdown or a move beyond tolerance is ``sensitive``."""
    verdicts = {}
    for name in JUDGED_METRICS:
        delta = metrics[name] - baseline[name]
        if name == "maximum_drawdown":
            stable = delta >= -tolerances[name]
        else:
            stable = abs(delta) <= tolerances[name]
        verdicts[name] = "stable" if stable else "sensitive"
    return verdicts


def run_robustness_subperiod(root: Path = Path(".")) -> Path:
    """Rerun Stage 8's experiments (about one minute each) and write the slice table."""
    root = root.resolve()
    protocol = load_robustness_protocol(root / "configs/robustness_protocol.yaml")
    validation = resolve_current(root / "processed/validation_evaluation")
    published_release = resolve_current(root / "processed/robustness")
    published = pl.read_parquet(published_release.datasets / "robustness_results.parquet")

    captured: dict[str, dict[str, pl.DataFrame]] = {}
    results, fatal = stage8._execute_experiments(
        root,
        root,
        validation,
        protocol,
        on_result=lambda experiment_id, result: captured.__setitem__(experiment_id, result),
    )
    if fatal:
        raise ValueError(f"Stage 8 rerun reported fatal events: {fatal}")
    _assert_reproduces(results, published)

    definitions = {item.experiment_id: item for item in protocol.experiments}
    tolerances = {
        "annual_return": float(protocol.interpretation["return_tolerance"]),
        "maximum_drawdown": float(protocol.interpretation["drawdown_tolerance"]),
        "turnover": float(protocol.interpretation["turnover_tolerance"]),
    }
    sliced = {
        experiment_id: validation_slice_metrics(
            result, boundary=_boundary(result), start=VALIDATION_START, end=VALIDATION_END
        )
        for experiment_id, result in captured.items()
    }
    _assert_baseline_matches_stage7(
        sliced[stage8_baseline_id(published, "execution_zero_cost")], validation, protocol
    )
    rows: list[dict[str, Any]] = []
    for experiment_id in sorted(sliced):
        category = definitions[experiment_id].category
        baseline_id = stage8_baseline_id(published, experiment_id)
        metrics, baseline = sliced[experiment_id], sliced[baseline_id]
        verdicts = judge_against_baseline(metrics, baseline, tolerances)
        full = _published_verdicts(published, experiment_id)
        rows.append(
            {
                "experiment_id": experiment_id,
                "category": category,
                "baseline_experiment_id": baseline_id,
                **metrics,
                **{f"{name}_delta": metrics[name] - baseline[name] for name in JUDGED_METRICS},
                **{f"{name}_slice_verdict": verdicts[name] for name in JUDGED_METRICS},
                **{f"{name}_full_period_verdict": full[name] for name in JUDGED_METRICS},
                "slice_verdict": _overall(verdicts.values()),
                "full_period_verdict": _overall(full.values()),
            }
        )
    table = pl.DataFrame(rows).sort("category", "experiment_id")

    output = root / OUTPUT_ROOT
    output.mkdir(parents=True, exist_ok=True)
    table.write_csv(output / "validation_slice.csv")
    _write_manifest(output, root, validation, published_release)
    return output


def stage8_baseline_id(published: pl.DataFrame, experiment_id: str) -> str:
    return str(
        published.filter(pl.col("experiment_id") == experiment_id).item(0, "baseline_experiment_id")
    )


def _assert_baseline_matches_stage7(
    baseline: dict[str, Any], validation: Any, protocol: Any
) -> None:
    published = pl.read_parquet(validation.artifacts / "research/validation_metrics.parquet")
    expected = published.filter(pl.col("candidate") == protocol.main_candidate).item(
        0, "net_annual_return"
    )
    if abs(baseline["annual_return"] - float(expected)) > _TOLERANCE:
        raise ValueError("baseline slice does not reproduce the published validation return")


def _boundary(result: dict[str, pl.DataFrame]) -> date:
    return result["nav"].filter(pl.col("date") <= RESEARCH_END).get_column("date").max()


def _overall(verdicts: Any) -> str:
    return "sensitive" if "sensitive" in set(verdicts) else "stable"


def _published_verdicts(published: pl.DataFrame, experiment_id: str) -> dict[str, str]:
    rows = published.filter(
        (pl.col("experiment_id") == experiment_id) & pl.col("metric").is_in(JUDGED_METRICS)
    )
    return dict(rows.select("metric", "interpretation").iter_rows())


def _assert_reproduces(results: list[dict[str, Any]], published: pl.DataFrame) -> None:
    """Every rerun full-period metric must equal the published Stage 8 value."""
    reference = {
        (row["experiment_id"], row["metric"]): row["value"]
        for row in published.filter(pl.col("sample") == "2005-2021").iter_rows(named=True)
    }
    for result in results:
        if result.get("sample") != "2005-2021" or result.get("status") != "ready":
            continue
        for name in _REPRODUCED_METRICS:
            expected = reference[(result["experiment_id"], name)]
            if abs(result["metrics"][name] - expected) > _TOLERANCE:
                raise ValueError(
                    f"{result['experiment_id']} {name} does not reproduce the published Stage 8 value"
                )


def _write_manifest(output: Path, root: Path, validation: Any, published_release: Any) -> None:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain", "--", "src", "configs"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    )
    manifest = {
        "purpose": "descriptive validation-slice view of Stage 8; never used for selection",
        "slice": [RESEARCH_END.isoformat(), VALIDATION_END.isoformat()],
        "code_commit": commit,
        "code_dirty": dirty,
        "inputs": {
            f"stage7:{validation.run_id}/manifest.json": validation.manifest_sha256,
            f"stage8:{published_release.run_id}/manifest.json": published_release.manifest_sha256,
            "configs/robustness_protocol.yaml": sha256_file(
                root / "configs/robustness_protocol.yaml"
            ),
            "configs/market_rules.yaml": sha256_file(root / "configs/market_rules.yaml"),
        },
        "outputs": {"validation_slice.csv": sha256_file(output / "validation_slice.csv")},
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
