from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

from ashare_multifactor.audit.records import file_record, sha256_file, verify_file_record


@dataclass(frozen=True)
class PublishedRelease:
    run_id: str
    root: Path
    datasets: Path
    artifacts: Path
    manifest: Path
    lineage: Path
    manifest_sha256: str


def publish_release(
    root: Path,
    *,
    run_id: str,
    staged_datasets: Path,
    staged_artifacts: Path,
    lineage: dict[str, object],
    manifest_metadata: dict[str, object] | None = None,
    fail_before_switch: bool = False,
) -> PublishedRelease:
    releases = root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    final = releases / run_id
    if final.exists():
        raise FileExistsError(f"release already exists: {run_id}")
    temporary = releases / f".{run_id}.tmp"
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir()
    try:
        shutil.copytree(staged_datasets, temporary / "datasets")
        shutil.copytree(staged_artifacts, temporary / "artifacts")
        _write_json(temporary / "lineage.json", lineage)
        records = []
        for path in sorted(item for item in temporary.rglob("*") if item.is_file()):
            role = "lineage" if path.name == "lineage.json" else path.parts[-2]
            records.append(file_record(path, root=temporary, role=role).to_dict())
        _write_json(
            temporary / "manifest.json",
            {"files": records, "run_id": run_id, **(manifest_metadata or {})},
        )
        os.replace(temporary, final)
        manifest_hash = sha256_file(final / "manifest.json")
        if fail_before_switch:
            raise RuntimeError("injected failure before pointer switch")
        root.mkdir(parents=True, exist_ok=True)
        pointer_tmp = root / ".CURRENT.json.tmp"
        _write_json(
            pointer_tmp,
            {"manifest_sha256": manifest_hash, "run_id": run_id},
        )
        os.replace(pointer_tmp, root / "CURRENT.json")
        return _published(root, run_id, manifest_hash)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def resolve_current(root: Path) -> PublishedRelease:
    pointer = json.loads((root / "CURRENT.json").read_text(encoding="utf-8"))
    published = _published(root, pointer["run_id"], pointer["manifest_sha256"])
    if sha256_file(published.manifest) != published.manifest_sha256:
        raise ValueError("manifest digest mismatch")
    manifest = json.loads(published.manifest.read_text(encoding="utf-8"))
    for item in manifest["files"]:
        verify_file_record(item, root=published.root)
    return published


def resolve_release(root: Path, run_id: str) -> PublishedRelease:
    """Resolve and fully verify one release without consulting CURRENT."""
    release = root / "releases" / run_id
    if release.is_symlink() or not release.is_dir():
        raise ValueError("release is missing or uses a symlink")
    manifest = release / "manifest.json"
    manifest_hash = sha256_file(manifest)
    published = _published(root, run_id, manifest_hash)
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError("invalid release manifest") from error
    if not isinstance(payload, dict) or payload.get("run_id") != run_id:
        raise ValueError("release manifest identity differs")
    records = payload.get("files")
    if not isinstance(records, list):
        raise ValueError("release manifest files are missing")
    for item in records:
        verify_file_record(item, root=release)
    return published


def restore_current(root: Path, release: PublishedRelease) -> None:
    """Atomically create CURRENT for one already verified orphan release."""
    verified = resolve_release(root, release.run_id)
    if verified.manifest_sha256 != release.manifest_sha256:
        raise ValueError("orphan release identity changed before CURRENT recovery")
    current = root / "CURRENT.json"
    if current.exists() or current.is_symlink():
        raise FileExistsError("CURRENT already exists")
    temporary = root / f".CURRENT.{uuid4().hex}.tmp"
    _write_json(
        temporary,
        {"manifest_sha256": release.manifest_sha256, "run_id": release.run_id},
    )
    try:
        os.link(temporary, current)
    finally:
        temporary.unlink(missing_ok=True)


def _published(root: Path, run_id: str, manifest_hash: str) -> PublishedRelease:
    release = root / "releases" / run_id
    return PublishedRelease(
        run_id=run_id,
        root=release,
        datasets=release / "datasets",
        artifacts=release / "artifacts",
        manifest=release / "manifest.json",
        lineage=release / "lineage.json",
        manifest_sha256=manifest_hash,
    )


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
