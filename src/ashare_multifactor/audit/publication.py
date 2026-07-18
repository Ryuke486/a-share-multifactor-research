from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
from typing import Iterator, Mapping
from uuid import uuid4

from ashare_multifactor.audit.secure_tree import (
    atomic_rename_no_replace_at,
    read_frozen_tree_at,
    write_frozen_tree_at,
)
from ashare_multifactor.audit.publication_lock import (
    publication_read_lock_at,
    publication_write_lock_at,
)


_CURRENT_PUBLICATION_LOCK = ".CURRENT.publication.lock"


@dataclass(frozen=True)
class PublishedRelease:
    run_id: str
    root: Path
    datasets: Path
    artifacts: Path
    manifest: Path
    lineage: Path
    manifest_sha256: str
    directory_identity: tuple[int, int] | None = None


@dataclass
class HeldPublishedRelease:
    release: PublishedRelease
    files: dict[str, bytes]
    manifest: dict[str, object]
    root_parent_fd: int
    root_fd: int
    releases_fd: int
    release_fd: int
    current_fd: int
    root_identity: tuple[int, int]
    releases_identity: tuple[int, int]
    release_identity: tuple[int, int]
    current_identity: tuple[int, int]

    def assert_unchanged(self) -> None:
        _assert_entry_identity(
            self.root_parent_fd,
            self.release.root.parent.parent.name,
            self.root_identity,
            label="publication root",
        )
        _assert_entry_identity(
            self.root_fd,
            "releases",
            self.releases_identity,
            label="release root",
        )
        _assert_entry_identity(
            self.releases_fd,
            self.release.run_id,
            self.release_identity,
            label="published release",
        )
        _assert_file_entry_identity(
            self.root_fd,
            "CURRENT.json",
            self.current_identity,
            label="CURRENT pointer",
        )


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
    current_must_be_absent: bool = False,
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
            path: payload for path, payload in release_files.items() if not path.startswith(prefix)
        }
        release_files.update({f"{prefix}{path}": payload for path, payload in files.items()})
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
    releases_identity = _descriptor_identity(releases_fd)
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
        _assert_entry_identity(
            root_fd,
            "releases",
            releases_identity,
            label="release root",
        )
        temporary_fd = os.open(
            temporary.name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW"),
            dir_fd=releases_fd,
        )
        temporary_identity = _descriptor_identity(temporary_fd)
        if read_frozen_tree_at(temporary_fd, label="release temporary staging") != complete_files:
            raise ValueError("release temporary staging bytes differ")
        _assert_entry_identity(
            root_fd,
            "releases",
            releases_identity,
            label="release root",
        )
        atomic_rename_no_replace_at(releases_fd, temporary.name, final.name)
        _assert_entry_identity(
            releases_fd,
            final.name,
            temporary_identity,
            label="release temporary staging",
        )
        _assert_entry_identity(
            root_fd,
            "releases",
            releases_identity,
            label="release root",
        )
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        if fail_before_switch:
            raise RuntimeError("injected failure before pointer switch")
        pointer = {"manifest_sha256": manifest_hash, "run_id": run_id}
        if current_must_be_absent:
            _write_current_at(root_fd, pointer)
        else:
            _replace_current_at(root_fd, pointer)
        _assert_entry_identity(
            root_fd,
            "releases",
            releases_identity,
            label="release root",
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
        os.close(releases_fd)
        os.close(root_fd)
        os.close(root_parent_fd)
        raise


def publish_release_at(
    root: Path,
    *,
    root_parent_fd: int,
    root_fd: int,
    releases_fd: int,
    run_id: str,
    frozen_release_files: Mapping[str, bytes],
    manifest_metadata: Mapping[str, object] | None = None,
    current_must_be_absent: bool = False,
    fail_before_switch: bool = False,
) -> PublishedRelease:
    """Publish through one caller-held publication-root descriptor chain."""
    release_files = dict(frozen_release_files)
    manifest_bytes = _json_payload_bytes(
        {
            "files": _release_records(release_files),
            "run_id": run_id,
            **dict(manifest_metadata or {}),
        }
    )
    complete_files = {**release_files, "manifest.json": manifest_bytes}
    root_identity = _descriptor_identity(root_fd)
    releases_identity = _descriptor_identity(releases_fd)
    _assert_entry_identity(root_parent_fd, root.name, root_identity, label="publication root")
    _assert_entry_identity(root_fd, "releases", releases_identity, label="release root")
    try:
        os.stat(run_id, dir_fd=releases_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(f"release already exists: {run_id}")
    temporary_name = f".{run_id}.{uuid4().hex}.tmp"
    temporary_fd: int | None = None
    try:
        write_frozen_tree_at(
            releases_fd,
            temporary_name,
            complete_files,
            resumable=False,
            label="release temporary staging",
        )
        temporary_fd = os.open(
            temporary_name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW"),
            dir_fd=releases_fd,
        )
        temporary_identity = _descriptor_identity(temporary_fd)
        if read_frozen_tree_at(temporary_fd, label="release temporary staging") != complete_files:
            raise ValueError("release temporary staging bytes differ")
        _assert_entry_identity(root_parent_fd, root.name, root_identity, label="publication root")
        _assert_entry_identity(root_fd, "releases", releases_identity, label="release root")
        atomic_rename_no_replace_at(releases_fd, temporary_name, run_id)
        _assert_entry_identity(releases_fd, run_id, temporary_identity, label="published release")
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        if fail_before_switch:
            raise RuntimeError("injected failure before pointer switch")
        pointer = {"manifest_sha256": manifest_hash, "run_id": run_id}
        if current_must_be_absent:
            _write_current_at(root_fd, pointer)
        else:
            _replace_current_at(root_fd, pointer)
        _assert_entry_identity(root_parent_fd, root.name, root_identity, label="publication root")
        _assert_entry_identity(root_fd, "releases", releases_identity, label="release root")
        _assert_entry_identity(releases_fd, run_id, temporary_identity, label="published release")
        return _published(
            root,
            run_id,
            manifest_hash,
            directory_identity=temporary_identity,
        )
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)


def resolve_release_at(root: Path, releases_fd: int, run_id: str) -> PublishedRelease:
    release, _, _ = resolve_release_package_at(root, releases_fd, run_id)
    return release


def resolve_release_package_at(
    root: Path, releases_fd: int, run_id: str
) -> tuple[PublishedRelease, dict[str, bytes], dict[str, object]]:
    release_fd = os.open(
        run_id,
        os.O_RDONLY | getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW"),
        dir_fd=releases_fd,
    )
    try:
        identity = _descriptor_identity(release_fd)
        manifest_hash, files, manifest = _verify_release_at(release_fd, run_id=run_id)
        _assert_entry_identity(releases_fd, run_id, identity, label="release")
        return (
            _published(root, run_id, manifest_hash, directory_identity=identity),
            files,
            manifest,
        )
    finally:
        os.close(release_fd)


def restore_current_at(
    root_fd: int,
    releases_fd: int,
    release: PublishedRelease,
) -> None:
    release_fd = os.open(
        release.run_id,
        os.O_RDONLY | getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW"),
        dir_fd=releases_fd,
    )
    try:
        identity = _descriptor_identity(release_fd)
        if release.directory_identity is not None and identity != release.directory_identity:
            raise ValueError("orphan release identity changed before CURRENT recovery")
        manifest_hash, _, _ = _verify_release_at(
            release_fd,
            run_id=release.run_id,
            expected_manifest_sha256=release.manifest_sha256,
        )
        _write_current_at(
            root_fd,
            {"run_id": release.run_id, "manifest_sha256": manifest_hash},
        )
        _assert_entry_identity(releases_fd, release.run_id, identity, label="orphan release")
    finally:
        os.close(release_fd)


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
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _write_current_at(root_fd: int, payload: Mapping[str, object]) -> None:
    _materialize_current_at(root_fd, payload, replace=False)


def _replace_current_at(root_fd: int, payload: Mapping[str, object]) -> None:
    _materialize_current_at(root_fd, payload, replace=True)


def _materialize_current_at(
    root_fd: int,
    payload: Mapping[str, object],
    *,
    replace: bool,
) -> None:
    with publication_write_lock_at(root_fd, _CURRENT_PUBLICATION_LOCK):
        _materialize_current_locked_at(root_fd, payload, replace=replace)


def _materialize_current_locked_at(
    root_fd: int,
    payload: Mapping[str, object],
    *,
    replace: bool,
) -> None:
    temporary = f".CURRENT.{uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW"),
        0o600,
        dir_fd=root_fd,
    )
    expected = _json_payload_bytes(payload)
    installed_identity: tuple[int, int] | None = None
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(expected)
            stream.flush()
            os.fsync(descriptor)
        temporary_identity = _descriptor_identity(descriptor)
        _assert_file_entry_identity(
            root_fd, temporary, temporary_identity, label="CURRENT temporary"
        )
        if replace:
            os.replace(
                temporary,
                "CURRENT.json",
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
            )
        else:
            os.link(
                temporary,
                "CURRENT.json",
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
                follow_symlinks=False,
            )
        current_fd = os.open(
            "CURRENT.json",
            os.O_RDONLY | getattr(os, "O_NOFOLLOW"),
            dir_fd=root_fd,
        )
        try:
            installed_identity = _descriptor_identity(current_fd)
            with os.fdopen(current_fd, "rb", closefd=False) as stream:
                installed = stream.read()
        finally:
            os.close(current_fd)
        if installed_identity != temporary_identity or installed != expected:
            _unlink_if_identity(root_fd, "CURRENT.json", installed_identity)
            raise ValueError("CURRENT pointer identity or bytes changed")
        os.fsync(root_fd)
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


