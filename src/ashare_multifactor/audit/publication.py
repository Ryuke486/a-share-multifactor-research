from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Mapping
from uuid import uuid4

from ashare_multifactor.audit.records import sha256_file, verify_file_record
from ashare_multifactor.audit.secure_tree import (
    atomic_rename_no_replace_at,
    read_frozen_tree_at,
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
    frozen_release_files: Mapping[str, bytes] | None = None,
    fail_before_switch: bool = False,
) -> PublishedRelease:
    release_files = (
        dict(frozen_release_files)
        if frozen_release_files is not None
        else _freeze_staged_release(
            staged_datasets,
            staged_artifacts,
            lineage=lineage,
        )
    )
    for name, files in sorted(dict(frozen_artifact_trees or {}).items()):
        prefix = f"artifacts/{name}/"
        release_files = {
            path: payload
            for path, payload in release_files.items()
            if not path.startswith(prefix)
        }
        release_files.update(
            {f"{prefix}{path}": payload for path, payload in files.items()}
        )
    records = _release_records(release_files)
    manifest_bytes = _json_payload_bytes(
        {"files": records, "run_id": run_id, **(manifest_metadata or {})}
    )
    complete_files = {**release_files, "manifest.json": manifest_bytes}
    root.mkdir(parents=True, exist_ok=True)
    root_parent_fd = os.open(
        root.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW"),
    )
    root_fd = os.open(
        root.name,
        os.O_RDONLY | getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW"),
        dir_fd=root_parent_fd,
    )
    root_identity = _descriptor_identity(root_fd)
    releases = root / "releases"
    try:
        os.mkdir("releases", mode=0o700, dir_fd=root_fd)
    except FileExistsError:
        pass
    final = releases / run_id
    if final.exists():
        raise FileExistsError(f"release already exists: {run_id}")
    temporary = releases / f".{run_id}.tmp"
    releases_fd = os.open(
        "releases",
        os.O_RDONLY | getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW"),
        dir_fd=root_fd,
    )
    temporary_fd: int | None = None
    temporary_identity: tuple[int, int] | None = None
    try:
        write_frozen_tree_at(
            releases_fd,
            temporary.name,
            complete_files,
            resumable=False,
            label="release temporary staging",
        )
        temporary_fd = os.open(
            temporary.name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW"),
            dir_fd=releases_fd,
        )
        temporary_identity = _descriptor_identity(temporary_fd)
        if read_frozen_tree_at(
            temporary_fd, label="release temporary staging"
        ) != complete_files:
            raise ValueError("release temporary staging bytes differ")
        atomic_rename_no_replace_at(releases_fd, temporary.name, final.name)
        _assert_entry_identity(
            releases_fd,
            final.name,
            temporary_identity,
            label="release temporary staging",
        )
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        if fail_before_switch:
            raise RuntimeError("injected failure before pointer switch")
        _write_current_at(
            root_fd,
            {"manifest_sha256": manifest_hash, "run_id": run_id},
        )
        _assert_entry_identity(
            releases_fd,
            final.name,
            temporary_identity,
            label="published release",
        )
        _assert_entry_identity(
            root_parent_fd,
            root.name,
            root_identity,
            label="publication root",
        )
        os.close(temporary_fd)
        temporary_fd = None
        published = _published(root, run_id, manifest_hash)
        os.close(releases_fd)
        os.close(root_fd)
        os.close(root_parent_fd)
        return published
    except BaseException:
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
        os.close(root_fd)
        os.close(root_parent_fd)
        raise


def _freeze_staged_release(
    staged_datasets: Path,
    staged_artifacts: Path,
    *,
    lineage: Mapping[str, object],
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for prefix, path in (
        ("datasets", staged_datasets),
        ("artifacts", staged_artifacts),
    ):
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW"),
        )
        try:
            frozen = read_frozen_tree_at(descriptor, label=f"staged {prefix}")
        finally:
            os.close(descriptor)
        files.update({f"{prefix}/{name}": payload for name, payload in frozen.items()})
    files["lineage.json"] = _json_payload_bytes(lineage)
    return files


def _release_records(files: Mapping[str, bytes]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path, payload in sorted(files.items()):
        parts = Path(path).parts
        role = "lineage" if path == "lineage.json" else parts[-2]
        records.append(
            {
                "path": path,
                "role": role,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        )
    return records


def _json_payload_bytes(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _write_current_at(root_fd: int, payload: Mapping[str, object]) -> None:
    temporary = f".CURRENT.{uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW"),
        0o600,
        dir_fd=root_fd,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(_json_payload_bytes(payload))
            stream.flush()
            os.fsync(descriptor)
        os.replace(
            temporary,
            "CURRENT.json",
            src_dir_fd=root_fd,
            dst_dir_fd=root_fd,
        )
    finally:
        os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=root_fd)
        except FileNotFoundError:
            pass


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
