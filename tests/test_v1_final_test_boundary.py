"""Boundary contract between the v1.0 layers and the deferred final test.

The final-test package is archived as optional v2.0 work. v1.0 computation and
release code must therefore stay free of it: the only crossing allowed is the
explicitly named bridge, which is the ``rehearse-evidence`` operator command and
the lazy compatibility facade it needs.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import ashare_multifactor


CODE_ROOT = Path(ashare_multifactor.__file__).resolve().parents[2]
SOURCE_ROOT = CODE_ROOT / "src/ashare_multifactor"
FINAL_TEST_PREFIX = "ashare_multifactor.final_test"
BRIDGE_FACADE = SOURCE_ROOT / "robustness/evidence_workflow_rehearsal.py"
NEW_REHEARSAL_PATH = "src/ashare_multifactor/final_test/evidence_workflow_rehearsal.py"
LEGACY_REHEARSAL_PATH = "src/ashare_multifactor/robustness/evidence_workflow_rehearsal.py"

V1_LIBRARY_PACKAGES = (
    "audit",
    "combination",
    "data",
    "execution",
    "factors",
    "portfolio",
    "research",
    "robustness",
    "validation",
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


def _v1_library_files() -> list[Path]:
    files = [
        SOURCE_ROOT / "__init__.py",
        SOURCE_ROOT / "__main__.py",
        SOURCE_ROOT / "config.py",
    ]
    for package in V1_LIBRARY_PACKAGES:
        files.extend(sorted((SOURCE_ROOT / package).rglob("*.py")))
    return [path for path in files if path.is_file()]


def _run_in_project(script: str) -> str:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = "src"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=CODE_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_v1_library_layers_do_not_import_the_final_test_package() -> None:
    offenders: list[str] = []
    for path in _v1_library_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            modules: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            if any(module.startswith(FINAL_TEST_PREFIX) for module in modules):
                offenders.append(f"{path.relative_to(CODE_ROOT)}:{node.lineno}")
    assert offenders == []


def test_the_only_final_test_mention_in_v1_layers_is_the_documented_facade() -> None:
    mentioning = {
        path
        for path in _v1_library_files()
        if FINAL_TEST_PREFIX in path.read_text(encoding="utf-8")
    }
    assert mentioning == {BRIDGE_FACADE}


def test_importing_v1_library_layers_never_loads_final_test() -> None:
    script = "\n".join(
        (
            "import importlib, json, pkgutil, sys",
            f"packages = {list(V1_LIBRARY_PACKAGES)!r}",
            "for name in packages:",
            "    package = importlib.import_module('ashare_multifactor.' + name)",
            "    for info in pkgutil.walk_packages(",
            "        package.__path__, package.__name__ + '.'",
            "    ):",
            "        importlib.import_module(info.name)",
            "importlib.import_module('ashare_multifactor.cli.validation')",
            "loaded = sorted(m for m in sys.modules if m.startswith("
            f"{FINAL_TEST_PREFIX!r}))",
            "print(json.dumps(loaded))",
        )
    )
    assert json.loads(_run_in_project(script).strip()) == []


def test_legacy_rehearsal_path_resolves_lazily_through_the_bridge() -> None:
    script = "\n".join(
        (
            "import importlib, json, sys",
            "module = importlib.import_module("
            "'ashare_multifactor.robustness.evidence_workflow_rehearsal')",
            "before = sorted(m for m in sys.modules if m.startswith("
            f"{FINAL_TEST_PREFIX!r}))",
            "function = module.build_evidence_workflow_rehearsal",
            "after = sorted(m for m in sys.modules if m.startswith("
            f"{FINAL_TEST_PREFIX!r}))",
            "print(json.dumps({",
            "    'loaded_before': before,",
            "    'loaded_after_nonempty': bool(after),",
            "    'module': function.__module__,",
            "}))",
        )
    )
    payload = json.loads(_run_in_project(script).strip())
    assert payload["loaded_before"] == []
    assert payload["loaded_after_nonempty"] is True
    assert payload["module"] == (
        "ashare_multifactor.final_test.evidence_workflow_rehearsal"
    )


def test_legacy_rehearsal_path_rejects_unknown_attributes() -> None:
    from ashare_multifactor.robustness import evidence_workflow_rehearsal

    try:
        evidence_workflow_rehearsal.not_a_rehearsal_symbol
    except AttributeError as error:
        assert "not_a_rehearsal_symbol" in str(error)
    else:  # pragma: no cover - the facade must not invent attributes
        raise AssertionError("unknown facade attribute was resolved")


def test_v1_robustness_entry_fails_closed_without_final_test_state(
    tmp_path: Path,
) -> None:
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


def test_identity_contract_paths_survive_the_rehearsal_relocation() -> None:
    from ashare_multifactor.robustness.protocol_identities import (
        _EVIDENCE_WORKFLOW_EXTRA_PATHS,
        _FINAL_EXECUTION_PATHS,
        build_evidence_workflow_identity,
        build_final_execution_identity,
    )

    assert _FINAL_EXECUTION_PATHS == FINAL_EXECUTION_PATHS
    execution = build_final_execution_identity(CODE_ROOT)
    assert set(execution["records"]) == set(FINAL_EXECUTION_PATHS)
    workflow = build_evidence_workflow_identity(CODE_ROOT)
    assert NEW_REHEARSAL_PATH in workflow["records"]
    assert LEGACY_REHEARSAL_PATH in workflow["records"]
    assert NEW_REHEARSAL_PATH in _EVIDENCE_WORKFLOW_EXTRA_PATHS
    assert all(
        (CODE_ROOT / relative).is_file() for relative in workflow["records"]
    )
