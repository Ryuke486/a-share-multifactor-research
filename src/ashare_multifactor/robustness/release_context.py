"""Shared filesystem and market-scope checks for Stage-8 publication."""

from __future__ import annotations

from pathlib import Path

import polars as pl

from ashare_multifactor.data.security import assert_supported_markets


def resolve_robustness_data_root(code_root: Path) -> Path:
    """Resolve generated data without assuming the code worktree is authoritative."""
    root = code_root.resolve()
    marker = Path("processed/validation_evaluation/CURRENT.json")
    if (root / marker).is_file():
        return root
    if root.parent.name == ".worktrees" and (root.parent.parent / marker).is_file():
        return root.parent.parent
    raise FileNotFoundError("cannot locate authoritative validation release")


def assert_validation_market_scope(
    validation: object,
    supported_markets: tuple[str, ...],
) -> None:
    """Recheck every Stage-7 handoff before a Stage-8 release is published."""
    inputs = validation.datasets / "inputs"
    frames = (
        ("forward_returns.parquet", ("symbol",)),
        ("execution_panel.parquet", ("symbol",)),
        ("corporate_actions.parquet", ("symbol",)),
        ("security_events.parquet", ("source_symbol", "target_symbol")),
    )
    for name, columns in frames:
        assert_supported_markets(
            pl.read_parquet(inputs / name),
            supported_markets,
            label=f"Stage-8 publish validation handoff {name}",
            symbol_columns=columns,
        )
    target_paths = sorted(inputs.glob("continuous_targets_*.parquet"))
    if not target_paths:
        raise ValueError("Stage-8 publish validation handoff lacks continuous targets")
    for path in target_paths:
        assert_supported_markets(
            pl.read_parquet(path),
            supported_markets,
            label=f"Stage-8 publish validation handoff {path.name}",
        )
