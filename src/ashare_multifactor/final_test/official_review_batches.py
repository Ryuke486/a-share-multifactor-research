"""Immutable reviewed shards that merge into one exact review submission."""

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
from ashare_multifactor.final_test.official_evidence_workspace import (
    EvidenceWorkspace,
    VerifiedReviewQueue,
    load_verified_review_queue,
)
from ashare_multifactor.final_test.official_query_collection_root import (
    opened_safe_directory,
)
from ashare_multifactor.final_test.official_query_coverage import canonical_json_bytes
from ashare_multifactor.final_test.official_review_batch_workspace import (
    PLAN_MANIFEST_NAME,
    PLANS_DIRECTORY,
    ReviewBatch,
    VerifiedReviewBatchWorkspace,
    load_verified_review_batch_workspace,
)
from ashare_multifactor.final_test.official_review_contract import (
    validate_review_frames,
)
from ashare_multifactor.final_test.official_review_submission import (
    ANNOUNCEMENT_DECISIONS_NAME,
    CORPORATE_DISPOSITIONS_NAME,
    CORPORATE_FACTS_NAME,
    SECURITY_FACTS_NAME,
    VerifiedReviewSubmission,
    load_verified_review_submission,
    publish_review_submission,
)
from ashare_multifactor.final_test.recovery_secure_fs import open_directory_at


SUBMISSIONS_DIRECTORY = "official_review_batch_submissions"
BATCH_SUBMISSION_MANIFEST_NAME = "review_batch_submission.json"

_SCHEMA_VERSION = "1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DATA_NAMES = (
    ANNOUNCEMENT_DECISIONS_NAME,
    CORPORATE_DISPOSITIONS_NAME,
    CORPORATE_FACTS_NAME,
    SECURITY_FACTS_NAME,
)


@dataclass(frozen=True, eq=False)
class VerifiedReviewBatch:
    """One complete immutable review shard."""

    batch_id: str
    root: Path
    manifest_path: Path
    manifest_sha256: str
    reviewer_id: str
    reviewed_at: str
    announcement_decisions: pl.DataFrame
    corporate_action_dispositions: pl.DataFrame
    corporate_action_facts: pl.DataFrame
    security_event_facts: pl.DataFrame

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, VerifiedReviewBatch):
            return NotImplemented
        return (
            self.batch_id == other.batch_id
            and self.root == other.root
            and self.manifest_path == other.manifest_path
            and self.manifest_sha256 == other.manifest_sha256
            and self.reviewer_id == other.reviewer_id
            and self.reviewed_at == other.reviewed_at
            and self.announcement_decisions.equals(other.announcement_decisions)
            and self.corporate_action_dispositions.equals(
                other.corporate_action_dispositions
            )
            and self.corporate_action_facts.equals(other.corporate_action_facts)
            and self.security_event_facts.equals(other.security_event_facts)
        )


def publish_review_batch(
    *,
    workspace: EvidenceWorkspace,
    candidate_manifest_path: Path | None,
    batch_manifest_path: Path,
    announcement_decisions: pl.DataFrame,
    corporate_action_dispositions: pl.DataFrame,
    corporate_action_facts: pl.DataFrame,
    security_event_facts: pl.DataFrame,
    reviewer_id: str,
    reviewed_at: str,
) -> VerifiedReviewBatch:
    """Validate and freeze one exact symbol shard without requiring later shards."""
    plan, batch = _load_plan_for_batch(
        batch_manifest_path,
        workspace=workspace,
        candidate_manifest_path=candidate_manifest_path,
    )
    queue = load_verified_review_queue(workspace)
    reviewer, timestamp, _reviewed = _review_metadata(reviewer_id, reviewed_at)
    frames = _validated_batch_frames(
        queue,
        batch,
        announcement_decisions=announcement_decisions,
        corporate_action_dispositions=corporate_action_dispositions,
        corporate_action_facts=corporate_action_facts,
        security_event_facts=security_event_facts,
    )
    data = {
        name: _parquet_bytes(frame)
        for name, frame in zip(_DATA_NAMES, frames, strict=True)
    }
    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "role": "official_review_batch_submission",
        "status": "complete",
        "attempt_id": workspace.root.parent.name,
        "plan_id": plan.plan_id,
        "plan_manifest_sha256": plan.manifest_sha256,
        "batch_id": batch.batch_id,
        "batch_manifest_sha256": batch.batch_id,
        "reviewer_id": reviewer,
        "reviewed_at": timestamp,
        "files": [
            {
                "path": name,
                "sha256": hashlib.sha256(data[name]).hexdigest(),
                "size_bytes": len(data[name]),
                "row_count": frame.height,
            }
            for name, frame in zip(_DATA_NAMES, frames, strict=True)
        ],
    }
    manifest_bytes = canonical_json_bytes(manifest)
    destination = workspace.root.parent
    with opened_safe_directory(
        destination,
        label="official evidence destination",
    ) as root_fd:
        submissions_fd = _open_or_create_hash_directory(
            root_fd,
            SUBMISSIONS_DIRECTORY,
            label="official review batch submissions",
        )
        try:
            plan_fd = _open_or_create_hash_directory(
                submissions_fd,
                plan.plan_id,
                label="official review plan submissions",
            )
            try:
                try:
                    write_frozen_tree_at(
                        plan_fd,
                        batch.batch_id,
                        {
                            **data,
                            BATCH_SUBMISSION_MANIFEST_NAME: manifest_bytes,
                        },
                        resumable=True,
                        label="official review batch submission",
                    )
                except (FileExistsError, ValueError) as error:
                    raise ValueError(
                        "review batch decisions differ from immutable slot"
                    ) from error
            finally:
                os.close(plan_fd)
        finally:
            os.close(submissions_fd)
    return _load_verified_review_batch_bound(
        _submission_manifest_path(destination, plan, batch),
        workspace=workspace,
        plan=plan,
        batch=batch,
        queue=queue,
    )


