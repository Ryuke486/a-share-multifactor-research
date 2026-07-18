from pathlib import Path
import hashlib
import json
from datetime import date
import os

import ashare_multifactor.final_test as final_test
import pytest

from ashare_multifactor.final_test.registry import (
    append_attempt_outcome,
    append_attempt_state,
)
from test_build import _config, _write_pair
from test_final_test_data import _authorized_context, _build


def test_failed_attempt_archive_api_is_available(tmp_path: Path) -> None:
    del tmp_path
    assert callable(getattr(final_test, "archive_failed_attempt", None))


def test_incident_manifest_atomic_publisher_is_available() -> None:
    from ashare_multifactor.final_test import incident_archive

    assert callable(getattr(incident_archive, "_publish_manifest_atomic", None))


def test_incident_manifest_partial_write_is_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ashare_multifactor.final_test import incident_archive

    root = tmp_path / "archive-source"
    root.mkdir()
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    original_writer = incident_archive.write_bytes_exclusive_at

    def interrupted_writer(parent_fd: int, name: str, payload: bytes) -> None:
        del payload
        file_descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_fd,
        )
        try:
            os.write(file_descriptor, b"{")
        finally:
            os.close(file_descriptor)
        raise KeyboardInterrupt("injected partial manifest write")

    monkeypatch.setattr(
        incident_archive,
        "write_bytes_exclusive_at",
        interrupted_writer,
    )
    try:
        with pytest.raises(KeyboardInterrupt, match="partial manifest"):
            incident_archive._publish_manifest_atomic(descriptor, b'{"valid":true}\n')
        assert not (root / "incident_manifest.json").exists()
        assert list(root.iterdir()) == []

        monkeypatch.setattr(
            incident_archive,
            "write_bytes_exclusive_at",
            original_writer,
        )
        incident_archive._publish_manifest_atomic(descriptor, b'{"valid":true}\n')
    finally:
        os.close(descriptor)

    assert (root / "incident_manifest.json").read_bytes() == b'{"valid":true}\n'


