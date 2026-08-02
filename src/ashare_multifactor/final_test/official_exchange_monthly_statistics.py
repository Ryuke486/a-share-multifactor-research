"""Immutable SZSE monthly-statistics evidence and review-session binding."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re

from ashare_multifactor.audit.secure_tree import (
    read_frozen_tree_at,
    write_frozen_tree_at,
)
from ashare_multifactor.final_test.official_candidate_query_supplement_basis import (
    file_binding,
)
from ashare_multifactor.final_test.official_evidence_workspace import (
    EvidenceWorkspace,
    REVIEW_MANIFEST_NAME,
    REVIEW_QUEUE_NAME,
    REVIEW_SESSIONS_DIRECTORY,
    VerifiedReviewQueue,
    load_verified_review_queue,
)
from ashare_multifactor.final_test.official_exchange_monthly_statistics_authorization import (
    load_exchange_monthly_statistics_authorization,
)
from ashare_multifactor.final_test.official_exchange_monthly_statistics_validation import (
    build_verified_statistics_record,
    validate_verified_statistics_record,
)
from ashare_multifactor.final_test.official_query_collection_root import (
    opened_safe_directory,
)
from ashare_multifactor.final_test.official_query_coverage import canonical_json_bytes


STATISTICS_DIRECTORY = "official_exchange_monthly_statistics"
STATISTICS_MANIFEST_NAME = "statistics_manifest.json"

_SCHEMA_VERSION = "1"
_REVIEW_SCHEMA_VERSION = "4"
_IMMUTABLE_ID = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ExchangeMonthlyStatisticsInput:
    """Exact locally cached outputs of the authorized GET."""

    source_url: str
    document_path: Path
    receipt_path: Path


@dataclass(frozen=True)
class VerifiedExchangeMonthlyStatistics:
    """One replayed record anchored to a base review queue."""

    root: Path
    manifest_path: Path
    manifest_sha256: str
    manifest: dict[str, object]
    record: dict[str, object]


def publish_exchange_monthly_statistics(
    *,
    workspace: EvidenceWorkspace,
    queue: VerifiedReviewQueue,
    network_authorization_path: Path,
    statistics: ExchangeMonthlyStatisticsInput,
) -> VerifiedExchangeMonthlyStatistics:
    """Validate and freeze one statistics document without changing the queue."""
    verified_queue = load_verified_review_queue(workspace)
    if (
        queue.manifest_sha256 != verified_queue.manifest_sha256
        or not queue.frame.equals(verified_queue.frame)
        or verified_queue.manifest.get("schema_version") != "2"
    ):
        raise ValueError("exchange statistics base review queue differs")
    authorization = load_exchange_monthly_statistics_authorization(
        network_authorization_path,
        workspace=workspace,
        queue=verified_queue,
    )
    request = authorization.request
    if (
        statistics.source_url != request.source_url
        or statistics.document_path.absolute() != request.document_path.absolute()
        or statistics.receipt_path.absolute() != request.receipt_path.absolute()
    ):
        raise ValueError("exchange statistics request paths differ")
    html_bytes = _read_regular(
        statistics.document_path,
        label="exchange statistics HTML",
    )
    receipt_bytes = _read_regular(
        statistics.receipt_path,
        label="exchange statistics receipt",
    )
    record = build_verified_statistics_record(
        workspace=workspace,
        queue=verified_queue,
        authorization_payload=authorization.payload,
        authorization_sha256=authorization.sha256,
        request=request,
        html_bytes=html_bytes,
        receipt_bytes=receipt_bytes,
    )
    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "role": "official_exchange_monthly_statistics_evidence",
        "attempt_id": workspace.root.parent.name,
        "base_review_manifest": file_binding(verified_queue.manifest_path),
        "network_authorization": file_binding(authorization.path),
        "record_count": 1,
        "record": record,
        "source_period_end": authorization.payload["source_period_end"],
        "asset_path_date": authorization.payload["asset_path_date"],
        "post_event": True,
        "final_test_strategy_outputs_read": False,
    }
    manifest_bytes = canonical_json_bytes(manifest)
    evidence_id = hashlib.sha256(manifest_bytes).hexdigest()
    files = {
        STATISTICS_MANIFEST_NAME: manifest_bytes,
        "statistics.html": html_bytes,
        "statistics.receipt.json": receipt_bytes,
    }
    destination = workspace.root.parent
    with opened_safe_directory(
        destination,
        label="official evidence destination",
    ) as root_fd:
        try:
            os.mkdir(STATISTICS_DIRECTORY, mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        statistics_fd = os.open(
            STATISTICS_DIRECTORY,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        try:
            write_frozen_tree_at(
                statistics_fd,
                evidence_id,
                files,
                resumable=True,
                label="exchange monthly statistics evidence",
            )
        finally:
            os.close(statistics_fd)
    return load_verified_exchange_monthly_statistics(
        destination
        / STATISTICS_DIRECTORY
        / evidence_id
        / STATISTICS_MANIFEST_NAME,
        workspace=workspace,
        queue=verified_queue,
    )


def load_verified_exchange_monthly_statistics(
    manifest_path: Path,
    *,
    workspace: EvidenceWorkspace,
    queue: VerifiedReviewQueue,
) -> VerifiedExchangeMonthlyStatistics:
    """Replay authorization, receipt, source bytes, row, and canonical PDF."""
    absolute = manifest_path.absolute()
    root = absolute.parent
    if (
        absolute.name != STATISTICS_MANIFEST_NAME
        or root.parent != workspace.root.parent / STATISTICS_DIRECTORY
        or _IMMUTABLE_ID.fullmatch(root.name) is None
    ):
        raise ValueError("exchange statistics evidence path is invalid")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        files = read_frozen_tree_at(
            descriptor,
            label="exchange monthly statistics evidence",
        )
    finally:
        os.close(descriptor)
    base_queue = _authorization_base_queue(workspace, queue)
    base_workspace = replace(
        workspace,
        review_queue_path=base_queue.manifest_path.with_name(REVIEW_QUEUE_NAME),
    )
    manifest_bytes = files.get(STATISTICS_MANIFEST_NAME, b"")
    manifest = _canonical_object(manifest_bytes)
    base = manifest.get("base_review_manifest")
    authorization_binding = manifest.get("network_authorization")
    record = manifest.get("record")
    if (
        hashlib.sha256(manifest_bytes).hexdigest() != root.name
        or manifest.get("schema_version") != _SCHEMA_VERSION
        or manifest.get("role")
        != "official_exchange_monthly_statistics_evidence"
        or manifest.get("attempt_id") != workspace.root.parent.name
        or manifest.get("record_count") != 1
        or manifest.get("post_event") is not True
        or manifest.get("final_test_strategy_outputs_read") is not False
        or not isinstance(base, dict)
        or base != file_binding(base_queue.manifest_path)
        or not isinstance(authorization_binding, dict)
        or not isinstance(record, dict)
    ):
        raise ValueError("exchange statistics evidence manifest is invalid")
    authorization_path = Path(str(authorization_binding.get("path", "")))
    if file_binding(authorization_path) != authorization_binding:
        raise ValueError("exchange statistics authorization changed")
    authorization = load_exchange_monthly_statistics_authorization(
        authorization_path,
        workspace=base_workspace,
        queue=base_queue,
    )
    if (
        manifest.get("source_period_end")
        != authorization.payload["source_period_end"]
        or manifest.get("asset_path_date")
        != authorization.payload["asset_path_date"]
    ):
        raise ValueError("exchange statistics period binding differs")
    validate_verified_statistics_record(
        record,
        workspace=base_workspace,
        queue=base_queue,
        authorization_payload=authorization.payload,
        authorization_sha256=authorization.sha256,
        request=authorization.request,
        files=files,
    )
    return VerifiedExchangeMonthlyStatistics(
        root=root,
        manifest_path=absolute,
        manifest_sha256=root.name,
        manifest=manifest,
        record=record,
    )


def _authorization_base_queue(
    workspace: EvidenceWorkspace,
    queue: VerifiedReviewQueue,
) -> VerifiedReviewQueue:
    if queue.manifest.get("schema_version") == "2":
        return queue
    base = queue.manifest.get("base_review_manifest")
    if (
        queue.manifest.get("schema_version") != _REVIEW_SCHEMA_VERSION
        or not isinstance(base, dict)
        or not isinstance(base.get("relative_path"), str)
    ):
        raise ValueError("exchange statistics base review queue differs")
    manifest_path = workspace.root.parent / str(base["relative_path"])
    base_workspace = replace(
        workspace,
        review_queue_path=manifest_path.with_name(REVIEW_QUEUE_NAME),
    )
    verified = load_verified_review_queue(base_workspace)
    if (
        verified.manifest_path != manifest_path
        or verified.manifest_sha256 != base.get("sha256")
        or verified.manifest.get("schema_version") != "2"
    ):
        raise ValueError("exchange statistics base review queue differs")
    return verified


def bind_exchange_monthly_statistics(
    *,
    workspace: EvidenceWorkspace,
    queue: VerifiedReviewQueue,
    statistics: VerifiedExchangeMonthlyStatistics,
) -> EvidenceWorkspace:
    """Publish schema-4 review identity binding the verified evidence."""
    verified_queue = load_verified_review_queue(workspace)
    loaded = load_verified_exchange_monthly_statistics(
        statistics.manifest_path,
        workspace=workspace,
        queue=verified_queue,
    )
    if (
        queue.manifest_sha256 != verified_queue.manifest_sha256
        or not queue.frame.equals(verified_queue.frame)
        or verified_queue.manifest.get("schema_version") != "2"
        or statistics.manifest_sha256 != loaded.manifest_sha256
    ):
        raise ValueError("exchange statistics binding inputs differ")
    base_queue = verified_queue.manifest.get("review_queue")
    if not isinstance(base_queue, dict) or not isinstance(
        base_queue.get("sha256"),
        str,
    ):
        raise ValueError("exchange statistics base review queue is invalid")
    manifest = dict(verified_queue.manifest)
    manifest["schema_version"] = _REVIEW_SCHEMA_VERSION
    manifest["base_review_manifest"] = {
        "relative_path": verified_queue.manifest_path.relative_to(
            workspace.root.parent
        ).as_posix(),
        "sha256": verified_queue.manifest_sha256,
        "queue_sha256": base_queue["sha256"],
    }
    manifest["exchange_monthly_statistics"] = {
        "relative_path": loaded.manifest_path.relative_to(
            workspace.root.parent
        ).as_posix(),
        "manifest_sha256": loaded.manifest_sha256,
        "record_count": 1,
    }
    manifest_bytes = canonical_json_bytes(manifest)
    queue_bytes = workspace.review_queue_path.read_bytes()
    session_id = hashlib.sha256(
        canonical_json_bytes(
            {
                "base_review_manifest_sha256": verified_queue.manifest_sha256,
                "base_review_queue_sha256": base_queue["sha256"],
                "exchange_monthly_statistics_manifest_sha256": (
                    loaded.manifest_sha256
                ),
            }
        )
    ).hexdigest()
    sessions_root = workspace.root / REVIEW_SESSIONS_DIRECTORY
    descriptor = os.open(
        sessions_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        write_frozen_tree_at(
            descriptor,
            session_id,
            {
                REVIEW_QUEUE_NAME: queue_bytes,
                REVIEW_MANIFEST_NAME: manifest_bytes,
            },
            resumable=True,
            label="official evidence review session",
        )
    finally:
        os.close(descriptor)
    bound = EvidenceWorkspace(
        root=workspace.root,
        catalog_path=workspace.catalog_path,
        routing_path=workspace.routing_path,
        review_queue_path=sessions_root / session_id / REVIEW_QUEUE_NAME,
        ready=False,
    )
    load_verified_review_queue(bound)
    return bound


def _read_regular(path: Path, *, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is missing or unsafe")
    for ancestor in path.parents:
        if ancestor.is_symlink():
            raise ValueError(f"{label} uses a symlink")
    return path.read_bytes()


def _canonical_object(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("exchange statistics evidence manifest is invalid") from error
    if not isinstance(value, dict) or raw != canonical_json_bytes(value):
        raise ValueError("exchange statistics evidence manifest is invalid")
    return value