def load_verified_review_batch(
    manifest_path: Path,
    *,
    workspace: EvidenceWorkspace,
    candidate_manifest_path: Path | None,
    batch_manifest_path: Path,
) -> VerifiedReviewBatch:
    """Reload one reviewed shard and repeat its complete semantic validation."""
    plan, batch = _load_plan_for_batch(
        batch_manifest_path,
        workspace=workspace,
        candidate_manifest_path=candidate_manifest_path,
    )
    return _load_verified_review_batch_bound(
        manifest_path,
        workspace=workspace,
        plan=plan,
        batch=batch,
        queue=load_verified_review_queue(workspace),
    )


def _load_verified_review_batch_bound(
    manifest_path: Path,
    *,
    workspace: EvidenceWorkspace,
    plan: VerifiedReviewBatchWorkspace,
    batch: ReviewBatch,
    queue: VerifiedReviewQueue,
) -> VerifiedReviewBatch:
    absolute = manifest_path.absolute()
    expected = _submission_manifest_path(workspace.root.parent, plan, batch)
    if absolute != expected.absolute():
        raise ValueError("official review batch submission path is not canonical")
    root = absolute.parent
    _assert_safe_directory(root, label="official review batch submission")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        files = read_frozen_tree_at(
            descriptor,
            label="official review batch submission",
        )
    finally:
        os.close(descriptor)
    if set(files) != {*_DATA_NAMES, BATCH_SUBMISSION_MANIFEST_NAME}:
        raise ValueError("official review batch submission inventory is invalid")
    manifest_bytes = files[BATCH_SUBMISSION_MANIFEST_NAME]
    manifest = _canonical_object(
        manifest_bytes,
        label="official review batch submission manifest",
    )
    raw_frames = _read_frames(files)
    frames = _validated_batch_frames(
        queue,
        batch,
        announcement_decisions=raw_frames[0],
        corporate_action_dispositions=raw_frames[1],
        corporate_action_facts=raw_frames[2],
        security_event_facts=raw_frames[3],
    )
    if any(
        not actual.equals(canonical)
        for actual, canonical in zip(raw_frames, frames, strict=True)
    ):
        raise ValueError("official review batch rows are not canonical")
    reviewer, timestamp, _reviewed = _review_metadata(
        manifest.get("reviewer_id"),
        manifest.get("reviewed_at"),
    )
    _validate_manifest(
        manifest,
        files=files,
        frames=frames,
        workspace=workspace,
        plan=plan,
        batch=batch,
    )
    return VerifiedReviewBatch(
        batch_id=batch.batch_id,
        root=root,
        manifest_path=absolute,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        reviewer_id=reviewer,
        reviewed_at=timestamp,
        announcement_decisions=frames[0],
        corporate_action_dispositions=frames[1],
        corporate_action_facts=frames[2],
        security_event_facts=frames[3],
    )


