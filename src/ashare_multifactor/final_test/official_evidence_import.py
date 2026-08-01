"""Import compatible raw evidence into a new attempt without network access."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any

import polars as pl

from ashare_multifactor.final_test.action_source_contract import FinalActionSourceContract
from ashare_multifactor.final_test.final_root_binding import FinalRootBinding
from ashare_multifactor.final_test.gate import FinalTestAuthorization
from ashare_multifactor.final_test.historical_attempt import (
    load_historical_attempt_authorization,
)
from ashare_multifactor.final_test.official_announcement_catalog import (
    build_announcement_catalog,
    load_verified_announcement_catalog,
)
from ashare_multifactor.final_test.official_announcement_catalog_schema import (
    VerifiedCandidateQueryCatalogSupplement,
)
from ashare_multifactor.final_test.official_announcement_routing import (
    build_announcement_routing,
)
from ashare_multifactor.final_test.official_document_validation import (
    OfficialDocumentValidationError,
    validate_official_document,
)
from ashare_multifactor.final_test.official_evidence_workspace import (
    DOCUMENT_MANIFEST_NAME,
    DOCUMENT_NAME,
    DOCUMENTS_DIRECTORY,
    QUARANTINE_DIRECTORY,
    QUARANTINE_MANIFEST_NAME,
    EvidenceWorkspace,
    load_cached_document,
    load_quarantined_document,
    open_workspace,
    publish_document_cache,
    publish_review_workspace,
)
from ashare_multifactor.final_test.official_query_collection_root import (
    COVERAGE_DIRECTORY,
    IDENTITIES_DIRECTORY,
    assert_directory_identities,
    collection_lock,
    expected_output_root,
    open_evidence_root,
    opened_safe_directory,
    write_or_verify_collection_manifest,
)
from ashare_multifactor.final_test.official_query_coverage import canonical_json_bytes
from ashare_multifactor.final_test.official_query_index import (
    copy_validated_official_query_coverage,
    validate_official_query_coverage_index,
)
from ashare_multifactor.final_test.official_query_collector import (
    expected_scopes,
    verify_official_query_collection_binding,
)
from ashare_multifactor.final_test.official_security_identity import (
    import_verified_cninfo_security_identities,
    load_verified_cninfo_security_identities,
)
from ashare_multifactor.final_test.preparation import (
    FinalTestPreparation,
    verify_preparation,
)
from ashare_multifactor.final_test.recovery_secure_fs import open_directory_at


@dataclass(frozen=True)
class RejectedImportedDocument:
    source_url: str
    reason: str


@dataclass(frozen=True)
class OfficialEvidenceImport:
    """Complete reuse report; rejected URLs are the only required refetch scope."""

    output_root: Path
    query_index_path: Path
    identity_index_path: Path
    catalog_path: Path
    workspace: EvidenceWorkspace
    reused_document_count: int
    rejected_documents: tuple[RejectedImportedDocument, ...]
    missing_urls: tuple[str, ...]


@dataclass(frozen=True)
class OfficialEvidenceImportInputs:
    """Verified inputs-only import state before any catalog is published."""

    source_preparation: FinalTestPreparation
    source_authorization: FinalTestAuthorization
    destination_preparation: FinalTestPreparation
    destination_authorization: FinalTestAuthorization
    contract: FinalActionSourceContract
    source_root: Path
    destination_root: Path
    query_index_path: Path
    identity_index_path: Path


def load_evidence_import_source_authorization(
    *,
    code_root: Path,
    data_root: Path,
    attempt_id: str,
) -> FinalTestAuthorization:
    """Verify an older source attempt without requiring its secret or CURRENT."""
    return load_historical_attempt_authorization(
        code_root=code_root,
        data_root=data_root,
        attempt_id=attempt_id,
    )


def verify_historical_evidence_source_preparation(
    final_root: Path,
    *,
    attempt_id: str,
    authorization: FinalTestAuthorization,
    root_binding: FinalRootBinding | None = None,
) -> FinalTestPreparation:
    """Verify the immutable preparation selected by source authorization."""
    return verify_preparation(
        final_root,
        attempt_id=attempt_id,
        authorization=authorization,
        root_binding=root_binding,
        require_current_stage8=False,
    )


def import_compatible_official_evidence(
    *,
    source_preparation: FinalTestPreparation,
    source_authorization: FinalTestAuthorization,
    destination_preparation: FinalTestPreparation,
    destination_authorization: FinalTestAuthorization,
    contract: FinalActionSourceContract,
    source_root: Path,
    destination_root: Path,
) -> OfficialEvidenceImport:
    """Preserve the original one-shot import behavior over two strict phases."""
    inputs = import_compatible_official_evidence_inputs(
        source_preparation=source_preparation,
        source_authorization=source_authorization,
        destination_preparation=destination_preparation,
        destination_authorization=destination_authorization,
        contract=contract,
        source_root=source_root,
        destination_root=destination_root,
    )
    return complete_compatible_official_evidence_import(inputs)


def import_compatible_official_evidence_inputs(
    *,
    source_preparation: FinalTestPreparation,
    source_authorization: FinalTestAuthorization,
    destination_preparation: FinalTestPreparation,
    destination_authorization: FinalTestAuthorization,
    contract: FinalActionSourceContract,
    source_root: Path,
    destination_root: Path,
) -> OfficialEvidenceImportInputs:
    """Rebind only identities and coverage, leaving the catalog unpublished."""
    if source_authorization.attempt_id == destination_authorization.attempt_id:
        raise ValueError("evidence import requires a new attempt")
    source_final = _final_root_from_preparation(source_preparation)
    destination_final = _final_root_from_preparation(destination_preparation)
    if source_final.absolute() != destination_final.absolute():
        raise ValueError("evidence import final roots differ")
    if source_root.absolute() != expected_output_root(
        source_final, source_authorization.attempt_id
    ).absolute() or destination_root.absolute() != expected_output_root(
        destination_final, destination_authorization.attempt_id
    ).absolute():
        raise ValueError("evidence import root differs from attempt binding")
    with FinalRootBinding.open(destination_final) as binding:
        source_verified = verify_historical_evidence_source_preparation(
            source_final,
            attempt_id=source_authorization.attempt_id,
            authorization=source_authorization,
            root_binding=binding,
        )
        destination_verified = verify_preparation(
            destination_final,
            attempt_id=destination_authorization.attempt_id,
            authorization=destination_authorization,
            root_binding=binding,
        )
        source_symbols = _load_symbols(source_verified)
        destination_symbols = _load_symbols(destination_verified)
        if (
            source_symbols != destination_symbols
            or source_preparation.symbols_sha256
            != destination_preparation.symbols_sha256
        ):
            raise ValueError("evidence import symbol scope differs")
        source_coverage = verify_official_query_collection_binding(
            source_root,
            preparation=source_verified,
            authorization=source_authorization,
            contract=contract,
        )
        with open_evidence_root(
            binding,
            attempt_id=destination_authorization.attempt_id,
        ) as destination:
            with collection_lock(destination.root_fd):
                write_or_verify_collection_manifest(
                    destination.root_fd,
                    preparation=destination_verified,
                    authorization=destination_authorization,
                    contract=contract,
                )
                with opened_safe_directory(
                    source_root,
                    label="source official evidence root",
                ) as source_fd:
                    source_identities_fd = open_directory_at(
                        source_fd,
                        IDENTITIES_DIRECTORY,
                        label="source CNInfo security identities",
                    )
                    try:
                        identities = import_verified_cninfo_security_identities(
                            source_fd=source_identities_fd,
                            source_root=source_root / IDENTITIES_DIRECTORY,
                            source_preparation=source_verified,
                            source_authorization=source_authorization,
                            destination_fd=destination.identities_fd,
                            destination_root=(
                                destination.output_root / IDENTITIES_DIRECTORY
                            ),
                            destination_preparation=destination_verified,
                            destination_authorization=destination_authorization,
                            symbols=destination_symbols,
                        )
                    finally:
                        os.close(source_identities_fd)
                scopes = expected_scopes(
                    destination_verified,
                    contract,
                    identities=identities,
                )
                copy_validated_official_query_coverage(
                    source_coverage / "official_query_coverage.json",
                    expected_scopes=scopes,
                    destination_root=destination.output_root / COVERAGE_DIRECTORY,
                )
                assert_directory_identities(
                    binding,
                    destination,
                    attempt_id=destination_authorization.attempt_id,
                )
    query_index = destination_root / COVERAGE_DIRECTORY / "official_query_coverage.json"
    identity_index = (
        destination_root
        / IDENTITIES_DIRECTORY
        / "official_security_identities.json"
    )
    return OfficialEvidenceImportInputs(
        source_preparation=source_verified,
        source_authorization=source_authorization,
        destination_preparation=destination_verified,
        destination_authorization=destination_authorization,
        contract=contract,
        source_root=source_root,
        destination_root=destination_root,
        query_index_path=query_index,
        identity_index_path=identity_index,
    )


def complete_compatible_official_evidence_import(
    inputs: OfficialEvidenceImportInputs,
    *,
    supplement: VerifiedCandidateQueryCatalogSupplement | None = None,
) -> OfficialEvidenceImport:
    """Publish catalog/routing/workspace only after the caller's interphase work."""
    source_verified, destination_verified = _revalidate_import_inputs(inputs)
    destination_authorization = inputs.destination_authorization
    contract = inputs.contract
    source_root = inputs.source_root
    destination_root = inputs.destination_root
    query_index = inputs.query_index_path
    catalog_path = build_announcement_catalog(
        query_index,
        preparation=destination_verified,
        authorization=destination_authorization,
        contract=contract,
        destination=destination_root,
        supplement=supplement,
    )
    catalog = load_verified_announcement_catalog(catalog_path)
    routing = build_announcement_routing(catalog_path, destination=destination_root)
    selected_urls = (
        catalog.frame.join(routing.frame, on="catalog_id", how="inner")
        .filter(pl.col("route") != "excluded")
        .get_column("source_url")
        .unique()
        .sort()
        .to_list()
    )
    rejected: list[RejectedImportedDocument] = []
    missing: list[str] = []
    with open_workspace(destination_root) as (
        workspace_root,
        _workspace_fd,
        documents_fd,
        quarantine_fd,
        sessions_fd,
    ):
        cached_documents = {}
        quarantined_documents = {}
        for source_url in selected_urls:
            cached = load_cached_document(documents_fd, source_url=source_url)
            if cached is None:
                payload, reason = _load_reusable_document(
                    source_root,
                    source_url=source_url,
                )
                if payload is not None:
                    cached = publish_document_cache(
                        documents_fd,
                        source_url=source_url,
                        payload=payload,
                    )
                else:
                    missing.append(source_url)
                    rejected.append(
                        RejectedImportedDocument(
                            source_url=source_url,
                            reason=reason,
                        )
                    )
            if cached is not None:
                cached_documents[source_url] = cached
                continue
            quarantined = load_quarantined_document(
                quarantine_fd,
                source_url=source_url,
            )
            if quarantined is not None:
                quarantined_documents[source_url] = quarantined
        workspace = publish_review_workspace(
            workspace_root=workspace_root,
            sessions_fd=sessions_fd,
            catalog=catalog,
            routing=routing,
            cached_documents=cached_documents,
            quarantined_documents=quarantined_documents,
        )
    return OfficialEvidenceImport(
        output_root=destination_root,
        query_index_path=query_index,
        identity_index_path=inputs.identity_index_path,
        catalog_path=catalog_path,
        workspace=workspace,
        reused_document_count=len(cached_documents),
        rejected_documents=tuple(rejected),
        missing_urls=tuple(missing),
    )