def _assert_file_entry_identity(
    parent_fd: int,
    name: str,
    expected: tuple[int, int],
    *,
    label: str,
) -> None:
    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (metadata.st_dev, metadata.st_ino) != expected:
        raise ValueError(f"{label} identity changed")


def _unlink_if_identity(parent_fd: int, name: str, expected: tuple[int, int] | None) -> None:
    if expected is None:
        return
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (metadata.st_dev, metadata.st_ino) == expected:
        os.unlink(name, dir_fd=parent_fd)


def resolve_current(root: Path) -> PublishedRelease:
    with opened_verified_current(root) as held:
        return held.release


@contextmanager
def opened_verified_current(root: Path) -> Iterator[HeldPublishedRelease]:
    root_parent_fd = os.open(
        root.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW")
    )
    root_fd: int | None = None
    try:
        root_fd = os.open(
            root.name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW"),
            dir_fd=root_parent_fd,
        )
        with publication_read_lock_at(root_fd, _CURRENT_PUBLICATION_LOCK):
            with _opened_verified_current_locked(
                root,
                root_parent_fd=root_parent_fd,
                root_fd=root_fd,
            ) as held:
                yield held
    finally:
        if root_fd is not None:
            os.close(root_fd)
        os.close(root_parent_fd)


@contextmanager
def _opened_verified_current_locked(
    root: Path,
    *,
    root_parent_fd: int,
    root_fd: int,
) -> Iterator[HeldPublishedRelease]:
    releases_fd = os.open(
        "releases",
        os.O_RDONLY | getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW"),
        dir_fd=root_fd,
    )
    current_fd: int | None = None
    release_fd: int | None = None
    try:
        current_fd = os.open(
            "CURRENT.json",
            os.O_RDONLY | getattr(os, "O_NOFOLLOW"),
            dir_fd=root_fd,
        )
        root_identity = _descriptor_identity(root_fd)
        releases_identity = _descriptor_identity(releases_fd)
        current_identity = _descriptor_identity(current_fd)
        with os.fdopen(current_fd, "rb", closefd=False) as stream:
            pointer = json.loads(stream.read())
        if not isinstance(pointer, dict) or set(pointer) != {"run_id", "manifest_sha256"}:
            raise ValueError("CURRENT pointer identity differs")
        run_id = str(pointer["run_id"])
        release_fd = os.open(
            run_id,
            os.O_RDONLY | getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW"),
            dir_fd=releases_fd,
        )
        release_identity = _descriptor_identity(release_fd)
        manifest_hash, files, manifest = _verify_release_at(
            release_fd,
            run_id=run_id,
            expected_manifest_sha256=str(pointer["manifest_sha256"]),
        )
        held = HeldPublishedRelease(
            release=_published(root, run_id, manifest_hash, directory_identity=release_identity),
            files=files,
            manifest=manifest,
            root_parent_fd=root_parent_fd,
            root_fd=root_fd,
            releases_fd=releases_fd,
            release_fd=release_fd,
            current_fd=current_fd,
            root_identity=root_identity,
            releases_identity=releases_identity,
            release_identity=release_identity,
            current_identity=current_identity,
        )
        held.assert_unchanged()
        yield held
        held.assert_unchanged()
    finally:
        if release_fd is not None:
            os.close(release_fd)
        if current_fd is not None:
            os.close(current_fd)
        os.close(releases_fd)


