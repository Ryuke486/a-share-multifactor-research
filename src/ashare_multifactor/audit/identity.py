from __future__ import annotations

import hashlib
from importlib.metadata import version
from pathlib import Path
import platform
import subprocess

from ashare_multifactor.audit.records import file_record


def code_identity(root: Path) -> dict[str, object]:
    root = root.resolve()
    commit = _git(root, "rev-parse", "HEAD").strip()
    tree = _git(root, "rev-parse", "HEAD^{tree}").strip()
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    diff = _git(root, "diff", "--no-ext-diff", "--binary", "HEAD")
    normalized = (status + "\n" + diff).replace(str(root), "<ROOT>").encode()
    sources = [
        file_record(path, root=root, role="source").to_dict()
        for path in sorted((root / "src").rglob("*.py"))
    ]
    return {
        "commit": commit,
        "tree": tree,
        "dirty": bool(status.strip()),
        "diff_sha256": hashlib.sha256(normalized).hexdigest(),
        "sources": sources,
        "python": platform.python_version(),
        "dependencies": {
            package: version(package)
            for package in (
                "baostock",
                "numpy",
                "polars",
                "scipy",
                "PyYAML",
                "matplotlib",
            )
        },
    }


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout
