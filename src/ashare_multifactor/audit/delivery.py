"""Delivery consistency checks for the v1.0 research deliverable.

Three families of checks are implemented here:

1. local Markdown links resolve to files that are part of the deliverable;
2. numbers quoted in the delivered documents are consistent with a tracked,
   machine-generated record of the frozen research releases;
3. hashes in the delivered manifest describe the frozen release tree.

The checks never read Data/, processed/ or artifacts/ unless the caller
explicitly asks for source verification, so they can run in CI.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote

import polars as pl

KEY_RESULTS_SCHEMA_VERSION = 1
DEFAULT_DELIVERY_MANIFEST = Path("releases/v1.0.0-research-validation.json")
DEFAULT_KEY_RESULTS = Path("docs/results/v1.0-key-results.json")

_LINK_PATTERN = re.compile(
    r"!?\[[^\]]*\]\(\s*(?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\s*\)"
)
# ASCII-only boundaries: CJK characters around a number must not block a match.
_NUMBER_PATTERN = re.compile(
    r"(?<![0-9A-Za-z_.])(?P<number>-?\d[\d,]*(?:\.\d+)?)(?![0-9A-Za-z_])"
)
_EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "#")


@dataclass(frozen=True)
class DeliveryIssue:
    """One concrete inconsistency found by a delivery check."""

    check: str
    path: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"check": self.check, "path": self.path, "detail": self.detail}


@dataclass(frozen=True)
class CheckOutcome:
    """Result of a single delivery check."""

    name: str
    issues: tuple[DeliveryIssue, ...] = ()
    notes: tuple[str, ...] = ()
    skipped: str | None = None

    @property
    def ok(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "ok": self.ok,
            "issues": [issue.as_dict() for issue in self.issues],
            "notes": list(self.notes),
        }
        if self.skipped is not None:
            payload["skipped"] = self.skipped
        return payload


@dataclass(frozen=True)
class DeliveryReport:
    """Aggregate result of every delivery check."""

    outcomes: tuple[CheckOutcome, ...]

    @property
    def ok(self) -> bool:
        return all(outcome.ok for outcome in self.outcomes)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "outcomes": [o.as_dict() for o in self.outcomes]}

    def render(self) -> str:
        lines: list[str] = []
        for outcome in self.outcomes:
            state = "SKIP" if outcome.skipped else ("PASS" if outcome.ok else "FAIL")
            lines.append(f"[{state}] {outcome.name}")
            if outcome.skipped:
                lines.append(f"    skipped: {outcome.skipped}")
            for note in outcome.notes:
                lines.append(f"    note: {note}")
            for issue in outcome.issues:
                lines.append(f"    {issue.path}: {issue.detail}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Git and file helpers
# ---------------------------------------------------------------------------


def _git_bytes(root: Path, *args: str) -> bytes | None:
    """Run git read-only helper command; return None when git is unavailable."""
    try:
        result = subprocess.run(
            ("git", *args),
            cwd=root,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout


def tracked_files(root: Path) -> set[str] | None:
    """Tracked paths relative to root, or None outside a git work tree."""
    output = _git_bytes(root, "ls-files", "-z")
    if output is None:
        return None
    return {name for name in output.decode("utf-8", "replace").split("\0") if name}


def _is_ignored(root: Path, relative: str) -> bool:
    try:
        result = subprocess.run(
            ("git", "check-ignore", "-q", "--", relative),
            cwd=root,
            capture_output=True,
        )
    except OSError:
        return False
    return result.returncode == 0


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def markdown_documents(root: Path, names: Iterable[str] | None = None) -> tuple[str, ...]:
    """Deliverable Markdown documents, restricted to tracked files when possible."""
    if names is not None:
        return tuple(sorted(names))
    tracked = tracked_files(root)
    if tracked is None:
        candidates = {
            path.relative_to(root).as_posix() for path in root.rglob("*.md")
        }
        return tuple(sorted(candidates))
    return tuple(sorted(name for name in tracked if name.endswith(".md")))


# ---------------------------------------------------------------------------
# Check 1: local Markdown links
# ---------------------------------------------------------------------------


def check_markdown_links(
    root: Path,
    documents: Sequence[str] | None = None,
) -> CheckOutcome:
    """Every relative Markdown link must resolve inside the deliverable."""
    root = root.resolve()
    issues: list[DeliveryIssue] = []
    checked = 0
    for name in markdown_documents(root, documents):
        path = root / name
        if not path.is_file():
            issues.append(DeliveryIssue("markdown_links", name, "document is missing"))
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _LINK_PATTERN.finditer(text):
            target = match.group("target")
            if target.startswith(_EXTERNAL_PREFIXES):
                continue
            checked += 1
            note = _link_issue(root, name, target)
            if note is not None:
                issues.append(DeliveryIssue("markdown_links", name, note))
    return CheckOutcome(
        name="markdown_links",
        issues=tuple(issues),
        notes=(f"{checked} local links checked in {len(markdown_documents(root, documents))} documents",),
    )


def _link_issue(root: Path, document: str, target: str) -> str | None:
    base = target.split("#", 1)[0].split("?", 1)[0]
    if not base:
        return None
    if base.startswith("/"):
        resolved = root / unquote(base).lstrip("/")
        relative = unquote(base).lstrip("/")
    else:
        resolved = (root / document).parent / unquote(base)
        try:
            relative = resolved.resolve().relative_to(root).as_posix()
        except ValueError:
            return f"link escapes the repository: {target}"
    if not resolved.exists():
        return f"link target does not exist: {target}"
    tracked = tracked_files(root)
    if tracked is not None and relative not in tracked and _is_ignored(root, relative):
        return f"link target is not deliverable (git-ignored): {target}"
    return None


# ---------------------------------------------------------------------------
# Check 2: delivered manifest hashes
# ---------------------------------------------------------------------------


def _git_text(root: Path, *args: str) -> str | None:
    payload = _git_bytes(root, *args)
    if payload is None:
        return None
    return payload.decode("utf-8", "replace").strip()


def _publication_commit(root: Path, manifest_name: str) -> str | None:
    """Commit that last published the manifest, whose tree the hashes describe."""
    return _git_text(root, "log", "-1", "--format=%H", "--", manifest_name) or None


def _source_release_issues(root: Path, manifest: Mapping[str, Any]) -> list[DeliveryIssue]:
    """Local layer: recorded source-release hashes must match the local pointers."""
    issues: list[DeliveryIssue] = []
    for entry in manifest.get("source_releases", []):
        hint = str(entry.get("path_hint", ""))
        if not hint:
            continue
        if entry.get("run_id"):
            pointer = root / Path(hint).parents[2] / "CURRENT.json"
            if not pointer.is_file():
                continue
            current = json.loads(pointer.read_text(encoding="utf-8"))
            if current.get("manifest_sha256") != entry.get("manifest_sha256"):
                issues.append(
                    DeliveryIssue(
                        "delivery_manifest",
                        str(pointer.relative_to(root)),
                        f"stage {entry.get('stage')} pointer no longer matches the delivered hash",
                    )
                )
            elif current.get("run_id") != entry.get("run_id"):
                issues.append(
                    DeliveryIssue(
                        "delivery_manifest",
                        str(pointer.relative_to(root)),
                        f"stage {entry.get('stage')} run id changed since the delivery",
                    )
                )
            continue
        artifact = root / hint
        if artifact.is_file() and sha256_file(artifact) != entry.get("manifest_sha256"):
            issues.append(
                DeliveryIssue(
                    "delivery_manifest",
                    hint,
                    f"stage {entry.get('stage')} artifact no longer matches the delivered hash",
                )
            )
    return issues


def check_delivery_manifest(
    root: Path,
    manifest_path: Path | str = DEFAULT_DELIVERY_MANIFEST,
    *,
    verify_sources: bool = False,
) -> CheckOutcome:
    """Verify a delivered manifest against the tree it was published with.

    The recorded hashes describe the publication commit, not the current working
    tree: later authorized work is reported as a note instead of an error, and a
    new delivery version needs its own authorization.
    """
    root = root.resolve()
    manifest_name = Path(manifest_path).as_posix()
    path = Path(manifest_path)
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        return CheckOutcome(
            name="delivery_manifest",
            issues=(
                DeliveryIssue("delivery_manifest", manifest_name, "manifest is missing"),
            ),
        )
    manifest = json.loads(path.read_text(encoding="utf-8"))
    anchor = _publication_commit(root, manifest_name)
    if anchor is None:
        return CheckOutcome(
            name="delivery_manifest",
            skipped="git history for the delivered manifest is unavailable",
        )
    issues: list[DeliveryIssue] = []
    notes: list[str] = [f"delivery hashes are read from publication commit {anchor[:12]}"]
    baseline = manifest.get("code_baseline")
    if isinstance(baseline, dict) and baseline.get("commit"):
        commit = str(baseline["commit"])
        tree = _git_text(root, "rev-parse", f"{commit}^{{tree}}")
        recorded_tree = str(baseline.get("tree", ""))
        if tree is None:
            issues.append(
                DeliveryIssue(
                    "delivery_manifest",
                    manifest_name,
                    f"code baseline commit {commit[:12]} is absent from this clone",
                )
            )
        elif recorded_tree and tree != recorded_tree:
            issues.append(
                DeliveryIssue(
                    "delivery_manifest",
                    manifest_name,
                    "code baseline commit does not match the recorded tree",
                )
            )
        else:
            notes.append(f"code baseline commit {commit[:12]} matches the recorded tree")
    drifted: list[str] = []
    for entry in manifest.get("delivery_files", []):
        relative = str(entry["path"])
        payload = _git_bytes(root, "cat-file", "blob", f"{anchor}:{relative}")
        if payload is None:
            issues.append(
                DeliveryIssue(
                    "delivery_manifest",
                    relative,
                    f"absent from publication commit {anchor[:12]}",
                )
            )
            continue
        if sha256_bytes(payload) != entry["sha256"]:
            issues.append(
                DeliveryIssue(
                    "delivery_manifest",
                    relative,
                    f"bytes at publication commit {anchor[:12]} do not match the recorded hash",
                )
            )
        elif (root / relative).is_file() and sha256_file(root / relative) != entry["sha256"]:
            drifted.append(relative)
    if drifted:
        notes.append(
            "working tree differs from the published delivery for "
            + ", ".join(sorted(drifted))
            + " (expected after later authorized work)"
        )
    seal = manifest.get("final_test")
    if isinstance(seal, dict) and "current_pointer_exists" in seal:
        actual = (root / "processed/final_test/CURRENT.json").exists()
        if bool(seal["current_pointer_exists"]) != actual:
            issues.append(
                DeliveryIssue(
                    "delivery_manifest",
                    "processed/final_test/CURRENT.json",
                    "final-test seal state does not match the delivered manifest",
                )
            )
    if verify_sources:
        issues.extend(_source_release_issues(root, manifest))
    return CheckOutcome(name="delivery_manifest", issues=tuple(issues), notes=tuple(notes))


# ---------------------------------------------------------------------------
# Number rendering used by the documented-results check
# ---------------------------------------------------------------------------


def _precision(literal: str) -> int | None:
    body = literal.replace(",", "").lstrip("-+")
    if "." in body:
        whole, fraction = body.split(".", 1)
        if not whole.isdigit() or not fraction.isdigit():
            return None
        return len(fraction)
    if not body.isdigit():
        return None
    return 0


def number_renders_value(value: float, literal: str) -> bool:
    """True when the literal is an exact fixed-point rendering of the value.

    The literal decides its own precision, so the same machine value may be
    quoted as 17.6031%, 0.176031 or 0.17603097829253445. A changed digit no
    longer renders the value.
    """
    precision = _precision(literal)
    if precision is None or precision > 17:
        return False
    body = literal.replace(",", "").replace("+", "")
    for scale in (1.0, 100.0):
        for candidate in (f"{value * scale:.{precision}f}",):
            if candidate == body:
                return True
    return False


def _is_quotable(literal: str) -> bool:
    body = literal.replace(",", "").lstrip("-")
    whole, _, fraction = body.partition(".")
    digits = (whole + fraction).lstrip("0")
    if len(digits) >= 3:
        return True
    return len(fraction) >= 6


def _phrase_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) or (isinstance(value, float) and float(value).is_integer()):
        return str(int(value))
    return f"{value:.10f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# Check 3: documented numbers against the recorded machine results
# ---------------------------------------------------------------------------


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
                (("period", period), ("portfolio", portfolio), ("cost_mode", "net"),
                 ("benchmark", benchmark)),
                "benchmark_annual_return",
            )
    portfolios = [(label, period, portfolio) for label, period, portfolio in _SUPPLEMENT_PERIODS]
    portfolios += [
        ("validation", "validation_2017_2021", candidate)
        for candidate in ("family_equal_size_stratified_buffered", "family_equal_top100_equal")
    ]
    for label, period, portfolio in portfolios:
        for cost_mode in ("net", "zero_cost"):
            where = (("period", period), ("portfolio", portfolio), ("cost_mode", cost_mode),
                     ("benchmark", "equal_weight"))
            for field in ("annual_relative_return", "information_ratio", "beta"):
                relative(f"{label}.{portfolio}.{cost_mode}.equal_weight.{field}", where, field)
    for year in range(2017, 2022):
        for field in ("strategy_net", "equal_weight", "cap_weight"):
            specs.append(
                KeyResultSpec(
                    f"supplement.calendar.{year}.{field}",
                    "v1_supplements",
                    ResultSelect(
                        "calendar_year_returns.csv", kind="csv", field=field,
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
                            "leg_decomposition.csv", kind="csv", field=field,
                            where=(("period", period), ("object_name", name)),
                        ),
                        required_documents=report,
                    )
                )
    specs.extend(_factor_statistic_specs(report))
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
        names = ("momentum_60", "momentum_120", "momentum_12_1") if period == research else (
            "momentum_60",
            "log_market_cap",
        )
        for name in names:
            add(
                f"supplement.newey_west.{period}.{name}.mean_ic",
                "newey_west_lags.csv",
                "mean_ic",
                (("period", period), ("object_name", name)),
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


def _source_base(root: Path, record: Mapping[str, Any]) -> Path:
    if record["kind"] == "release":
        return root / str(record["root"]) / "releases" / str(record["run_id"])
    return root / str(record["root"])


def _source_record(root: Path, spec: SourceSpec) -> dict[str, Any]:
    if spec.kind == "release":
        current = json.loads((root / spec.root / "CURRENT.json").read_text(encoding="utf-8"))
        return {
            "name": spec.name,
            "kind": spec.kind,
            "root": spec.root,
            "run_id": current["run_id"],
            "manifest_sha256": current["manifest_sha256"],
        }
    return {"name": spec.name, "kind": spec.kind, "root": spec.root}


def _json_value(payload: Any, path: str | None) -> Any:
    if path is None:
        return payload
    current = payload
    for part in path.split("."):
        if isinstance(current, list):
            current = current[int(part)]
        else:
            current = current[part]
    return current


def _resolve_value(base: Path, select: ResultSelect) -> Any:
    path = base / select.artifact
    if not path.is_file():
        raise FileNotFoundError(f"missing artifact {path}")
    if select.kind == "json":
        return _json_value(json.loads(path.read_text(encoding="utf-8")), select.field)
    frame = pl.read_parquet(path) if select.kind == "parquet" else pl.read_csv(path)
    for column, expected in select.where:
        frame = frame.filter(pl.col(column).cast(pl.Utf8) == str(expected))
    if select.stat == "count":
        return frame.height
    if select.stat == "n_unique":
        return frame.get_column(str(select.field)).n_unique()
    if select.stat == "mean":
        return frame.get_column(str(select.field)).mean()
    if select.stat == "sum_ratio":
        if select.denominator is None:
            raise ValueError("a sum_ratio selection requires a denominator column")
        return frame.get_column(str(select.field)).sum() / frame.get_column(
            select.denominator
        ).sum()
    if select.field is None:
        raise ValueError("a field is required for a single-row selection")
    if frame.height != 1:
        raise ValueError(f"expected exactly one row for {select.artifact}, found {frame.height}")
    return frame.get_column(select.field)[0]


def _discover_citations(
    texts: Mapping[str, str],
    value: float,
    documents: Sequence[str],
) -> list[dict[str, str]]:
    citations: list[dict[str, str]] = []
    for document in documents:
        text = texts.get(document)
        if text is None:
            continue
        seen: set[str] = set()
        for match in _NUMBER_PATTERN.finditer(text):
            literal = match.group("number")
            if literal in seen:
                continue
            if not _is_quotable(literal) and not (value == 0 and float(literal.replace(",", "")) == 0.0):
                continue
            if number_renders_value(value, literal):
                seen.add(literal)
                citations.append({"document": document, "literal": literal, "kind": "number"})
    return citations


def build_key_results(
    root: Path,
    *,
    specs: Sequence[KeyResultSpec] | None = None,
    sources: Sequence[SourceSpec] = V1_SOURCES,
    documents: Sequence[str] = V1_DOCUMENTS,
    release_id: str = "v1.0.0-research-validation",
    generated_on: str | None = None,
) -> dict[str, Any]:
    """Build the tracked record of documented numbers from local releases."""
    root = root.resolve()
    specs = tuple(specs) if specs is not None else v1_key_result_specs()
    source_records = [_source_record(root, spec) for spec in sources]
    by_name = {record["name"]: record for record in source_records}
    texts = {
        name: (root / name).read_text(encoding="utf-8")
        for name in documents
        if (root / name).is_file()
    }
    results: list[dict[str, Any]] = []
    for spec in specs:
        if spec.source not in by_name:
            raise ValueError(f"{spec.key}: unknown source {spec.source}")
        record = by_name[spec.source]
        base = _source_base(root, record)
        value = _resolve_value(base, spec.select)
        if isinstance(value, float) and not value == value:
            raise ValueError(f"{spec.key}: source value is not a number")
        citations: list[dict[str, str]] = []
        phrases: list[dict[str, str]] = []
        if isinstance(value, str):
            for document in spec.required_documents:
                if value not in texts.get(document, ""):
                    raise ValueError(f"{spec.key}: {value!r} is missing from {document}")
                citations.append({"document": document, "literal": value, "kind": "text"})
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            citations = _discover_citations(texts, float(value), spec.required_documents)
        for template in spec.templates:
            text = template.format(value=_phrase_value(value))
            for document in spec.required_documents:
                if text not in texts.get(document, ""):
                    raise ValueError(f"{spec.key}: {text!r} is missing from {document}")
                phrases.append({"document": document, "text": text})
        for document in spec.required_documents:
            covered = any(item["document"] == document for item in citations + phrases)
            if not covered:
                raise ValueError(f"{spec.key}: no citation recorded for {document}")
        artifact = base / spec.select.artifact
        results.append(
            {
                "key": spec.key,
                "value": value,
                "source": spec.source,
                "artifact": spec.select.artifact,
                "artifact_sha256": sha256_file(artifact),
                "required_documents": list(spec.required_documents),
                "citations": citations,
                "phrases": phrases,
            }
        )
    return {
        "schema_version": KEY_RESULTS_SCHEMA_VERSION,
        "release_id": release_id,
        "generated_on": generated_on,
        "documents": list(documents),
        "sources": source_records,
        "results": results,
    }


def write_key_results(
    root: Path,
    path: Path | str = DEFAULT_KEY_RESULTS,
    **kwargs: Any,
) -> Path:
    """Record the documented numbers under the repository root."""
    root = root.resolve()
    record = build_key_results(root, **kwargs)
    target = Path(path)
    if not target.is_absolute():
        target = root / target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return target


def check_documented_results(
    root: Path,
    results_path: Path | str = DEFAULT_KEY_RESULTS,
) -> CheckOutcome:
    """Documented numbers must still match the tracked machine-results record."""
    root = root.resolve()
    path = Path(results_path)
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        return CheckOutcome(
            name="documented_results",
            issues=(DeliveryIssue("documented_results", str(results_path), "record is missing"),),
        )
    record = json.loads(path.read_text(encoding="utf-8"))
    issues: list[DeliveryIssue] = []
    if record.get("schema_version") != KEY_RESULTS_SCHEMA_VERSION:
        issues.append(
            DeliveryIssue(
                "documented_results",
                str(results_path),
                "unsupported record schema version",
            )
        )
    texts = {
        name: (root / name).read_text(encoding="utf-8")
        for name in record.get("documents", [])
        if (root / name).is_file()
    }
    citations = 0
    for result in record.get("results", []):
        key = str(result["key"])
        value = result["value"]
        covered: set[str] = set()
        for citation in result.get("citations", []):
            document = str(citation["document"])
            literal = str(citation["literal"])
            citations += 1
            if document not in texts:
                issues.append(
                    DeliveryIssue("documented_results", document, f"{key}: document is missing")
                )
                continue
            if literal not in texts[document]:
                issues.append(
                    DeliveryIssue(
                        "documented_results",
                        document,
                        f"{key}: recorded value {literal!r} is no longer stated",
                    )
                )
                continue
            if citation.get("kind") == "number" and not number_renders_value(value, literal):
                issues.append(
                    DeliveryIssue(
                        "documented_results",
                        document,
                        f"{key}: {literal!r} no longer renders {value!r}",
                    )
                )
                continue
            covered.add(document)
        for phrase in result.get("phrases", []):
            document = str(phrase["document"])
            text = str(phrase["text"])
            citations += 1
            if text not in texts.get(document, ""):
                issues.append(
                    DeliveryIssue(
                        "documented_results",
                        document,
                        f"{key}: recorded statement {text!r} is no longer present",
                    )
                )
                continue
            covered.add(document)
        for document in result.get("required_documents", []):
            if document not in covered:
                issues.append(
                    DeliveryIssue(
                        "documented_results",
                        str(document),
                        f"{key}: no valid citation remains in this document",
                    )
                )
    return CheckOutcome(
        name="documented_results",
        issues=tuple(issues),
        notes=(f"{len(record.get('results', []))} results, {citations} citations",),
    )


def _values_match(recorded: Any, observed: Any) -> bool:
    if isinstance(recorded, bool) or isinstance(observed, bool):
        return recorded == observed
    if isinstance(recorded, (int, float)) and isinstance(observed, (int, float)):
        return abs(float(recorded) - float(observed)) <= 1e-12 * max(1.0, abs(float(observed)))
    return recorded == observed


def verify_key_result_sources(
    root: Path,
    results_path: Path | str = DEFAULT_KEY_RESULTS,
) -> CheckOutcome:
    """Local layer: recorded numbers must still match the local releases."""
    root = root.resolve()
    path = Path(results_path)
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        return CheckOutcome(
            name="result_sources",
            issues=(DeliveryIssue("result_sources", str(results_path), "record is missing"),),
        )
    record = json.loads(path.read_text(encoding="utf-8"))
    missing = [
        str(source["name"])
        for source in record.get("sources", [])
        if not (root / str(source["root"])).is_dir()
        or (
            source["kind"] == "release"
            and not (root / str(source["root"]) / "CURRENT.json").is_file()
        )
    ]
    if missing:
        return CheckOutcome(
            name="result_sources",
            skipped="local sources absent: " + ", ".join(sorted(missing)),
        )
    issues: list[DeliveryIssue] = []
    by_name = {str(source["name"]): source for source in record.get("sources", [])}
    for name, source in sorted(by_name.items()):
        if source["kind"] != "release":
            continue
        current = json.loads(
            (root / str(source["root"]) / "CURRENT.json").read_text(encoding="utf-8")
        )
        if current.get("run_id") != source.get("run_id") or current.get(
            "manifest_sha256"
        ) != source.get("manifest_sha256"):
            issues.append(
                DeliveryIssue(
                    "result_sources",
                    str(source["root"]),
                    "current release differs from the recorded release identity",
                )
            )
    for result in record.get("results", []):
        key = str(result["key"])
        source = by_name.get(str(result["source"]))
        if source is None:
            issues.append(DeliveryIssue("result_sources", key, "unknown source"))
            continue
        base = _source_base(root, source)
        artifact = base / str(result["artifact"])
        if not artifact.is_file():
            issues.append(
                DeliveryIssue("result_sources", key, f"artifact is missing: {result['artifact']}")
            )
            continue
        if sha256_file(artifact) != result.get("artifact_sha256"):
            issues.append(
                DeliveryIssue(
                    "result_sources",
                    key,
                    "artifact bytes changed since the record was generated",
                )
            )
            continue
        spec = _spec_for(key)
        if spec is None:
            continue
        observed = _resolve_value(base, spec.select)
        if not _values_match(result["value"], observed):
            issues.append(
                DeliveryIssue(
                    "result_sources",
                    key,
                    f"recorded {result['value']!r} but the release reports {observed!r}",
                )
            )
    return CheckOutcome(
        name="result_sources",
        issues=tuple(issues),
        notes=(f"{len(record.get('results', []))} results re-read from local releases",),
    )


def _spec_for(key: str) -> KeyResultSpec | None:
    for spec in v1_key_result_specs():
        if spec.key == key:
            return spec
    return None


def run_delivery_checks(
    root: Path,
    *,
    manifest_path: Path | str = DEFAULT_DELIVERY_MANIFEST,
    results_path: Path | str = DEFAULT_KEY_RESULTS,
    documents: Sequence[str] | None = None,
    verify_sources: bool = False,
) -> DeliveryReport:
    """Run every delivery check that does not need local research releases."""
    outcomes = [
        check_markdown_links(root, documents),
        check_documented_results(root, results_path),
        check_delivery_manifest(root, manifest_path, verify_sources=verify_sources),
    ]
    if verify_sources:
        outcomes.append(verify_key_result_sources(root, results_path))
    return DeliveryReport(outcomes=tuple(outcomes))




