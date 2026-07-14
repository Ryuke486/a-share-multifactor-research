from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FormalBacktestPaths:
    processed: Path
    artifacts: Path


def formal_backtest_paths(root: Path) -> FormalBacktestPaths:
    return FormalBacktestPaths(
        root / "processed" / "formal_backtest",
        root / "artifacts" / "formal_backtest",
    )

