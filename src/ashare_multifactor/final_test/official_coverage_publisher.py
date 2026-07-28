"""Atomically publish both execution coverages from verified human decisions."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat

import polars as pl

from ashare_multifactor.audit.secure_tree import (
    atomic_rename_no_replace_at,
    write_frozen_tree_at,
)
from ashare_multifactor.data.security import market_for_symbol
from ashare_multifactor.final_test.action_source_contract import (
    FinalActionSourceContract,
    SHARED_ANNOUNCEMENT_CATEGORY,
)
from ashare_multifactor.final_test.corporate_action_coverage import (
    validate_corporate_action_coverage,
)
from ashare_multifactor.final_test.official_coverage_materialization import (
    CORPORATE_MANIFEST,
    SECURITY_MANIFEST,
    materialize_execution_coverage_files,
)
from ashare_multifactor.final_test.execution_sources import (
    validate_security_event_coverage,
)
from ashare_multifactor.final_test.final_root_binding import FinalRootBinding
from ashare_multifactor.final_test.gate import (
    FINAL_TEST_END,
    FINAL_TEST_START,
    FinalTestAuthorization,
)
from ashare_multifactor.final_test.official_evidence_workspace import (
    EvidenceWorkspace,
)
from ashare_multifactor.final_test.official_query_collection_root import (
    collection_lock,
    expected_output_root,
    opened_safe_directory,
)
from ashare_multifactor.final_test.official_query_coverage import (
    OfficialQueryScope,
    canonical_json_bytes,
)
from ashare_multifactor.final_test.official_query_index import (
    validate_official_query_coverage_index,
)
from ashare_multifactor.final_test.official_query_collector import (
    verify_official_query_collection_binding,
)
from ashare_multifactor.final_test.official_review_batches import (
    load_verified_batch_review_submission,
)
from ashare_multifactor.final_test.preparation import (
    FinalTestPreparation,
    verify_preparation,
)
from ashare_multifactor.final_test.recovery_secure_fs import open_directory_at
from ashare_multifactor.final_test.shared_evidence import (
    EXECUTION_COVERAGES_DIRECTORY,
)


_PUBLICATION_ID = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class PublishedExecutionCoverages:
    """One ready pair; neither coverage can be observed alone at publication."""

    publication_id: str
    root: Path
    corporate_root: Path
    security_manifest_path: Path
    corporate_manifest_sha256: str
    security_manifest_sha256: str


def publish_official_execution_coverages(
    *,
    preparation: FinalTestPreparation,
    authorization: FinalTestAuthorization,
    contract: FinalActionSourceContract,
    workspace: EvidenceWorkspace,
    candidate_manifest_path: Path | None,
    submission_manifest_path: Path,
    official_query_index_path: Path,
) -> PublishedExecutionCoverages:
    """Build, validate, and atomically expose the two ready coverages."""
    final_root = _final_root_from_preparation(preparation)
    output_root = expected_output_root(final_root, authorization.attempt_id)
    if workspace.root.parent.absolute() != output_root.absolute():
        raise ValueError("coverage workspace differs from the attempt evidence root")
    with FinalRootBinding.open(final_root) as binding:
        verified = verify_preparation(
            final_root,
            attempt_id=authorization.attempt_id,
            authorization=authorization,
            root_binding=binding,
        )
    if preparation != verified:
        raise ValueError("coverage preparation identity differs")
    symbols = _load_bound_symbols(verified)
    verify_official_query_collection_binding(
        output_root,
        preparation=verified,
        authorization=authorization,
        contract=contract,
    )
    expected_index = output_root / "official_query_coverage/official_query_coverage.json"
    if official_query_index_path.absolute() != expected_index.absolute():
        raise ValueError("coverage official-query index path is not canonical")
    scopes = tuple(
        OfficialQueryScope(
            symbol=symbol,
            market=market_for_symbol(symbol),
            category=SHARED_ANNOUNCEMENT_CATEGORY,
            query_category="",
            start=FINAL_TEST_START,
            end=FINAL_TEST_END,
        )
        for symbol in symbols
    )
    query_index = validate_official_query_coverage_index(
        official_query_index_path,
        expected_scopes=scopes,
    )
    submission = load_verified_batch_review_submission(
        submission_manifest_path,
        workspace=workspace,
        candidate_manifest_path=candidate_manifest_path,
    )
    if candidate_manifest_path is None:
        raise ValueError("coverage candidate snapshot is incomplete")
    candidate_manifest_bytes = candidate_manifest_path.read_bytes()
    candidate_manifest_sha256 = hashlib.sha256(candidate_manifest_bytes).hexdigest()
    candidate_frame = pl.read_parquet(candidate_manifest_path.parent / "candidates.parquet")
    publication_id = hashlib.sha256(
        canonical_json_bytes(
            {
                "attempt_id": authorization.attempt_id,
                "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
                "prepare_manifest_sha256": preparation.manifest_sha256,
                "review_submission_sha256": submission.manifest_sha256,
                "candidate_manifest_sha256": candidate_manifest_sha256,
                "official_query_index_sha256": query_index.index_sha256,
                "symbols_sha256": preparation.symbols_sha256,
            }
        )
    ).hexdigest()
    files = materialize_execution_coverage_files(
        symbols=symbols,
        output_root=output_root,
        query_index_path=official_query_index_path,
        query_index_sha256=query_index.index_sha256,
        workspace=workspace,
        submission=submission,
        candidates=candidate_frame,
        publication_id=publication_id,
        authorization=authorization,
        preparation=preparation,
        candidate_manifest_sha256=candidate_manifest_sha256,
    )
    with opened_safe_directory(output_root, label="official evidence destination") as root_fd:
        with collection_lock(root_fd):
            publications_fd = _open_or_create_publications(root_fd)
            try:
                if _entry_is_directory(publications_fd, publication_id):
                    return _load_publication(
                        output_root / EXECUTION_COVERAGES_DIRECTORY / publication_id,
                        publication_id=publication_id,
                        authorization=authorization,
                        submission_sha256=submission.manifest_sha256,
                        candidate_manifest_sha256=candidate_manifest_sha256,
                        query_index_sha256=query_index.index_sha256,
                        symbols=symbols,
                    )
                staging = f".{publication_id}.staging"
                write_frozen_tree_at(
                    publications_fd,
                    staging,
                    files,
                    resumable=True,
                    label="official execution coverage pair",
                )
                staging_root = (
                    output_root / EXECUTION_COVERAGES_DIRECTORY / staging
                )
                _validate_pair(staging_root, symbols=symbols)
                atomic_rename_no_replace_at(
                    publications_fd,
                    staging,
                    publication_id,
                )
                os.fsync(publications_fd)
            finally:
                os.close(publications_fd)
    return _load_publication(
        output_root / EXECUTION_COVERAGES_DIRECTORY / publication_id,
        publication_id=publication_id,
        authorization=authorization,
        submission_sha256=submission.manifest_sha256,
        candidate_manifest_sha256=candidate_manifest_sha256,
        query_index_sha256=query_index.index_sha256,
        symbols=symbols,
    )


def _load_publication(
    root: Path,
    *,
    publication_id: str,
    authorization: FinalTestAuthorization,
    submission_sha256: str,
    candidate_manifest_sha256: str,
    query_index_sha256: str,
    symbols: list[str],
) -> PublishedExecutionCoverages:
    if _PUBLICATION_ID.fullmatch(publication_id) is None:
        raise ValueError("official execution coverage publication id is invalid")
    corporate, security = _validate_pair(root, symbols=symbols)
    for manifest_path in (root / CORPORATE_MANIFEST, root / SECURITY_MANIFEST):
        manifest = json.loads(manifest_path.read_bytes())
        if (
            manifest.get("publication_id") != publication_id
            or manifest.get("attempt_id") != authorization.attempt_id
            or manifest.get("review_submission_sha256") != submission_sha256
            or manifest.get("candidate_manifest_sha256")
            != candidate_manifest_sha256
            or manifest.get("official_query_index_sha256") != query_index_sha256
        ):
            raise ValueError("official execution coverage pair binding differs")
    return PublishedExecutionCoverages(
        publication_id=publication_id,
        root=root,
        corporate_root=root / "corporate",
        security_manifest_path=root / SECURITY_MANIFEST,
        corporate_manifest_sha256=str(corporate["coverage_manifest_sha256"]),
        security_manifest_sha256=str(security["coverage_manifest_sha256"]),
    )


def _validate_pair(
    root: Path,
    *,
    symbols: list[str],
) -> tuple[dict[str, object], dict[str, object]]:
    corporate = validate_corporate_action_coverage(
        root / "corporate",
        symbols=symbols,
    )
    security = validate_security_event_coverage(
        root / SECURITY_MANIFEST,
        symbols=symbols,
    )
    return corporate, security


def _open_or_create_publications(root_fd: int) -> int:
    try:
        os.mkdir(EXECUTION_COVERAGES_DIRECTORY, mode=0o700, dir_fd=root_fd)
    except FileExistsError:
        pass
    descriptor = open_directory_at(
        root_fd,
        EXECUTION_COVERAGES_DIRECTORY,
        label="official execution coverages",
    )
    for name in os.listdir(descriptor):
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        valid_name = _PUBLICATION_ID.fullmatch(name) is not None or (
            name.startswith(".")
            and name.endswith(".staging")
            and _PUBLICATION_ID.fullmatch(name[1:-8]) is not None
        )
        if not valid_name or not stat.S_ISDIR(metadata.st_mode):
            os.close(descriptor)
            raise ValueError("official execution coverage inventory is invalid")
    return descriptor


def _entry_is_directory(parent_fd: int, name: str) -> bool:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("official execution coverage entry is unsafe")
    return True


def _load_bound_symbols(preparation: FinalTestPreparation) -> list[str]:
    frame = pl.read_parquet(preparation.symbol_scope_path)
    symbols = frame.get_column("symbol").cast(pl.String).to_list()
    digest = hashlib.sha256(("\n".join(symbols) + "\n").encode()).hexdigest()
    if (
        frame.columns != ["symbol"]
        or len(symbols) != preparation.symbol_count
        or symbols != sorted(set(symbols))
        or digest != preparation.symbols_sha256
    ):
        raise ValueError("coverage preparation symbol scope identity changed")
    return symbols


def _final_root_from_preparation(preparation: FinalTestPreparation) -> Path:
    if not isinstance(preparation, FinalTestPreparation):
        raise TypeError("coverage preparation is invalid")
    root = preparation.root
    if (
        root.name != preparation.attempt_id
        or root.parent.name != "preparations"
        or root.parent.parent.name != "final_test"
    ):
        raise ValueError("coverage preparation root is not canonical")
    return root.parent.parent