def _revalidate_import_inputs(
    inputs: OfficialEvidenceImportInputs,
) -> tuple[FinalTestPreparation, FinalTestPreparation]:
    if not isinstance(inputs, OfficialEvidenceImportInputs):
        raise TypeError("evidence import inputs are invalid")
    source_preparation = inputs.source_preparation
    source_authorization = inputs.source_authorization
    destination_preparation = inputs.destination_preparation
    destination_authorization = inputs.destination_authorization
    if source_authorization.attempt_id == destination_authorization.attempt_id:
        raise ValueError("evidence import requires a new attempt")
    if (
        source_preparation.attempt_id != source_authorization.attempt_id
        or destination_preparation.attempt_id != destination_authorization.attempt_id
    ):
        raise ValueError("evidence import preparation differs from authorization")
    source_final = _final_root_from_preparation(source_preparation)
    destination_final = _final_root_from_preparation(destination_preparation)
    if source_final.absolute() != destination_final.absolute():
        raise ValueError("evidence import final roots differ")
    if inputs.source_root.absolute() != expected_output_root(
        source_final, source_authorization.attempt_id
    ).absolute():
        raise ValueError("evidence import source root differs from attempt binding")
    if inputs.destination_root.absolute() != expected_output_root(
        destination_final, destination_authorization.attempt_id
    ).absolute():
        raise ValueError("evidence import destination root differs from attempt binding")
    expected_query_index = (
        inputs.destination_root
        / COVERAGE_DIRECTORY
        / "official_query_coverage.json"
    )
    expected_identity_index = (
        inputs.destination_root
        / IDENTITIES_DIRECTORY
        / "official_security_identities.json"
    )
    if (
        inputs.query_index_path.absolute() != expected_query_index.absolute()
        or inputs.identity_index_path.absolute() != expected_identity_index.absolute()
    ):
        raise ValueError("evidence import phase identity differs")

    with FinalRootBinding.open(destination_final) as binding:
        source_verified = verify_historical_evidence_source_preparation(
            destination_final,
            attempt_id=source_authorization.attempt_id,
            authorization=source_authorization,
            root_binding=binding,
        )
        destination_verified = verify_preparation(
            destination_final,
            attempt_id=destination_authorization.attempt_id,
            authorization=destination_authorization,
            root_binding=binding,
        )
    if (
        source_verified != source_preparation
        or destination_verified != destination_preparation
    ):
        raise ValueError("evidence import preparation identity changed")
    source_symbols = _load_symbols(source_verified)
    destination_symbols = _load_symbols(destination_verified)
    if (
        source_symbols != destination_symbols
        or source_verified.symbols_sha256 != destination_verified.symbols_sha256
    ):
        raise ValueError("evidence import symbol scope differs")

    verify_official_query_collection_binding(
        inputs.source_root,
        preparation=source_verified,
        authorization=source_authorization,
        contract=inputs.contract,
    )
    verify_official_query_collection_binding(
        inputs.destination_root,
        preparation=destination_verified,
        authorization=destination_authorization,
        contract=inputs.contract,
    )
    with opened_safe_directory(
        inputs.destination_root,
        label="destination official evidence root",
    ) as destination_fd:
        identities_fd = open_directory_at(
            destination_fd,
            IDENTITIES_DIRECTORY,
            label="destination CNInfo security identities",
        )
        try:
            identities = load_verified_cninfo_security_identities(
                identities_fd=identities_fd,
                identity_root=inputs.destination_root / IDENTITIES_DIRECTORY,
                symbols=destination_symbols,
                preparation=destination_verified,
                authorization=destination_authorization,
            )
        finally:
            os.close(identities_fd)
    scopes = expected_scopes(destination_verified, inputs.contract, identities=identities)
    validate_official_query_coverage_index(
        inputs.query_index_path,
        expected_scopes=scopes,
    )
    return source_verified, destination_verified