def finalize_review_batches(
    *,
    workspace: EvidenceWorkspace,
    candidate_manifest_path: Path | None,
    batch_workspace_manifest_path: Path,
    reviewer_id: str,
    reviewed_at: str,
) -> VerifiedReviewSubmission:
    """Require every immutable shard, then run the unchanged full-review gate."""
    plan = load_verified_review_batch_workspace(
        batch_workspace_manifest_path,
        workspace=workspace,
        candidate_manifest_path=candidate_manifest_path,
    )
    reviewer, timestamp, finalized_at = _review_metadata(reviewer_id, reviewed_at)
    queue = load_verified_review_queue(workspace)
    reviewed: list[VerifiedReviewBatch] = []
    for batch in plan.batches:
        manifest_path = _submission_manifest_path(
            workspace.root.parent,
            plan,
            batch,
        )
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise ValueError("review batches are incomplete")
        reviewed.append(
            _load_verified_review_batch_bound(
                manifest_path,
                workspace=workspace,
                plan=plan,
                batch=batch,
                queue=queue,
            )
        )
    if any(
        item.reviewer_id != reviewer
        or datetime.fromisoformat(item.reviewed_at) > finalized_at
        for item in reviewed
    ):
        raise ValueError("review batch reviewer or timestamp differs")
    combined = _combined_batch_frames(reviewed)
    submission = publish_review_submission(
        workspace=workspace,
        candidate_manifest_path=candidate_manifest_path,
        announcement_decisions=combined[0],
        corporate_action_dispositions=combined[1],
        corporate_action_facts=combined[2],
        security_event_facts=combined[3],
        reviewer_id=reviewer,
        reviewed_at=timestamp,
        batch_provenance=_batch_provenance(plan, reviewed),
    )
    return load_verified_batch_review_submission(
        submission.manifest_path,
        workspace=workspace,
        candidate_manifest_path=candidate_manifest_path,
    )


def load_verified_batch_review_submission(
    manifest_path: Path,
    *,
    workspace: EvidenceWorkspace,
    candidate_manifest_path: Path | None,
) -> VerifiedReviewSubmission:
    """Require one final submission to derive from every immutable review shard."""
    submission = load_verified_review_submission(
        manifest_path,
        workspace=workspace,
        candidate_manifest_path=candidate_manifest_path,
    )
    provenance = submission.batch_provenance
    if not isinstance(provenance, dict):
        raise ValueError("review batch provenance is required")
    plan_id = provenance.get("plan_id")
    if not isinstance(plan_id, str) or _SHA256.fullmatch(plan_id) is None:
        raise ValueError("review batch provenance is invalid")
    plan = load_verified_review_batch_workspace(
        workspace.root.parent
        / PLANS_DIRECTORY
        / plan_id
        / PLAN_MANIFEST_NAME,
        workspace=workspace,
        candidate_manifest_path=candidate_manifest_path,
    )
    queue = load_verified_review_queue(workspace)
    reviewed: list[VerifiedReviewBatch] = []
    for batch in plan.batches:
        try:
            reviewed.append(
                _load_verified_review_batch_bound(
                    _submission_manifest_path(
                        workspace.root.parent,
                        plan,
                        batch,
                    ),
                    workspace=workspace,
                    plan=plan,
                    batch=batch,
                    queue=queue,
                )
            )
        except (FileNotFoundError, ValueError) as error:
            raise ValueError("review batch provenance is incomplete") from error
    if provenance != _batch_provenance(plan, reviewed):
        raise ValueError("review batch provenance differs")
    finalized_at = datetime.fromisoformat(submission.reviewed_at)
    if any(
        item.reviewer_id != submission.reviewer_id
        or datetime.fromisoformat(item.reviewed_at) > finalized_at
        for item in reviewed
    ):
        raise ValueError("review batch provenance reviewer or timestamp differs")
    combined = _combined_batch_frames(reviewed)
    submitted = (
        submission.announcement_decisions,
        submission.corporate_action_dispositions,
        submission.corporate_action_facts,
        submission.security_event_facts,
    )
    if any(
        not actual.equals(expected)
        for actual, expected in zip(submitted, combined, strict=True)
    ):
        raise ValueError("review batch provenance rows differ")
    return submission


def _batch_provenance(
    plan: VerifiedReviewBatchWorkspace,
    reviewed: list[VerifiedReviewBatch],
) -> dict[str, object]:
    return {
        "role": "complete_official_review_batch_provenance",
        "plan_id": plan.plan_id,
        "plan_manifest_sha256": plan.manifest_sha256,
        "batches": [
            {
                "batch_id": item.batch_id,
                "submission_manifest_sha256": item.manifest_sha256,
            }
            for item in reviewed
        ],
    }


