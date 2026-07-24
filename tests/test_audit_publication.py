import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event, get_ident

import pytest

from ashare_multifactor.audit.publication import (
    publish_release,
    resolve_current,
    resolve_release,
    restore_current,
)
from ashare_multifactor.audit import publication as publication_module
from ashare_multifactor.audit.identity import code_identity


def _staged(tmp_path: Path) -> tuple[Path, Path]:
    datasets = tmp_path / "staged-datasets"
    artifacts = tmp_path / "staged-artifacts"
    datasets.mkdir(parents=True)
    artifacts.mkdir(parents=True)
    (datasets / "target_weights.parquet").write_bytes(b"weights")
    (artifacts / "report.md").write_text("report", encoding="utf-8")
    return datasets, artifacts


def test_current_resolver_waits_until_installed_pointer_is_postverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "published"
    datasets, artifacts = _staged(tmp_path / "first")
    publish_release(
        root,
        run_id="first",
        staged_datasets=datasets,
        staged_artifacts=artifacts,
        lineage={"identity": "first"},
    )
    second_datasets, second_artifacts = _staged(tmp_path / "second")
    installed = Event()
    release_writer = Event()
    writer_thread: dict[str, int] = {}
    original_open = publication_module.os.open

    def pause_postverify(path: object, *args: object, **kwargs: object) -> int:
        if (
            path == "CURRENT.json"
            and get_ident() == writer_thread.get("id")
            and not installed.is_set()
        ):
            installed.set()
            if not release_writer.wait(timeout=5):
                raise TimeoutError("test did not release CURRENT writer")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(publication_module.os, "open", pause_postverify)

    def publish_second() -> object:
        writer_thread["id"] = get_ident()
        return publish_release(
            root,
            run_id="second",
            staged_datasets=second_datasets,
            staged_artifacts=second_artifacts,
            lineage={"identity": "second"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(publish_second)
        assert installed.wait(timeout=5)
        resolver = executor.submit(resolve_current, root)
        try:
            with pytest.raises(TimeoutError):
                resolver.result(timeout=0.2)
        finally:
            release_writer.set()
        assert writer.result(timeout=5).run_id == "second"
        assert resolver.result(timeout=5).run_id == "second"


def test_failed_release_does_not_change_current_pointer(tmp_path: Path) -> None:
    datasets, artifacts = _staged(tmp_path)
    root = tmp_path / "published"
    first = publish_release(
        root,
        run_id="first",
        staged_datasets=datasets,
        staged_artifacts=artifacts,
        lineage={"identity": "first"},
    )
    bad_datasets, bad_artifacts = _staged(tmp_path / "second")

    with pytest.raises(RuntimeError, match="before pointer switch"):
        publish_release(
            root,
            run_id="second",
            staged_datasets=bad_datasets,
            staged_artifacts=bad_artifacts,
            lineage={"identity": "second"},
            fail_before_switch=True,
        )

    assert resolve_current(root).run_id == first.run_id


def test_resolver_detects_manifest_or_dataset_tampering(tmp_path: Path) -> None:
    datasets, artifacts = _staged(tmp_path)
    root = tmp_path / "published"
    published = publish_release(
        root,
        run_id="only",
        staged_datasets=datasets,
        staged_artifacts=artifacts,
        lineage={"identity": "only"},
    )
    target = published.datasets / "target_weights.parquet"
    target.write_bytes(b"tampered")

    with pytest.raises(ValueError, match="mismatch"):
        resolve_current(root)


def test_current_pointer_contains_manifest_hash(tmp_path: Path) -> None:
    datasets, artifacts = _staged(tmp_path)
    root = tmp_path / "published"
    published = publish_release(
        root,
        run_id="only",
        staged_datasets=datasets,
        staged_artifacts=artifacts,
        lineage={"identity": "only"},
    )

    current = json.loads((root / "CURRENT.json").read_text(encoding="utf-8"))
    assert current == {"manifest_sha256": published.manifest_sha256, "run_id": "only"}


def test_release_manifest_preserves_dataset_contract_metadata(tmp_path: Path) -> None:
    datasets, artifacts = _staged(tmp_path)
    root = tmp_path / "published"
    published = publish_release(
        root,
        run_id="only",
        staged_datasets=datasets,
        staged_artifacts=artifacts,
        lineage={"identity": "only"},
        manifest_metadata={"datasets": [{"dataset": "target_weights", "rows": 10}]},
    )

    manifest = json.loads(published.manifest.read_text(encoding="utf-8"))
    assert manifest["datasets"] == [{"dataset": "target_weights", "rows": 10}]


def test_publish_ignores_replaced_staged_copy_for_frozen_artifact_tree(
    tmp_path: Path,
) -> None:
    datasets, artifacts = _staged(tmp_path)
    staged = artifacts / "execution_inputs"
    staged.mkdir()
    (staged / "manifest.json").write_bytes(b"attacker staged bytes")
    frozen = {"manifest.json": b"registry-bound bytes"}

    published = publish_release(
        tmp_path / "published",
        run_id="frozen",
        staged_datasets=datasets,
        staged_artifacts=artifacts,
        lineage={"identity": "frozen"},
        frozen_artifact_trees={"execution_inputs": frozen},
    )

    assert (
        published.artifacts / "execution_inputs/manifest.json"
    ).read_bytes() == b"registry-bound bytes"


def test_publish_rejects_artifacts_parent_replacement_during_frozen_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    datasets, artifacts = _staged(tmp_path)
    root = tmp_path / "published"
    write_tree = publication_module.write_frozen_tree_at
    swapped = False

    def replace_artifacts(*args: object, **kwargs: object) -> None:
        nonlocal swapped
        write_tree(*args, **kwargs)
        if not swapped:
            staging = root / "releases/.race.tmp"
            (staging / "artifacts").rename(staging / "artifacts.displaced")
            (staging / "artifacts").mkdir()
            swapped = True

    monkeypatch.setattr(
        publication_module,
        "write_frozen_tree_at",
        replace_artifacts,
    )

    with pytest.raises(ValueError, match="temporary staging bytes differ"):
        publish_release(
            root,
            run_id="race",
            staged_datasets=datasets,
            staged_artifacts=artifacts,
            lineage={"identity": "race"},
            frozen_artifact_trees={"execution_inputs": {"manifest.json": b"frozen"}},
        )

    assert not (root / "releases/race").exists()


def test_publish_rejects_temporary_directory_replacement_before_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    datasets, artifacts = _staged(tmp_path)
    root = tmp_path / "published"
    rename = publication_module.atomic_rename_no_replace_at
    swapped = False

    def replace_temporary_then_rename(
        parent_fd: int,
        source: str,
        destination: str,
    ) -> None:
        nonlocal swapped
        if not swapped and source == ".race.tmp":
            temporary = root / "releases/.race.tmp"
            displaced = root / "releases/.race.displaced"
            publication_module.os.rename(temporary, displaced)
            shutil.copytree(displaced, temporary)
            swapped = True
        rename(parent_fd, source, destination)

    monkeypatch.setattr(
        publication_module,
        "atomic_rename_no_replace_at",
        replace_temporary_then_rename,
    )

    with pytest.raises(ValueError, match="temporary staging identity changed"):
        publish_release(
            root,
            run_id="race",
            staged_datasets=datasets,
            staged_artifacts=artifacts,
            lineage={"identity": "race"},
            current_must_be_absent=True,
        )

    assert not (root / "CURRENT.json").exists()


def test_publish_rejects_releases_directory_replacement_before_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    datasets, artifacts = _staged(tmp_path)
    root = tmp_path / "published"
    rename = publication_module.atomic_rename_no_replace_at
    displaced = root / "releases.displaced"
    swapped = False

    def replace_releases_then_rename(
        parent_fd: int,
        source: str,
        destination: str,
    ) -> None:
        nonlocal swapped
        if not swapped:
            (root / "releases").rename(displaced)
            (root / "releases").mkdir()
            swapped = True
        rename(parent_fd, source, destination)

    monkeypatch.setattr(
        publication_module,
        "atomic_rename_no_replace_at",
        replace_releases_then_rename,
    )

    with pytest.raises(ValueError, match="release root identity changed"):
        publish_release(
            root,
            run_id="race",
            staged_datasets=datasets,
            staged_artifacts=artifacts,
            lineage={"identity": "race"},
            current_must_be_absent=True,
        )

    assert (displaced / "race/manifest.json").is_file()
    assert not (root / "CURRENT.json").exists()


def test_publish_rejects_publication_root_replacement_before_current_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    datasets, artifacts = _staged(tmp_path)
    root = tmp_path / "published"
    displaced = tmp_path / "published.displaced"
    write_current = publication_module._write_current_at

    def replace_root(root_fd: int, payload: dict[str, object]) -> None:
        root.rename(displaced)
        root.mkdir()
        write_current(root_fd, payload)

    monkeypatch.setattr(publication_module, "_write_current_at", replace_root)

    with pytest.raises(ValueError, match="publication root identity changed"):
        publish_release(
            root,
            run_id="race",
            staged_datasets=datasets,
            staged_artifacts=artifacts,
            lineage={"identity": "race"},
            current_must_be_absent=True,
        )

    assert (displaced / "CURRENT.json").is_file()
    assert not (root / "CURRENT.json").exists()


def test_one_shot_current_does_not_overwrite_concurrent_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    datasets, artifacts = _staged(tmp_path)
    root = tmp_path / "published"
    encode = publication_module._json_payload_bytes
    competitor = b'{"run_id":"competitor"}\n'
    injected = False

    def inject_after_temporary_open(payload: dict[str, object]) -> bytes:
        nonlocal injected
        encoded = encode(payload)
        if set(payload) == {"manifest_sha256", "run_id"} and not injected:
            injected = True
            (root / "CURRENT.json").write_bytes(competitor)
        return encoded

    monkeypatch.setattr(
        publication_module,
        "_json_payload_bytes",
        inject_after_temporary_open,
    )

    with pytest.raises(FileExistsError):
        publish_release(
            root,
            run_id="one-shot",
            staged_datasets=datasets,
            staged_artifacts=artifacts,
            lineage={"identity": "one-shot"},
            current_must_be_absent=True,
        )

    assert (root / "CURRENT.json").read_bytes() == competitor


def test_orphan_recovery_does_not_overwrite_concurrent_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    datasets, artifacts = _staged(tmp_path)
    root = tmp_path / "published"
    with pytest.raises(RuntimeError, match="before pointer switch"):
        publish_release(
            root,
            run_id="orphan",
            staged_datasets=datasets,
            staged_artifacts=artifacts,
            lineage={"identity": "orphan"},
            current_must_be_absent=True,
            fail_before_switch=True,
        )
    orphan = resolve_release(root, "orphan")
    encode = publication_module._json_payload_bytes
    competitor = b'{"run_id":"competitor"}\n'
    injected = False

    def inject_after_temporary_open(payload: dict[str, object]) -> bytes:
        nonlocal injected
        encoded = encode(payload)
        if set(payload) == {"manifest_sha256", "run_id"} and not injected:
            injected = True
            (root / "CURRENT.json").write_bytes(competitor)
        return encoded

    monkeypatch.setattr(
        publication_module,
        "_json_payload_bytes",
        inject_after_temporary_open,
    )

    with pytest.raises(FileExistsError):
        restore_current(root, orphan)

    assert (root / "CURRENT.json").read_bytes() == competitor


def test_orphan_recovery_rejects_release_replaced_after_resolution(
    tmp_path: Path,
) -> None:
    datasets, artifacts = _staged(tmp_path)
    root = tmp_path / "published"
    with pytest.raises(RuntimeError, match="before pointer switch"):
        publish_release(
            root,
            run_id="orphan",
            staged_datasets=datasets,
            staged_artifacts=artifacts,
            lineage={"identity": "orphan"},
            current_must_be_absent=True,
            fail_before_switch=True,
        )
    orphan = resolve_release(root, "orphan")
    release_root = root / "releases/orphan"
    displaced = tmp_path / "displaced-orphan"
    release_root.rename(displaced)
    shutil.copytree(displaced, release_root)

    with pytest.raises(ValueError, match="orphan release identity changed"):
        restore_current(root, orphan)

    assert not (root / "CURRENT.json").exists()


@pytest.mark.parametrize("current_must_be_absent", [True, False])
def test_current_pointer_rejects_temporary_file_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    current_must_be_absent: bool,
) -> None:
    datasets, artifacts = _staged(tmp_path)
    root = tmp_path / "published"
    original_link = publication_module.os.link
    original_replace = publication_module.os.replace
    forged = b'{"manifest_sha256":"forged","run_id":"forged"}\n'

    def substitute(source: str, destination: str, **kwargs: object) -> None:
        source_fd = int(kwargs["src_dir_fd"])
        publication_module.os.unlink(source, dir_fd=source_fd)
        descriptor = publication_module.os.open(
            source,
            publication_module.os.O_WRONLY
            | publication_module.os.O_CREAT
            | publication_module.os.O_EXCL,
            0o600,
            dir_fd=source_fd,
        )
        with publication_module.os.fdopen(descriptor, "wb") as stream:
            stream.write(forged)
        operation = original_link if current_must_be_absent else original_replace
        operation(source, destination, **kwargs)

    monkeypatch.setattr(
        publication_module.os,
        "link" if current_must_be_absent else "replace",
        substitute,
    )

    with pytest.raises(ValueError, match="CURRENT.*identity|CURRENT.*bytes"):
        publish_release(
            root,
            run_id="one-shot",
            staged_datasets=datasets,
            staged_artifacts=artifacts,
            lineage={"identity": "one-shot"},
            current_must_be_absent=current_must_be_absent,
        )

    assert not (root / "CURRENT.json").exists() or (root / "CURRENT.json").read_bytes() != forged


def test_code_identity_records_dirty_diff_and_source_hashes(tmp_path: Path) -> None:
    source = tmp_path / "src" / "package" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    for args in (
        ("init",),
        ("config", "user.email", "test@example.invalid"),
        ("config", "user.name", "Test User"),
        ("add", "src/package/module.py"),
        ("commit", "-m", "baseline"),
    ):
        subprocess.run(
            ("git", *args),
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
    source.write_text("VALUE = 2\n", encoding="utf-8")

    identity = code_identity(tmp_path)

    assert identity["commit"]
    assert len(identity["tree"]) == 40
    assert identity["dirty"] is True
    assert len(identity["diff_sha256"]) == 64
    assert identity["sources"] == [
        {
            "path": "src/package/module.py",
            "role": "source",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "size_bytes": source.stat().st_size,
        }
    ]
