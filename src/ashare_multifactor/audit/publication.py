from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
from typing import Mapping
from uuid import uuid4

from ashare_multifactor.audit.records import file_record, sha256_file, verify_file_record
from ashare_multifactor.audit.secure_tree import (
    frozen_records,
    verify_frozen_tree_at,
    write_frozen_tree_at,
)


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
    frozen_artifact_trees: Mapping[str, Mapping[str, bytes]] | None = None,
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
    releases_fd = os.open(
        releases,
        os.O_RDONLY | getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW"),
    )
    temporary_fd: int | None = None
    artifacts_fd: int | None = None
    temporary_identity: tuple[int, int] | None = None
    try:
        shutil.copytree(staged_datasets, temporary / "datasets")
        frozen = dict(frozen_artifact_trees or {})
        shutil.copytree(
            staged_artifacts,
            temporary / "artifacts",
            ignore=(shutil.ignore_patterns(*frozen) if frozen else None),
        )
        temporary_fd = os.open(
            temporary.name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW"),
            dir_fd=releases_fd,
        )
        temporary_identity = _descriptor_identity(temporary_fd)
        artifacts_fd = os.open(
            "artifacts",
            os.O_RDONLY | getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW"),
            dir_fd=temporary_fd,
        )
        artifacts_identity = _descriptor_identity(artifacts_fd)
        try:
            for name, files in sorted(frozen.items()):
                if name in {"", ".", ".."} or "/" in name or "\\" in name:
                    raise ValueError("frozen artifact tree name is invalid")
                write_frozen_tree_at(
                    artifacts_fd,
                    name,
                    files,
                    resumable=False,
                    label=f"frozen artifact tree {name}",
                )
            _assert_entry_identity(
                temporary_fd,
                "artifacts",
                artifacts_identity,
                label="release artifacts staging",
            )
            for name, files in sorted(frozen.items()):
                verify_frozen_tree_at(
                    artifacts_fd,
                    name,
                    files,
                    label=f"frozen artifact tree {name}",
                )
        except BaseException:
            os.close(artifacts_fd)
            os.close(temporary_fd)
            artifacts_fd = None
            temporary_fd = None
            raise
        _write_json(temporary / "lineage.json", lineage)
        _assert_entry_identity(
            releases_fd,
            temporary.name,
            temporary_identity,
            label="release temporary staging",
        )
        records = []
        for path in sorted(item for item in temporary.rglob("*") if item.is_file()):
            relative = path.relative_to(temporary)
            if (
                len(relative.parts) >= 2
                and relative.parts[0] == "artifacts"
                and relative.parts[1] in frozen
            ):
                continue
            role = "lineage" if path.name == "lineage.json" else path.parts[-2]
            records.append(file_record(path, root=temporary, role=role).to_dict())
        for name, files in sorted(frozen.items()):
            records.extend(
                frozen_records(
                    files,
                    prefix=f"artifacts/{name}",
                    role=name,
                )
            )
        _write_json(
            temporary / "manifest.json",
            {"files": records, "run_id": run_id, **(manifest_metadata or {})},
        )
        _assert_entry_identity(
            releases_fd,
            temporary.name,
            temporary_identity,
            label="release temporary staging",
        )
        _assert_entry_identity(
            temporary_fd,
            "artifacts",
            artifacts_identity,
            label="release artifacts staging",
        )
        for name, files in sorted(frozen.items()):
            verify_frozen_tree_at(
                artifacts_fd,
                name,
                files,
                label=f"frozen artifact tree {name}",
            )
        os.close(artifacts_fd)
        os.close(temporary_fd)
        artifacts_fd = None
        temporary_fd = None
        _assert_entry_identity(
            releases_fd,
            temporary.name,
            temporary_identity,
            label="release temporary staging",
        )
        os.rename(
            temporary.name,
            final.name,
            src_dir_fd=releases_fd,
            dst_dir_fd=releases_fd,
        )
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
        published = _published(root, run_id, manifest_hash)
        os.close(releases_fd)
        return published
    except BaseException:
        if artifacts_fd is not None:
            os.close(artifacts_fd)
        if temporary_fd is not None:
            os.close(temporary_fd)
        try:
            if temporary_identity is not None:
                _assert_entry_identity(
                    releases_fd,
                    temporary.name,
                    temporary_identity,
                    label="release temporary staging",
                )
                shutil.rmtree(temporary, ignore_errors=True)
        except (FileNotFoundError, ValueError):
            pass
        os.close(releases_fd)
        raise


def _descriptor_identity(descriptor: int) -> tuple[int, int]:
    metadata = os.fstat(descriptor)
    return metadata.st_dev, metadata.st_ino


def _assert_entry_identity(
    parent_fd: int,
    name: str,
    expected: tuple[int, int],
    *,
    label: str,
) -> None:
    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (metadata.st_dev, metadata.st_ino) != expected:
        raise ValueError(f"{label} identity changed")


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
