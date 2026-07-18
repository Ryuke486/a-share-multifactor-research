from __future__ import annotations

import os
from pathlib import Path

import pytest

from ashare_multifactor.final_test.incident_snapshot import (
    copied_directory_snapshot,
    working_directory_at,
)
from ashare_multifactor.final_test.recovery_secure_fs import opened_directory


def test_directory_snapshot_keeps_original_content_after_source_path_replacement(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    archive = tmp_path / "archive"
    (source / "nested").mkdir(parents=True)
    archive.mkdir()
    (source / "nested" / "evidence.json").write_text('{"original":true}\n')

    with opened_directory(source, label="source") as source_fd:
        with opened_directory(archive, label="archive") as archive_fd:
            with copied_directory_snapshot(
                source_fd,
                archive_fd,
                label="test evidence",
            ) as snapshot:
                displaced = tmp_path / "displaced"
                source.rename(displaced)
                (source / "nested").mkdir(parents=True)
                (source / "nested" / "evidence.json").write_text('{"replacement":true}\n')

                with working_directory_at(snapshot.descriptor):
                    assert Path("nested/evidence.json").read_text() == '{"original":true}\n'

                assert not (archive / snapshot.name).is_symlink()

    assert list(archive.iterdir()) == []


def test_directory_snapshot_rejects_symbolic_link_input(tmp_path: Path) -> None:
    source = tmp_path / "source"
    archive = tmp_path / "archive"
    source.mkdir()
    archive.mkdir()
    target = tmp_path / "target"
    target.write_text("untrusted")
    os.symlink(target, source / "evidence-link")

    with opened_directory(source, label="source") as source_fd:
        with opened_directory(archive, label="archive") as archive_fd:
            with pytest.raises(ValueError, match="symlink"):
                with copied_directory_snapshot(
                    source_fd,
                    archive_fd,
                    label="test evidence",
                ):
                    pass

    assert list(archive.iterdir()) == []