def resolve_release(root: Path, run_id: str) -> PublishedRelease:
    """Resolve and fully verify one release without consulting CURRENT."""
    root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW"))
    releases_fd = os.open(
        "releases",
        os.O_RDONLY | getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW"),
        dir_fd=root_fd,
    )
    release_fd = os.open(
        run_id,
        os.O_RDONLY | getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW"),
        dir_fd=releases_fd,
    )
    try:
        identity = _descriptor_identity(release_fd)
        manifest_hash, _, _ = _verify_release_at(release_fd, run_id=run_id)
        _assert_entry_identity(releases_fd, run_id, identity, label="release")
        return _published(root, run_id, manifest_hash, directory_identity=identity)
    finally:
        os.close(release_fd)
        os.close(releases_fd)
        os.close(root_fd)


def restore_current(root: Path, release: PublishedRelease) -> None:
    """Atomically create CURRENT for one already verified orphan release."""
    root_parent_fd = os.open(
        root.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW"),
    )
    root_fd = os.open(
        root.name,
        os.O_RDONLY | getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW"),
        dir_fd=root_parent_fd,
    )
    releases_fd = os.open(
        "releases",
        os.O_RDONLY | getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW"),
        dir_fd=root_fd,
    )
    release_fd = os.open(
        release.run_id,
        os.O_RDONLY | getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW"),
        dir_fd=releases_fd,
    )
    try:
        root_identity = _descriptor_identity(root_fd)
        releases_identity = _descriptor_identity(releases_fd)
        release_identity = _descriptor_identity(release_fd)
        if release.directory_identity is None or release_identity != release.directory_identity:
            raise ValueError("orphan release identity changed before CURRENT recovery")
        manifest_hash, _, _ = _verify_release_at(
            release_fd,
            run_id=release.run_id,
            expected_manifest_sha256=release.manifest_sha256,
        )
        _write_current_at(
            root_fd,
            {
                "manifest_sha256": manifest_hash,
                "run_id": release.run_id,
            },
        )
        _assert_entry_identity(
            root_parent_fd,
            root.name,
            root_identity,
            label="publication root",
        )
        _assert_entry_identity(
            root_fd,
            "releases",
            releases_identity,
            label="release root",
        )
        _assert_entry_identity(
            releases_fd,
            release.run_id,
            release_identity,
            label="orphan release",
        )
    finally:
        os.close(release_fd)
        os.close(releases_fd)
        os.close(root_fd)
        os.close(root_parent_fd)