def _combined_batch_frames(
    reviewed: list[VerifiedReviewBatch],
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    return tuple(
        pl.concat(
            [getattr(item, attribute) for item in reviewed],
            how="vertical_relaxed",
        )
        for attribute in (
            "announcement_decisions",
            "corporate_action_dispositions",
            "corporate_action_facts",
            "security_event_facts",
        )
    )


def _load_plan_for_batch(
    batch_manifest_path: Path,
    *,
    workspace: EvidenceWorkspace,
    candidate_manifest_path: Path | None,
) -> tuple[VerifiedReviewBatchWorkspace, ReviewBatch]:
    absolute = batch_manifest_path.absolute()
    try:
        plan_manifest = absolute.parents[2] / PLAN_MANIFEST_NAME
    except IndexError as error:
        raise ValueError("review batch manifest path is not canonical") from error
    plan = load_verified_review_batch_workspace(
        plan_manifest,
        workspace=workspace,
        candidate_manifest_path=candidate_manifest_path,
    )
    matches = [
        batch
        for batch in plan.batches
        if batch.manifest_path.absolute() == absolute
    ]
    if len(matches) != 1:
        raise ValueError("review batch manifest path is not canonical")
    return plan, matches[0]


def _validated_batch_frames(
    queue: VerifiedReviewQueue,
    batch: ReviewBatch,
    *,
    announcement_decisions: pl.DataFrame,
    corporate_action_dispositions: pl.DataFrame,
    corporate_action_facts: pl.DataFrame,
    security_event_facts: pl.DataFrame,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    batch_queue = VerifiedReviewQueue(
        frame=pl.read_parquet(batch.queue_path),
        manifest=queue.manifest,
        manifest_path=queue.manifest_path,
        manifest_sha256=queue.manifest_sha256,
        session_id=queue.session_id,
    )
    candidates = pl.read_parquet(batch.candidates_path)
    return validate_review_frames(
        batch_queue,
        candidates,
        announcement_decisions=announcement_decisions,
        corporate_action_dispositions=corporate_action_dispositions,
        corporate_action_facts=corporate_action_facts,
        security_event_facts=security_event_facts,
    )


def _validate_manifest(
    manifest: dict[str, object],
    *,
    files: dict[str, bytes],
    frames: tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame],
    workspace: EvidenceWorkspace,
    plan: VerifiedReviewBatchWorkspace,
    batch: ReviewBatch,
) -> None:
    records = manifest.get("files")
    if (
        manifest.get("schema_version") != _SCHEMA_VERSION
        or manifest.get("role") != "official_review_batch_submission"
        or manifest.get("status") != "complete"
        or manifest.get("attempt_id") != workspace.root.parent.name
        or manifest.get("plan_id") != plan.plan_id
        or manifest.get("plan_manifest_sha256") != plan.manifest_sha256
        or manifest.get("batch_id") != batch.batch_id
        or manifest.get("batch_manifest_sha256") != batch.batch_id
        or not isinstance(records, list)
    ):
        raise ValueError("official review batch submission identity differs")
    expected_records = [
        {
            "path": name,
            "sha256": hashlib.sha256(files[name]).hexdigest(),
            "size_bytes": len(files[name]),
            "row_count": frame.height,
        }
        for name, frame in zip(_DATA_NAMES, frames, strict=True)
    ]
    if records != expected_records:
        raise ValueError("official review batch submission files changed")


def _read_frames(
    files: dict[str, bytes],
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    try:
        return tuple(
            pl.read_parquet(BytesIO(files[name]))
            for name in _DATA_NAMES
        )
    except pl.exceptions.PolarsError as error:
        raise ValueError("official review batch parquet is invalid") from error


def _submission_manifest_path(
    destination: Path,
    plan: VerifiedReviewBatchWorkspace,
    batch: ReviewBatch,
) -> Path:
    return (
        destination
        / SUBMISSIONS_DIRECTORY
        / plan.plan_id
        / batch.batch_id
        / BATCH_SUBMISSION_MANIFEST_NAME
    )


def _review_metadata(
    reviewer_id: object,
    reviewed_at: object,
) -> tuple[str, str, datetime]:
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
    return reviewer_id, timestamp.isoformat(), timestamp


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


def _open_or_create_hash_directory(
    parent_fd: int,
    name: str,
    *,
    label: str,
) -> int:
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