def _load_reusable_document(
    source_root: Path,
    *,
    source_url: str,
) -> tuple[bytes | None, str]:
    cache_id = hashlib.sha256(source_url.encode()).hexdigest()
    entry = (
        source_root
        / "official_document_workspace"
        / DOCUMENTS_DIRECTORY
        / cache_id
    )
    if not entry.exists():
        reason = _source_quarantine_reason(source_root, cache_id=cache_id)
        return None, reason or "source_document_missing"
    _assert_safe_directory(entry, label="source official document cache")
    if {path.name for path in entry.iterdir()} != {
        DOCUMENT_NAME,
        DOCUMENT_MANIFEST_NAME,
    }:
        return None, "source_document_inventory_invalid"
    document_path = entry / DOCUMENT_NAME
    manifest_path = entry / DOCUMENT_MANIFEST_NAME
    _assert_safe_file(document_path, label="source official document")
    _assert_safe_file(manifest_path, label="source official document manifest")
    payload = document_path.read_bytes()
    try:
        manifest = _canonical_object(manifest_path.read_bytes())
    except ValueError:
        return None, "source_document_manifest_invalid"
    version = manifest.get("schema_version")
    expected_fields = {
        "schema_version",
        "role",
        "source_url",
        "cache_path",
        "sha256",
        "size_bytes",
    }
    if version == "2":
        expected_fields |= {"media_type", "page_count"}
    if (
        version not in {"1", "2"}
        or set(manifest) != expected_fields
        or manifest.get("role") != "official_document_cache"
        or manifest.get("source_url") != source_url
        or manifest.get("cache_path")
        != f"{DOCUMENTS_DIRECTORY}/{cache_id}/{DOCUMENT_NAME}"
        or manifest.get("sha256") != hashlib.sha256(payload).hexdigest()
        or manifest.get("size_bytes") != len(payload)
    ):
        return None, "source_document_identity_invalid"
    try:
        validate_official_document(payload, source_url=source_url)
    except OfficialDocumentValidationError as error:
        return None, error.reason
    return payload, ""