def _verify_release_at(
    release_fd: int,
    *,
    run_id: str,
    expected_manifest_sha256: str | None = None,
) -> tuple[str, dict[str, bytes], dict[str, object]]:
    complete = read_frozen_tree_at(release_fd, label="published release")
    try:
        manifest_bytes = complete.pop("manifest.json")
        manifest = json.loads(manifest_bytes)
    except (KeyError, json.JSONDecodeError) as error:
        raise ValueError("invalid release manifest") from error
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    if expected_manifest_sha256 is not None and manifest_hash != expected_manifest_sha256:
        raise ValueError("orphan release identity changed before CURRENT recovery")
    if not isinstance(manifest, dict) or manifest.get("run_id") != run_id:
        raise ValueError("release manifest identity mismatch")
    records = manifest.get("files")
    if not isinstance(records, list):
        raise ValueError("release manifest identity mismatch")
    expected_records = {str(record["path"]): record for record in _release_records(complete)}
    actual_records = {
        str(record.get("path")): record for record in records if isinstance(record, dict)
    }
    if set(actual_records) != set(expected_records) or len(records) != len(actual_records):
        raise ValueError("release manifest identity mismatch")
    for path, expected in expected_records.items():
        actual = actual_records[path]
        if actual.get("size_bytes") != expected["size_bytes"]:
            raise ValueError("published release file size mismatch")
        if actual.get("sha256") != expected["sha256"]:
            raise ValueError("published release file digest mismatch")
        if actual.get("role") != expected["role"]:
            raise ValueError("release manifest identity mismatch")
    return manifest_hash, complete, manifest


def _published(
    root: Path,
    run_id: str,
    manifest_hash: str,
    *,
    directory_identity: tuple[int, int] | None = None,
) -> PublishedRelease:
    release = root / "releases" / run_id
    return PublishedRelease(
        run_id=run_id,
        root=release,
        datasets=release / "datasets",
        artifacts=release / "artifacts",
        manifest=release / "manifest.json",
        lineage=release / "lineage.json",
        manifest_sha256=manifest_hash,
        directory_identity=directory_identity,
    )


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
