"""Boundary contract after archiving the deferred final test off main.

The Stage 9-10 final-test code is optional v2.0 work. It was removed from main in
one revertable commit and is preserved under the Git tag
``archive/stage9-final-test`` (commit ``ARCHIVE_COMMIT``). These tests prove that
nothing on main still depends on it, that every public entry point resolves, and
that the frozen identity contracts still name files that exist either on main or
in the archived commit, so a future v2.0 can restore them byte-for-byte.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import ashare_multifactor


CODE_ROOT = Path(ashare_multifactor.__file__).resolve().parents[2]
SOURCE_ROOT = CODE_ROOT / "src/ashare_multifactor"
FINAL_TEST_PREFIX = "ashare_multifactor.final_test"
ARCHIVE_COMMIT = "d7c28288265d4f3f0e20a5371ce984157c14760f"
ARCHIVED_PATHS = (
    "src/ashare_multifactor/final_test",
    "src/ashare_multifactor/cli/final_test.py",
    "src/ashare_multifactor/cli/final_test_review.py",
    "src/ashare_multifactor/robustness/change_impact.py",
    "src/ashare_multifactor/robustness/evidence_workflow_readiness.py",
    "src/ashare_multifactor/robustness/evidence_workflow_rehearsal.py",
    "src/ashare_multifactor/robustness/evidence_workflow_successor_release.py",
)
FINAL_EXECUTION_PATHS = (
    "configs/final_execution_sources.yaml",
    "configs/market_rules.yaml",
    "src/ashare_multifactor/final_test/action_source_contract.py",
    "src/ashare_multifactor/final_test/backtest.py",
    "src/ashare_multifactor/final_test/coverage_snapshot.py",
    "src/ashare_multifactor/final_test/execution_contracts.py",
    "src/ashare_multifactor/final_test/execution_identity.py",
    "src/ashare_multifactor/final_test/pipeline.py",
    "src/ashare_multifactor/final_test/resume.py",
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", *args), cwd=CODE_ROOT, capture_output=True, text=True, check=False
    )


def _archive_available() -> bool:
    return _git("cat-file", "-e", f"{ARCHIVE_COMMIT}^{{commit}}").returncode == 0


def test_archived_paths_are_absent_from_main() -> None:
    present = [relative for relative in ARCHIVED_PATHS if (CODE_ROOT / relative).exists()]
    assert present == []


def test_no_source_module_imports_the_final_test_package() -> None:
    offenders: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            modules: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            if any(module.startswith(FINAL_TEST_PREFIX) for module in modules):
                offenders.append(f"{path.relative_to(CODE_ROOT)}:{node.lineno}")
    assert offenders == []


def test_every_module_on_main_imports_without_the_archived_code() -> None:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = "src"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    script = "\n".join(
        (
            "import importlib, json, pkgutil, sys",
            "import ashare_multifactor",
            "names = [info.name for info in pkgutil.walk_packages(",
            "    ashare_multifactor.__path__, 'ashare_multifactor.')]",
            "for name in names:",
            "    importlib.import_module(name)",
            "loaded = sorted(m for m in sys.modules if m.startswith("
            f"{FINAL_TEST_PREFIX!r}))",
            "print(json.dumps({'modules': len(names), 'final_test': loaded}))",
        )
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=CODE_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())
    assert payload["modules"] > 0
    assert payload["final_test"] == []


def test_every_declared_console_script_resolves() -> None:
    scripts = tomllib.loads((CODE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["scripts"]
    assert "ashare-final-test" not in scripts
    for target in scripts.values():
        module_name, function_name = target.split(":")
        assert callable(getattr(importlib.import_module(module_name), function_name))


def test_robustness_cli_offers_only_stage_eight_commands() -> None:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = "src"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from ashare_multifactor.cli.robustness import main; main()",
            "rehearse-evidence",
        ],
        cwd=CODE_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "invalid choice" in result.stderr


def test_v1_robustness_entry_fails_closed_without_release_state(tmp_path: Path) -> None:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = "src"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from ashare_multifactor.cli.robustness import main; main()",
            "run",
            "--root",
            str(tmp_path),
        ],
        cwd=CODE_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert list(tmp_path.iterdir()) == []


def test_frozen_identity_paths_are_unchanged() -> None:
    from ashare_multifactor.robustness.protocol_identities import _FINAL_EXECUTION_PATHS

    assert _FINAL_EXECUTION_PATHS == FINAL_EXECUTION_PATHS


def test_every_frozen_identity_path_is_restorable_from_main_or_the_archive() -> None:
    if not _archive_available():
        pytest.skip("the archived commit is not in this clone's history")
    from ashare_multifactor.robustness.protocol_identities import (
        _EVIDENCE_WORKFLOW_EXTRA_PATHS,
        _FINAL_EXECUTION_PATHS,
    )

    unrecoverable = [
        relative
        for relative in (*_FINAL_EXECUTION_PATHS, *_EVIDENCE_WORKFLOW_EXTRA_PATHS)
        if not (CODE_ROOT / relative).is_file()
        and _git("cat-file", "-e", f"{ARCHIVE_COMMIT}:{relative}").returncode != 0
    ]
    assert unrecoverable == []


def test_archived_commit_still_contains_the_final_test_package() -> None:
    if not _archive_available():
        pytest.skip("the archived commit is not in this clone's history")
    listing = _git("ls-tree", "--name-only", ARCHIVE_COMMIT, "src/ashare_multifactor/final_test/")
    assert listing.returncode == 0
    files = [line for line in listing.stdout.splitlines() if line.endswith(".py")]
    assert len(files) == 91
