"""Immutable, symbol-sharded inputs for resumable official-evidence review."""

from __future__ import annotations

from dataclasses import dataclass
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
from ashare_multifactor.final_test.official_review_submission import (
    VerifiedCandidateSnapshot,
    load_verified_candidate_snapshot,
)
from ashare_multifactor.final_test.recovery_secure_fs import open_directory_at


PLANS_DIRECTORY = "official_review_batch_plans"
PLAN_MANIFEST_NAME = "review_batch_plan.json"
BATCH_MANIFEST_NAME = "review_batch_manifest.json"
BATCH_QUEUE_NAME = "review_queue.parquet"
BATCH_CANDIDATES_NAME = "corporate_action_candidates.parquet"

_SCHEMA_VERSION = "1"
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ReviewBatch:
    """One immutable symbol shard awaiting exactly one reviewed submission."""

    batch_id: str
    root: Path
    manifest_path: Path
    queue_path: Path
    candidates_path: Path
    symbols: tuple[str, ...]
    queue_row_count: int
    candidate_row_count: int


@dataclass(frozen=True)
class VerifiedReviewBatchWorkspace:
    """One deterministic review plan bound to queue and candidate identities."""

    root: Path
    manifest_path: Path
    manifest_sha256: str
    plan_id: str
    symbols_per_batch: int
    batches: tuple[ReviewBatch, ...]


def prepare_review_batch_workspace(
    *,
    workspace: EvidenceWorkspace,
    candidate_manifest_path: Path | None,
    symbols_per_batch: int,
) -> VerifiedReviewBatchWorkspace:
    """Partition review inputs by symbol and freeze a resumable batch plan."""
    size = _positive_int(symbols_per_batch)
    queue = load_verified_review_queue(workspace)
    candidates = load_verified_candidate_snapshot(
        candidate_manifest_path,
        workspace=workspace,
    )
    files, manifest_bytes = _plan_files(
        workspace=workspace,
        queue=queue,
        candidates=candidates,
        symbols_per_batch=size,
    )
    plan_id = hashlib.sha256(manifest_bytes).hexdigest()
    destination = workspace.root.parent
    with opened_safe_directory(
        destination,
        label="official evidence destination",
    ) as root_fd:
        plans_fd = _open_or_create_hash_directory(
            root_fd,
            PLANS_DIRECTORY,
            label="official review batch plans",
        )
        try:
            write_frozen_tree_at(
                plans_fd,
                plan_id,
                files,
                resumable=True,
                label="official review batch workspace",
            )
        finally:
            os.close(plans_fd)
    return load_verified_review_batch_workspace(
        destination / PLANS_DIRECTORY / plan_id / PLAN_MANIFEST_NAME,
        workspace=workspace,
        candidate_manifest_path=candidate_manifest_path,
    )


def load_verified_review_batch_workspace(
    manifest_path: Path,
    *,
    workspace: EvidenceWorkspace,
    candidate_manifest_path: Path | None,
) -> VerifiedReviewBatchWorkspace:
    """Reload a plan only when every shard still matches current frozen inputs."""
    absolute = manifest_path.absolute()
    root = absolute.parent
    expected_parent = workspace.root.parent / PLANS_DIRECTORY
    if (
        absolute.name != PLAN_MANIFEST_NAME
        or root.parent.absolute() != expected_parent.absolute()
        or _SHA256.fullmatch(root.name) is None
    ):
        raise ValueError("official review batch workspace path is not canonical")
    _assert_safe_directory(root, label="official review batch workspace")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        actual = read_frozen_tree_at(
            descriptor,
            label="official review batch workspace",
        )
    finally:
        os.close(descriptor)
    manifest_bytes = actual.get(PLAN_MANIFEST_NAME, b"")
    manifest = _canonical_object(
        manifest_bytes,
        label="official review batch plan",
    )
    if hashlib.sha256(manifest_bytes).hexdigest() != root.name:
        raise ValueError("official review batch workspace identity changed")
    queue = load_verified_review_queue(workspace)
    candidates = load_verified_candidate_snapshot(
        candidate_manifest_path,
        workspace=workspace,
    )
    size = _positive_int(manifest.get("symbols_per_batch"))
    expected, expected_manifest = _plan_files(
        workspace=workspace,
        queue=queue,
        candidates=candidates,
        symbols_per_batch=size,
    )
    if actual != expected or manifest_bytes != expected_manifest:
        raise ValueError("official review batch workspace differs from frozen inputs")
    records = manifest.get("batches")
    if not isinstance(records, list):
        raise ValueError("official review batch workspace inventory is invalid")
    batches = tuple(
        _batch_from_record(root, record)
        for record in records
    )
    return VerifiedReviewBatchWorkspace(
        root=root,
        manifest_path=absolute,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        plan_id=root.name,
        symbols_per_batch=size,
        batches=batches,
    )


def _plan_files(
    *,
    workspace: EvidenceWorkspace,
    queue: VerifiedReviewQueue,
    candidates: VerifiedCandidateSnapshot,
    symbols_per_batch: int,
) -> tuple[dict[str, bytes], bytes]:
    symbols = sorted(
        set(queue.frame.get_column("symbol").cast(pl.String).to_list())
        | set(candidates.candidates.get_column("symbol").cast(pl.String).to_list())
    )
    groups = [
        tuple(symbols[index : index + symbols_per_batch])
        for index in range(0, len(symbols), symbols_per_batch)
    ] or [()]
    files: dict[str, bytes] = {}
    records: list[dict[str, object]] = []
    for ordinal, group in enumerate(groups, start=1):
        queue_frame = queue.frame.filter(pl.col("symbol").is_in(group))
        candidate_frame = candidates.candidates.filter(
            pl.col("symbol").is_in(group)
        )
        queue_bytes = _parquet_bytes(queue_frame)
        candidate_bytes = _parquet_bytes(candidate_frame)
        batch_manifest = {
            "schema_version": _SCHEMA_VERSION,
            "role": "official_review_batch_input",
            "status": "awaiting_review",
            "attempt_id": workspace.root.parent.name,
            "review_session_id": queue.session_id,
            "review_manifest_sha256": queue.manifest_sha256,
            "candidate_manifest_sha256": candidates.manifest_sha256,
            "ordinal": ordinal,
            "symbols": list(group),
            "files": [
                _file_record(
                    BATCH_QUEUE_NAME,
                    queue_bytes,
                    row_count=queue_frame.height,
                ),
                _file_record(
                    BATCH_CANDIDATES_NAME,
                    candidate_bytes,
                    row_count=candidate_frame.height,
                ),
            ],
        }
        batch_manifest_bytes = canonical_json_bytes(batch_manifest)
        batch_id = hashlib.sha256(batch_manifest_bytes).hexdigest()
        prefix = f"batches/{batch_id}"
        files[f"{prefix}/{BATCH_MANIFEST_NAME}"] = batch_manifest_bytes
        files[f"{prefix}/{BATCH_QUEUE_NAME}"] = queue_bytes
        files[f"{prefix}/{BATCH_CANDIDATES_NAME}"] = candidate_bytes
        records.append(
            {
                "batch_id": batch_id,
                "ordinal": ordinal,
                "symbols": list(group),
                "queue_row_count": queue_frame.height,
                "candidate_row_count": candidate_frame.height,
            }
        )
    plan = {
        "schema_version": _SCHEMA_VERSION,
        "role": "official_review_batch_plan",
        "status": "awaiting_review",
        "attempt_id": workspace.root.parent.name,
        "review_session_id": queue.session_id,
        "review_manifest_sha256": queue.manifest_sha256,
        "candidate_manifest_sha256": candidates.manifest_sha256,
        "symbols_per_batch": symbols_per_batch,
        "symbol_count": len(symbols),
        "queue_row_count": queue.frame.height,
        "candidate_row_count": candidates.candidates.height,
        "batch_count": len(records),
        "batches": records,
    }
    manifest_bytes = canonical_json_bytes(plan)
    files[PLAN_MANIFEST_NAME] = manifest_bytes
    return files, manifest_bytes


def _batch_from_record(root: Path, record: object) -> ReviewBatch:
    if (
        not isinstance(record, dict)
        or _SHA256.fullmatch(str(record.get("batch_id", ""))) is None
        or not isinstance(record.get("ordinal"), int)
        or not isinstance(record.get("symbols"), list)
        or any(
            not isinstance(symbol, str) or len(symbol) != 6
            for symbol in record["symbols"]
        )
        or not isinstance(record.get("queue_row_count"), int)
        or not isinstance(record.get("candidate_row_count"), int)
    ):
        raise ValueError("official review batch workspace inventory is invalid")
    batch_id = str(record["batch_id"])
    batch_root = root / "batches" / batch_id
    return ReviewBatch(
        batch_id=batch_id,
        root=batch_root,
        manifest_path=batch_root / BATCH_MANIFEST_NAME,
        queue_path=batch_root / BATCH_QUEUE_NAME,
        candidates_path=batch_root / BATCH_CANDIDATES_NAME,
        symbols=tuple(record["symbols"]),
        queue_row_count=int(record["queue_row_count"]),
        candidate_row_count=int(record["candidate_row_count"]),
    )


def _file_record(
    name: str,
    payload: bytes,
    *,
    row_count: int,
) -> dict[str, object]:
    return {
        "path": name,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "row_count": row_count,
    }


def _positive_int(value: object) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
    ):
        raise ValueError("review batch symbol count must be positive")
    return value


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