def _source_quarantine_reason(source_root: Path, *, cache_id: str) -> str:
    manifest_path = (
        source_root
        / "official_document_workspace"
        / QUARANTINE_DIRECTORY
        / cache_id
        / QUARANTINE_MANIFEST_NAME
    )
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return ""
    try:
        manifest = _canonical_object(manifest_path.read_bytes())
    except ValueError:
        return "source_quarantine_manifest_invalid"
    reason = manifest.get("reason")
    return str(reason) if isinstance(reason, str) and reason else "source_quarantined"


def _load_symbols(preparation: FinalTestPreparation) -> list[str]:
    frame = pl.read_parquet(preparation.symbol_scope_path)
    symbols = frame.get_column("symbol").cast(pl.String).to_list()
    digest = hashlib.sha256(("\n".join(symbols) + "\n").encode()).hexdigest()
    if (
        frame.columns != ["symbol"]
        or symbols != sorted(set(symbols))
        or len(symbols) != preparation.symbol_count
        or digest != preparation.symbols_sha256
    ):
        raise ValueError("evidence import symbol scope identity changed")
    return symbols


def _final_root_from_preparation(preparation: FinalTestPreparation) -> Path:
    if not isinstance(preparation, FinalTestPreparation):
        raise TypeError("evidence import preparation is invalid")
    root = preparation.root
    if (
        root.name != preparation.attempt_id
        or root.parent.name != "preparations"
        or root.parent.parent.name != "final_test"
    ):
        raise ValueError("evidence import preparation root is not canonical")
    return root.parent.parent


def _canonical_object(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("source document manifest is invalid") from error
    if not isinstance(payload, dict) or raw != canonical_json_bytes(payload):
        raise ValueError("source document manifest is invalid")
    return payload


def _assert_safe_directory(path: Path, *, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label} is unsafe")
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise ValueError(f"{label} uses a symlink")


def _assert_safe_file(path: Path, *, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is unsafe")
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"{label} is unsafe")