def test_failed_attempt_archive_rejects_incomplete_attempt_evidence(
    tmp_path: Path,
) -> None:
    processed = tmp_path / "processed"
    root = processed / "final_test"
    attempts = root / "attempts"
    attempts.mkdir(parents=True)
    attempt_id = "attempt-001"
    (root / "data-build-claim.json").write_text(
        json.dumps({"attempt_id": attempt_id, "status": "published"}) + "\n",
        encoding="utf-8",
    )
    (attempts / f"{attempt_id}.outcome.json").write_text(
        json.dumps(
            {
                "attempt_id": attempt_id,
                "status": "failed",
                "authoritative": False,
                "reason": "unsupported market symbols",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="attempt|panel|evidence|lock"):
        final_test.archive_failed_attempt(processed, attempt_id=attempt_id)

    assert root.is_dir()


def test_failed_attempt_archive_preserves_complete_evidence_and_frees_active_root(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _write_pair(config, date(2022, 1, 3))
    code_root, authorization = _authorized_context(tmp_path, config)
    _build(config, authorization, code_root)
    processed = config.paths.processed
    root = processed / "final_test"
    attempts = root / "attempts"
    attempt_id = authorization.attempt_id
    append_attempt_state(attempts, attempt_id=attempt_id, state="preparing")
    append_attempt_outcome(
        attempts,
        attempt_id=attempt_id,
        status="failed",
        authoritative=False,
        reason="unsupported market symbols",
    )
    (attempts / f"{attempt_id}.preparation.lock").touch()
    original = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }

    archived = final_test.archive_failed_attempt(processed, attempt_id=attempt_id)

    assert archived == processed / "final_test_incidents" / attempt_id
    assert not root.exists()
    assert {
        path.relative_to(archived).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(archived.rglob("*"))
        if path.is_file() and path.name != "incident_manifest.json"
    } == original
    manifest = json.loads((archived / "incident_manifest.json").read_text())
    assert manifest["attempt_id"] == attempt_id
    assert manifest["status"] == "archived_failed_non_authoritative_attempt"
    assert manifest["reason"] == "unsupported market symbols"
    assert {
        record["path"].removeprefix("final_test/"): record["sha256"]
        for record in manifest["files"]
    } == original


def test_failed_attempt_archive_refuses_source_directory_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _write_pair(config, date(2022, 1, 3))
    code_root, authorization = _authorized_context(tmp_path, config)
    _build(config, authorization, code_root)
    processed = config.paths.processed
    root = processed / "final_test"
    attempts = root / "attempts"
    append_attempt_state(attempts, attempt_id=authorization.attempt_id, state="preparing")
    append_attempt_outcome(
        attempts,
        attempt_id=authorization.attempt_id,
        status="failed",
        authoritative=False,
        reason="unsupported market symbols",
    )
    (attempts / f"{authorization.attempt_id}.preparation.lock").touch()
    from ashare_multifactor.final_test import incident_archive

    original_records = incident_archive._evidence_records
    calls = 0
    displaced = processed / "displaced-original"

    def replace_after_final_scan(directory_fd: int) -> list[dict[str, object]]:
        nonlocal calls
        records = original_records(directory_fd)
        calls += 1
        if calls == 2:
            root.rename(displaced)
            root.mkdir()
        return records

    monkeypatch.setattr(incident_archive, "_evidence_records", replace_after_final_scan)

    with pytest.raises(ValueError):
        final_test.archive_failed_attempt(processed, attempt_id=authorization.attempt_id)

    assert displaced.is_dir()
    assert root.is_dir()
    assert not (
        processed / "final_test_incidents" / authorization.attempt_id
    ).exists()


def test_failed_attempt_archive_recovers_after_source_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _write_pair(config, date(2022, 1, 3))
    code_root, authorization = _authorized_context(tmp_path, config)
    _build(config, authorization, code_root)
    processed = config.paths.processed
    root = processed / "final_test"
    attempts = root / "attempts"
    append_attempt_state(attempts, attempt_id=authorization.attempt_id, state="preparing")
    append_attempt_outcome(
        attempts,
        attempt_id=authorization.attempt_id,
        status="failed",
        authoritative=False,
        reason="unsupported market symbols",
    )
    (attempts / f"{authorization.attempt_id}.preparation.lock").touch()
    from ashare_multifactor.final_test import incident_archive

    original_assert = incident_archive.assert_directory_entry
    calls = 0

    def interrupt_after_rename(*args: object, **kwargs: object) -> None:
        nonlocal calls
        original_assert(*args, **kwargs)
        calls += 1
        if calls == 3:
            raise KeyboardInterrupt("injected crash after source rename")

    monkeypatch.setattr(
        incident_archive,
        "assert_directory_entry",
        interrupt_after_rename,
    )
    with pytest.raises(KeyboardInterrupt, match="after source rename"):
        final_test.archive_failed_attempt(processed, attempt_id=authorization.attempt_id)

    destination = processed / "final_test_incidents" / authorization.attempt_id
    assert not root.exists()
    assert destination.is_dir()
    monkeypatch.setattr(
        incident_archive,
        "assert_directory_entry",
        original_assert,
    )
    assert final_test.archive_failed_attempt(
        processed,
        attempt_id=authorization.attempt_id,
    ) == destination


def test_failed_attempt_archive_rejects_mixed_authoritative_root(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _write_pair(config, date(2022, 1, 3))
    code_root, authorization = _authorized_context(tmp_path, config)
    _build(config, authorization, code_root)
    processed = config.paths.processed
    root = processed / "final_test"
    attempts = root / "attempts"
    append_attempt_state(attempts, attempt_id=authorization.attempt_id, state="preparing")
    append_attempt_outcome(
        attempts,
        attempt_id=authorization.attempt_id,
        status="failed",
        authoritative=False,
        reason="unsupported market symbols",
    )
    (attempts / f"{authorization.attempt_id}.preparation.lock").touch()
    (root / "CURRENT.json").write_text('{"run_id":"authoritative"}\n')

    with pytest.raises(ValueError, match="mixed or authoritative"):
        final_test.archive_failed_attempt(processed, attempt_id=authorization.attempt_id)

    assert root.is_dir()
    assert not (
        processed / "final_test_incidents" / authorization.attempt_id
    ).exists()


def test_failed_attempt_archive_recovers_orphan_manifest_temp(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _write_pair(config, date(2022, 1, 3))
    code_root, authorization = _authorized_context(tmp_path, config)
    _build(config, authorization, code_root)
    processed = config.paths.processed
    root = processed / "final_test"
    attempts = root / "attempts"
    append_attempt_state(attempts, attempt_id=authorization.attempt_id, state="preparing")
    append_attempt_outcome(
        attempts,
        attempt_id=authorization.attempt_id,
        status="failed",
        authoritative=False,
        reason="unsupported market symbols",
    )
    (attempts / f"{authorization.attempt_id}.preparation.lock").touch()
    orphan = root / f".incident-manifest.{'a' * 32}.tmp"
    orphan.write_bytes(b"{")

    destination = final_test.archive_failed_attempt(
        processed,
        attempt_id=authorization.attempt_id,
    )

    assert destination.is_dir()
    assert not (destination / orphan.name).exists()


def test_failed_attempt_archive_rejects_claim_attempt_identity_drift(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _write_pair(config, date(2022, 1, 3))
    code_root, authorization = _authorized_context(tmp_path, config)
    _build(config, authorization, code_root)
    processed = config.paths.processed
    root = processed / "final_test"
    attempts = root / "attempts"
    append_attempt_state(attempts, attempt_id=authorization.attempt_id, state="preparing")
    append_attempt_outcome(
        attempts,
        attempt_id=authorization.attempt_id,
        status="failed",
        authoritative=False,
        reason="unsupported market symbols",
    )
    (attempts / f"{authorization.attempt_id}.preparation.lock").touch()
    claim_path = root / "data-build-claim.json"
    claim = json.loads(claim_path.read_text())
    claim["approval_id"] = "different-approval"
    claim_path.write_text(json.dumps(claim) + "\n")

    with pytest.raises(ValueError, match="identity"):
        final_test.archive_failed_attempt(processed, attempt_id=authorization.attempt_id)

    assert root.is_dir()


def test_failed_attempt_archive_rejects_inventory_panel_identity_drift(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _write_pair(config, date(2022, 1, 3))
    code_root, authorization = _authorized_context(tmp_path, config)
    _build(config, authorization, code_root)
    processed = config.paths.processed
    root = processed / "final_test"
    attempts = root / "attempts"
    append_attempt_state(attempts, attempt_id=authorization.attempt_id, state="preparing")
    append_attempt_outcome(
        attempts,
        attempt_id=authorization.attempt_id,
        status="failed",
        authoritative=False,
        reason="unsupported market symbols",
    )
    (attempts / f"{authorization.attempt_id}.preparation.lock").touch()
    inventory_path = root / f"data-build-inputs/{authorization.attempt_id}.json"
    inventory = json.loads(inventory_path.read_text())
    inventory["tampered_after_publication"] = True
    inventory_path.write_text(json.dumps(inventory) + "\n")
    claim_path = root / "data-build-claim.json"
    claim = json.loads(claim_path.read_text())
    claim["input_inventory"]["sha256"] = hashlib.sha256(
        inventory_path.read_bytes()
    ).hexdigest()
    claim["input_inventory"]["size_bytes"] = inventory_path.stat().st_size
    claim_path.write_text(json.dumps(claim) + "\n")

    with pytest.raises(ValueError, match="inventory.*identity"):
        final_test.archive_failed_attempt(processed, attempt_id=authorization.attempt_id)

    assert root.is_dir()


def test_failed_attempt_archive_rejects_claim_inventory_path_substitution(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _write_pair(config, date(2022, 1, 3))
    code_root, authorization = _authorized_context(tmp_path, config)
    _build(config, authorization, code_root)
    processed = config.paths.processed
    root = processed / "final_test"
    attempts = root / "attempts"
    append_attempt_state(attempts, attempt_id=authorization.attempt_id, state="preparing")
    append_attempt_outcome(
        attempts,
        attempt_id=authorization.attempt_id,
        status="failed",
        authoritative=False,
        reason="unsupported market symbols",
    )
    (attempts / f"{authorization.attempt_id}.preparation.lock").touch()
    substituted = root / "daily_panel/input_files.json"
    claim_path = root / "data-build-claim.json"
    claim = json.loads(claim_path.read_text())
    claim["input_inventory"] = {
        "relative_path": "daily_panel/input_files.json",
        "sha256": hashlib.sha256(substituted.read_bytes()).hexdigest(),
        "size_bytes": substituted.stat().st_size,
    }
    claim_path.write_text(json.dumps(claim) + "\n")

    with pytest.raises(ValueError, match="inventory.*path"):
        final_test.archive_failed_attempt(processed, attempt_id=authorization.attempt_id)

    assert root.is_dir()
