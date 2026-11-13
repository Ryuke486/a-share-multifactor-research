"""Single entry point that chains the documented v1.0 rebuild commands.

The command adds no research logic: every step is one of the stage commands in
``docs/reproduction.md``, run in the documented order and stopped at the first
failure. It prints the plan by default; ``--execute`` is required to run it,
because the rebuild reads the full local data and publishes new releases.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

_CONFIG = "configs/research_protocol.yaml"


@dataclass(frozen=True)
class ReproductionStep:
    name: str
    scope: str
    module: str
    arguments: tuple[str, ...]

    def command(self, python: str) -> tuple[str, ...]:
        return (python, "-m", self.module, *self.arguments)


def reproduction_steps(run_suffix: str) -> tuple[ReproductionStep, ...]:
    """Documented v1.0 rebuild, research period first; never touches 2022 or later."""
    cli = "ashare_multifactor.cli"
    return (
        ReproductionStep(
            "factor-panel", "2003-2016", f"{cli}.factors", ("--config", _CONFIG, "--stage", "build")
        ),
        ReproductionStep(
            "factor-research",
            "2005-2016",
            f"{cli}.factors",
            ("--config", _CONFIG, "--stage", "all"),
        ),
        ReproductionStep("combination", "2005-2016", f"{cli}.combinations", ("--config", _CONFIG)),
        ReproductionStep(
            "backtest-audit", "2005-2016", f"{cli}.formal_backtest", ("audit", "--root", ".")
        ),
        ReproductionStep(
            "backtest",
            "2005-2016",
            f"{cli}.formal_backtest",
            ("all", "--root", ".", "--publish"),
        ),
        ReproductionStep(
            "validation",
            "2017-2021",
            f"{cli}.validation",
            ("reproduce", "--root", ".", "--run-id", f"v1-validation-{run_suffix}"),
        ),
        ReproductionStep(
            "robustness",
            "2005-2021",
            f"{cli}.robustness",
            ("reproduce", "--root", ".", "--run-id", f"v1-robustness-{run_suffix}"),
        ),
        ReproductionStep("supplements", "2005-2021", f"{cli}.supplements", ()),
        ReproductionStep(
            "delivery-check", "no data", f"{cli}.delivery", ("check", "--verify-sources")
        ),
    )


def run_reproduction(
    steps: Sequence[ReproductionStep],
    *,
    root: Path,
    python: str,
    execute: bool,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    echo: Callable[[str], None] = print,
) -> int:
    """Run steps in order and return the first non-zero exit code (0 when all pass)."""
    for index, step in enumerate(steps, start=1):
        command = step.command(python)
        echo(f"[{index}/{len(steps)}] {step.name} ({step.scope}): {' '.join(command)}")
        if not execute:
            continue
        completed = runner(command, cwd=root, check=False)
        if completed.returncode:
            echo(f"stopped: step {step.name} exited with {completed.returncode}")
            return int(completed.returncode)
    if not execute:
        echo("plan only; pass --execute to run it")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rebuild the v1.0 research and validation results in the documented order"
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--run-suffix", default="repro")
    parser.add_argument("--from-step", default=None, help="resume from this step name")
    parser.add_argument("--execute", action="store_true")
    arguments = parser.parse_args()
    steps = reproduction_steps(arguments.run_suffix)
    if arguments.from_step is not None:
        names = [step.name for step in steps]
        if arguments.from_step not in names:
            parser.error(f"unknown step {arguments.from_step!r}; choose from {', '.join(names)}")
        steps = steps[names.index(arguments.from_step) :]
    sys.exit(
        run_reproduction(
            steps, root=arguments.root, python=sys.executable, execute=arguments.execute
        )
    )


if __name__ == "__main__":
    main()
