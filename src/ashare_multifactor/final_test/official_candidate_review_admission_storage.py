"""Immutable storage for candidate review admission decisions."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re

import polars as pl

from ashare_multifactor.audit.secure_tree import (
    read_frozen_tree_at,
    write_frozen_tree_at,
)
from ashare_multifactor.final_test.official_query_collection_root import (
    opened_safe_directory,
)
from ashare_multifactor.final_test.official_query_coverage import (
    canonical_json_bytes,
)


ADMISSION_DIRECTORY = "candidate_review_admissions"
ADMISSION_MANIFEST_NAME = "candidate_review_admission.json"
UNRESOLVED_NAME = "unresolved_candidates.parquet"
DECISIONS_NAME = "candidate_admission_decisions.parquet"
_SCHEMA_VERSION = "1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_UNRESOLVED_SCHEMA = {
    "candidate_id": pl.String,
    "symbol": pl.String,
    "ex_date": pl.Date,
    "effective_date": pl.Date,
    "failure_codes": pl.List(pl.String),
    "same_symbol_announcement_count": pl.Int64,
    "strong_implementation_announcement_count": pl.Int64,
    "valid_cached_official_pdf_count": pl.Int64,
    "frozen_date_window_hit_count": pl.Int64,
    "related_catalog_ids": pl.List(pl.String),
}


@dataclass(frozen=True)
class VerifiedCandidateReviewAdmission:
    """One deterministic candidate-to-official-document closure decision."""

    root: Path
    manifest_path: Path
    unresolved_path: Path
    manifest_sha256: str
    manifest: dict[str, object]
    unresolved: pl.DataFrame
    ready: bool
    decisions_path: Path | None = None


class CandidateReviewAdmissionError(ValueError):
    """A precise blocked admission that was published before failing closed."""

    def __init__(self, admission: VerifiedCandidateReviewAdmission) -> None:
        self.manifest_path = admission.manifest_path
        self.manifest_sha256 = admission.manifest_sha256
        self.unresolved_count = admission.unresolved.height
        super().__init__(
            "candidate review admission blocked: "
            f"unresolved={self.unresolved_count} "
            f"manifest={self.manifest_path} "
            f"sha256={self.manifest_sha256}"
        )


def publish_candidate_review_admission(
    destination: Path,
    *,
    manifest: dict[str, object],
    unresolved_bytes: bytes,
    decisions_bytes: bytes | None = None,
) -> VerifiedCandidateReviewAdmission:
    """Write one content-addressed admission tree and verify it from disk."""
    manifest_bytes = canonical_json_bytes(manifest)
    admission_id = hashlib.sha256(manifest_bytes).hexdigest()
    files = {
        ADMISSION_MANIFEST_NAME: manifest_bytes,
        UNRESOLVED_NAME: unresolved_bytes,
    }
    if decisions_bytes is not None:
        files[DECISIONS_NAME] = decisions_bytes
    with opened_safe_directory(
        destination,
        label="official evidence destination",
    ) as root_fd:
        try:
            os.mkdir(ADMISSION_DIRECTORY, mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        admissions_fd = os.open(
            ADMISSION_DIRECTORY,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        try:
            write_frozen_tree_at(
                admissions_fd,
                admission_id,
                files,
                resumable=True,
                label="candidate review admission",
            )
        finally:
            os.close(admissions_fd)
    return load_published_candidate_review_admission(
        destination,
        admission_id=admission_id,
    )


def load_published_candidate_review_admission(
    destination: Path,
    *,
    admission_id: str,
) -> VerifiedCandidateReviewAdmission:
    """Read one content-addressed admission tree and verify every binding."""
    if _SHA256.fullmatch(admission_id) is None:
        raise ValueError("candidate review admission identity is invalid")
    root = destination / ADMISSION_DIRECTORY / admission_id
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        files = read_frozen_tree_at(
            descriptor,
            label="candidate review admission",
        )
    finally:
        os.close(descriptor)
    if not {ADMISSION_MANIFEST_NAME, UNRESOLVED_NAME} <= set(files):
        raise ValueError("candidate review admission inventory is invalid")
    manifest_bytes = files[ADMISSION_MANIFEST_NAME]
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        unresolved = pl.read_parquet(BytesIO(files[UNRESOLVED_NAME]))
        decisions = (
            pl.read_parquet(BytesIO(files[DECISIONS_NAME]))
            if DECISIONS_NAME in files
            else None
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        pl.exceptions.PolarsError,
    ) as error:
        raise ValueError("candidate review admission is invalid") from error
    record = (
        manifest.get("unresolved_candidates")
        if isinstance(manifest, dict)
        else None
    )
    decisions_record = (
        manifest.get("candidate_decisions")
        if isinstance(manifest, dict)
        else None
    )
    expected_names = {ADMISSION_MANIFEST_NAME, UNRESOLVED_NAME}
    if isinstance(decisions_record, dict):
        expected_names.add(DECISIONS_NAME)
    if (
        set(files) != expected_names
        or not isinstance(manifest, dict)
        or manifest_bytes != canonical_json_bytes(manifest)
        or hashlib.sha256(manifest_bytes).hexdigest() != admission_id
        or manifest.get("schema_version") != _SCHEMA_VERSION
        or manifest.get("role") != "candidate_review_admission"
        or manifest.get("status") not in {"ready", "blocked"}
        or not isinstance(record, dict)
        or record.get("path") != UNRESOLVED_NAME
        or record.get("sha256")
        != hashlib.sha256(files[UNRESOLVED_NAME]).hexdigest()
        or record.get("size_bytes") != len(files[UNRESOLVED_NAME])
        or record.get("row_count") != unresolved.height
        or manifest.get("unresolved_count") != unresolved.height
        or (manifest.get("status") == "ready") != unresolved.is_empty()
        or unresolved.schema != _UNRESOLVED_SCHEMA
        or not unresolved.equals(unresolved.sort("candidate_id"))
        or (
            isinstance(decisions_record, dict)
            and (
                decisions is None
                or decisions_record.get("path") != DECISIONS_NAME
                or decisions_record.get("sha256")
                != hashlib.sha256(files[DECISIONS_NAME]).hexdigest()
                or decisions_record.get("size_bytes")
                != len(files[DECISIONS_NAME])
                or decisions_record.get("row_count") != decisions.height
            )
        )
    ):
        raise ValueError("candidate review admission identity differs")
    return VerifiedCandidateReviewAdmission(
        root=root,
        manifest_path=root / ADMISSION_MANIFEST_NAME,
        unresolved_path=root / UNRESOLVED_NAME,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        manifest=manifest,
        unresolved=unresolved,
        ready=manifest["status"] == "ready",
        decisions_path=(
            root / DECISIONS_NAME
            if isinstance(decisions_record, dict)
            else None
        ),
    )


def parquet_bytes(frame: pl.DataFrame) -> bytes:
    """Encode one deterministic zstd parquet payload."""
    stream = BytesIO()
    frame.write_parquet(stream, compression="zstd")
    return stream.getvalue()
