"""Immutable human decisions that may later produce execution coverages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

import polars as pl

from ashare_multifactor.audit.secure_tree import (
    read_frozen_tree_at,
    write_frozen_tree_at,
)
from ashare_multifactor.final_test.gate import FINAL_TEST_END, FINAL_TEST_START
from ashare_multifactor.final_test.official_evidence_workspace import (
    EvidenceWorkspace,
    VerifiedReviewQueue,
    load_verified_review_queue,
)
from ashare_multifactor.final_test.official_query_collection_root import (
    opened_safe_directory,
)
from ashare_multifactor.final_test.official_query_coverage import canonical_json_bytes
from ashare_multifactor.final_test.recovery_secure_fs import open_directory_at
from ashare_multifactor.final_test.official_review_contract import (
    CANDIDATE_FIELDS,
    validate_review_frames,
)


SUBMISSIONS_DIRECTORY = "official_review_submissions"
MANIFEST_NAME = "submission_manifest.json"
ANNOUNCEMENT_DECISIONS_NAME = "announcement_decisions.parquet"
CORPORATE_DISPOSITIONS_NAME = "corporate_action_dispositions.parquet"
CORPORATE_FACTS_NAME = "corporate_action_facts.parquet"
SECURITY_FACTS_NAME = "security_event_facts.parquet"

_SCHEMA_VERSION = "1"
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, eq=False)
class VerifiedReviewSubmission:
    """Complete reviewer decisions bound to one queue and candidate snapshot."""

    root: Path
    manifest_path: Path
    manifest_sha256: str
    ready: bool
    announcement_decisions: pl.DataFrame
    corporate_action_dispositions: pl.DataFrame
    corporate_action_facts: pl.DataFrame
    security_event_facts: pl.DataFrame

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, VerifiedReviewSubmission):
            return NotImplemented
        return (
            self.root == other.root
            and self.manifest_path == other.manifest_path
            and self.manifest_sha256 == other.manifest_sha256
            and self.ready == other.ready
            and self.announcement_decisions.equals(other.announcement_decisions)
            and self.corporate_action_dispositions.equals(
                other.corporate_action_dispositions
            )
            and self.corporate_action_facts.equals(other.corporate_action_facts)
            and self.security_event_facts.equals(other.security_event_facts)
        )


@dataclass(frozen=True)
class VerifiedCandidateSnapshot:
    """One complete candidate snapshot with its frozen bytes rechecked."""

    manifest_path: Path
    manifest_sha256: str
    manifest: dict[str, object]
    candidates: pl.DataFrame


def publish_review_submission(
    *,
    workspace: EvidenceWorkspace,
    candidate_manifest_path: Path | None,
    announcement_decisions: pl.DataFrame,
    corporate_action_dispositions: pl.DataFrame,
    corporate_action_facts: pl.DataFrame,
    security_event_facts: pl.DataFrame,
    reviewer_id: str,
    reviewed_at: str,
) -> VerifiedReviewSubmission:
    """Validate every reviewer field before immutable publication."""
    queue = load_verified_review_queue(workspace)
    candidates = load_verified_candidate_snapshot(
        candidate_manifest_path,
        workspace=workspace,
    )
    reviewer, timestamp = _review_metadata(reviewer_id, reviewed_at)
    frames = validate_review_frames(
        queue,
        candidates.candidates,
        announcement_decisions=announcement_decisions,
        corporate_action_dispositions=corporate_action_dispositions,
        corporate_action_facts=corporate_action_facts,
        security_event_facts=security_event_facts,
    )
    data = {
        ANNOUNCEMENT_DECISIONS_NAME: _parquet_bytes(frames[0]),
        CORPORATE_DISPOSITIONS_NAME: _parquet_bytes(frames[1]),
        CORPORATE_FACTS_NAME: _parquet_bytes(frames[2]),
        SECURITY_FACTS_NAME: _parquet_bytes(frames[3]),
    }
    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "role": "official_evidence_review_submission",
        "status": "ready",
        "attempt_id": workspace.root.parent.name,
        "review_session_id": queue.session_id,
        "review_manifest_sha256": queue.manifest_sha256,
        "candidate_manifest_sha256": candidates.manifest_sha256,
        "reviewer_id": reviewer,
        "reviewed_at": timestamp,
        "files": [
            {
                "path": name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
                "row_count": frames[index].height,
            }
            for index, (name, payload) in enumerate(sorted(data.items()))
        ],
    }
    manifest_bytes = canonical_json_bytes(manifest)
    submission_id = hashlib.sha256(manifest_bytes).hexdigest()
    destination = workspace.root.parent
    with opened_safe_directory(destination, label="official evidence destination") as root_fd:
        submissions_fd = _open_or_create_directory(
            root_fd,
            SUBMISSIONS_DIRECTORY,
            label="official review submissions",
        )
        try:
            write_frozen_tree_at(
                submissions_fd,
                submission_id,
                {**data, MANIFEST_NAME: manifest_bytes},
                resumable=True,
                label="official review submission",
            )
        finally:
            os.close(submissions_fd)
    return load_verified_review_submission(
        destination / SUBMISSIONS_DIRECTORY / submission_id / MANIFEST_NAME,
        workspace=workspace,
        candidate_manifest_path=candidate_manifest_path,
    )


def load_verified_review_submission(
    manifest_path: Path,
    *,
    workspace: EvidenceWorkspace,
    candidate_manifest_path: Path | None,
) -> VerifiedReviewSubmission:
    """Reload a published submission and repeat all semantic checks."""
    queue = load_verified_review_queue(workspace)
    candidates = load_verified_candidate_snapshot(
        candidate_manifest_path,
        workspace=workspace,
    )
    absolute = manifest_path.absolute()
    root = absolute.parent
    expected_parent = workspace.root.parent / SUBMISSIONS_DIRECTORY
    if (
        absolute.name != MANIFEST_NAME
        or root.parent.absolute() != expected_parent.absolute()
        or _SHA256.fullmatch(root.name) is None
    ):
        raise ValueError("official review submission path is not canonical")
    _assert_safe_directory(root, label="official review submission")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        files = read_frozen_tree_at(descriptor, label="official review submission")
    finally:
        os.close(descriptor)
    expected_names = {
        MANIFEST_NAME,
        ANNOUNCEMENT_DECISIONS_NAME,
        CORPORATE_DISPOSITIONS_NAME,
        CORPORATE_FACTS_NAME,
        SECURITY_FACTS_NAME,
    }
    if set(files) != expected_names:
        raise ValueError("official review submission inventory is invalid")
    manifest = _canonical_object(files[MANIFEST_NAME], label="review submission manifest")
    _validate_submission_manifest(
        manifest,
        files=files,
        queue=queue,
        candidates=candidates,
        attempt_id=workspace.root.parent.name,
    )
    try:
        raw_frames = (
            pl.read_parquet(BytesIO(files[ANNOUNCEMENT_DECISIONS_NAME])),
            pl.read_parquet(BytesIO(files[CORPORATE_DISPOSITIONS_NAME])),
            pl.read_parquet(BytesIO(files[CORPORATE_FACTS_NAME])),
            pl.read_parquet(BytesIO(files[SECURITY_FACTS_NAME])),
        )
    except pl.exceptions.PolarsError as error:
        raise ValueError("official review submission parquet is invalid") from error
    frames = validate_review_frames(
        queue,
        candidates.candidates,
        announcement_decisions=raw_frames[0],
        corporate_action_dispositions=raw_frames[1],
        corporate_action_facts=raw_frames[2],
        security_event_facts=raw_frames[3],
    )
    if any(not actual.equals(expected) for actual, expected in zip(raw_frames, frames, strict=True)):
        raise ValueError("official review submission rows are not canonical")
    return VerifiedReviewSubmission(
        root=root,
        manifest_path=absolute,
        manifest_sha256=hashlib.sha256(files[MANIFEST_NAME]).hexdigest(),
        ready=True,
        announcement_decisions=frames[0],
        corporate_action_dispositions=frames[1],
        corporate_action_facts=frames[2],
        security_event_facts=frames[3],
    )


def load_verified_candidate_snapshot(
    manifest_path: Path | None,
    *,
    workspace: EvidenceWorkspace,
) -> VerifiedCandidateSnapshot:
    """Reload a candidate snapshot after verifying its complete frozen tree."""
    if manifest_path is None:
        raise ValueError("corporate-action candidate snapshot is incomplete")
    absolute = manifest_path.absolute()
    root = absolute.parent
    if (
        absolute.name != "manifest.json"
        or root.name != "snapshot"
        or root.parent.name != "corporate_action_candidate_collection"
        or root.parent.parent.absolute() != workspace.root.parent.absolute()
    ):
        raise ValueError("corporate-action candidate manifest path is not canonical")
    _assert_safe_directory(root, label="corporate-action candidate snapshot")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        files = read_frozen_tree_at(
            descriptor,
            label="corporate-action candidate snapshot",
        )
    finally:
        os.close(descriptor)
    manifest = _canonical_object(
        files.get("manifest.json", b""),
        label="corporate-action candidate manifest",
    )
    records = manifest.get("files")
    if (
        manifest.get("role") != "baostock_corporate_action_candidate_snapshot"
        or manifest.get("status") != "complete"
        or manifest.get("attempt_id") != workspace.root.parent.name
        or manifest.get("period")
        != [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()]
        or not isinstance(records, list)
    ):
        raise ValueError("corporate-action candidate snapshot is invalid")
    expected_names = {
        "manifest.json",
        "raw_candidates.parquet",
        "query_coverage.parquet",
        "candidates.parquet",
    }
    if set(files) != expected_names:
        raise ValueError("corporate-action candidate snapshot inventory is invalid")
    for record in records:
        if (
            not isinstance(record, dict)
            or record.get("path") not in expected_names - {"manifest.json"}
            or record.get("sha256")
            != hashlib.sha256(files[str(record.get("path"))]).hexdigest()
            or record.get("size_bytes") != len(files[str(record.get("path"))])
        ):
            raise ValueError("corporate-action candidate snapshot file changed")
    try:
        candidates = pl.read_parquet(BytesIO(files["candidates.parquet"]))
    except pl.exceptions.PolarsError as error:
        raise ValueError("corporate-action candidate snapshot is invalid") from error
    required = {"candidate_id", *CANDIDATE_FIELDS}
    if (
        not required.issubset(candidates.columns)
        or candidates.height != manifest.get("candidate_count")
        or candidates.select(pl.col("candidate_id").is_duplicated().any()).item()
    ):
        raise ValueError("corporate-action candidate rows are invalid")
    return VerifiedCandidateSnapshot(
        manifest_path=absolute,
        manifest_sha256=hashlib.sha256(files["manifest.json"]).hexdigest(),
        manifest=manifest,
        candidates=candidates.sort("candidate_id"),
    )


def _validate_submission_manifest(
    manifest: dict[str, object],
    *,
    files: dict[str, bytes],
    queue: VerifiedReviewQueue,
    candidates: VerifiedCandidateSnapshot,
    attempt_id: str,
) -> None:
    records = manifest.get("files")
    if (
        manifest.get("schema_version") != _SCHEMA_VERSION
        or manifest.get("role") != "official_evidence_review_submission"
        or manifest.get("status") != "ready"
        or manifest.get("attempt_id") != attempt_id
        or manifest.get("review_session_id") != queue.session_id
        or manifest.get("review_manifest_sha256") != queue.manifest_sha256
        or manifest.get("candidate_manifest_sha256") != candidates.manifest_sha256
        or not isinstance(records, list)
        or not isinstance(manifest.get("reviewer_id"), str)
        or not isinstance(manifest.get("reviewed_at"), str)
    ):
        raise ValueError("official review submission identity differs")
    record_by_path = {
        str(record.get("path")): record
        for record in records
        if isinstance(record, dict)
    }
    data_names = {
        ANNOUNCEMENT_DECISIONS_NAME,
        CORPORATE_DISPOSITIONS_NAME,
        CORPORATE_FACTS_NAME,
        SECURITY_FACTS_NAME,
    }
    if set(record_by_path) != data_names:
        raise ValueError("official review submission file records are invalid")
    for name in data_names:
        record = record_by_path[name]
        if (
            record.get("sha256") != hashlib.sha256(files[name]).hexdigest()
            or record.get("size_bytes") != len(files[name])
            or not isinstance(record.get("row_count"), int)
        ):
            raise ValueError("official review submission file changed")
    _review_metadata(
        str(manifest["reviewer_id"]),
        str(manifest["reviewed_at"]),
    )


def _review_metadata(reviewer_id: str, reviewed_at: str) -> tuple[str, str]:
    if (
        not isinstance(reviewer_id, str)
        or not reviewer_id.strip()
        or reviewer_id != reviewer_id.strip()
    ):
        raise ValueError("reviewer identity is invalid")
    try:
        timestamp = datetime.fromisoformat(reviewed_at)
    except (TypeError, ValueError) as error:
        raise ValueError("review timestamp is invalid") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("review timestamp must include a timezone")
    return reviewer_id, timestamp.isoformat()


def _parquet_bytes(frame: pl.DataFrame) -> bytes:
    stream = BytesIO()
    frame.write_parquet(stream, compression="zstd")
    return stream.getvalue()


def _canonical_object(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is invalid") from error
    if not isinstance(value, dict) or raw != canonical_json_bytes(value):
        raise ValueError(f"{label} is invalid")
    return value


def _open_or_create_directory(parent_fd: int, name: str, *, label: str) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    descriptor = open_directory_at(parent_fd, name, label=label)
    for entry in os.listdir(descriptor):
        metadata = os.stat(entry, dir_fd=descriptor, follow_symlinks=False)
        if _SHA256.fullmatch(entry) is None or not stat.S_ISDIR(metadata.st_mode):
            os.close(descriptor)
            raise ValueError(f"{label} inventory is invalid")
    return descriptor


def _assert_safe_directory(path: Path, *, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label} is missing or unsafe")
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise ValueError(f"{label} uses a symlink")
