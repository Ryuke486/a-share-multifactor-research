from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

from ashare_multifactor.cli.reproduce import reproduction_steps, run_reproduction


def test_steps_follow_the_one_way_data_flow_and_never_name_the_final_test() -> None:
    steps = reproduction_steps("unit")

    assert [step.name for step in steps] == [
        "factor-panel",
        "factor-research",
        "combination",
        "backtest-audit",
        "backtest",
        "validation",
        "robustness",
        "supplements",
        "delivery-check",
    ]
    for step in steps:
        assert hasattr(importlib.import_module(step.module), "main")
        assert "final" not in step.module
        assert not any(year in " ".join(step.arguments) for year in ("2022", "2025"))
    assert "v1-validation-unit" in steps[5].arguments
    assert "v1-robustness-unit" in steps[6].arguments


def test_plan_only_mode_runs_nothing(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []
    lines: list[str] = []

    code = run_reproduction(
        reproduction_steps("unit"),
        root=tmp_path,
        python="python",
        execute=False,
        runner=lambda command, **_: calls.append(command),
        echo=lines.append,
    )

    assert code == 0
    assert calls == []
    assert len(lines) == 10
    assert lines[-1] == "plan only; pass --execute to run it"


def test_execution_stops_at_the_first_failing_step(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess:
        assert kwargs == {"cwd": tmp_path, "check": False}
        calls.append(command)
        return subprocess.CompletedProcess(command, 3 if len(calls) == 3 else 0)

    lines: list[str] = []
    code = run_reproduction(
        reproduction_steps("unit"),
        root=tmp_path,
        python="python",
        execute=True,
        runner=runner,
        echo=lines.append,
    )

    assert code == 3
    assert len(calls) == 3
    assert calls[2][:3] == ("python", "-m", "ashare_multifactor.cli.combinations")
    assert lines[-1] == "stopped: step combination exited with 3"
