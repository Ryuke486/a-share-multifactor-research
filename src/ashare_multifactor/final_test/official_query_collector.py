"""Orchestrate immutable public-query coverage for one prepared attempt."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import os
from pathlib import Path

from ashare_multifactor.data.security import market_for_symbol
from ashare_multifactor.final_test.action_source_contract import (
    FinalActionSourceContract,
    official_query_scope,
)
from ashare_multifactor.final_test.final_root_binding import FinalRootBinding
from ashare_multifactor.final_test.gate import FinalTestAuthorization
from ashare_multifactor.final_test.official_query_client import (
    OfficialQueryTransport,
    RetryPolicy,
)
from ashare_multifactor.final_test.official_query_collection_root import (
    COLLECTION_MANIFEST,
    COVERAGE_DIRECTORY,
    IDENTITIES_DIRECTORY,
    assert_directory_identities,
    collection_lock,
    expected_output_root,
    open_evidence_root,
    opened_safe_directory,
    verify_collection_manifest,
    write_or_verify_collection_manifest,
)
from ashare_multifactor.final_test.official_query_coverage import OfficialQueryScope
from ashare_multifactor.final_test.official_query_packages import collect_coverage
from ashare_multifactor.final_test.official_security_identity import (
    VerifiedCNInfoSecurityIdentityIndex,
    collect_cninfo_security_identities,
)
from ashare_multifactor.final_test.preparation import (
    FinalTestPreparation,
    verify_preparation,
)
from ashare_multifactor.final_test.recovery_secure_fs import open_directory_at
from ashare_multifactor.final_test.resume import load_bound_symbol_scope


_CATEGORIES = ("corporate_actions", "security_events")


@dataclass(frozen=True)
class QueryCollectionResult:
    """Progress for one preparation-bound public query collection."""

    root: Path
    completed_scopes: int
    total_scopes: int
    index_path: Path | None
    binding_path: Path
    identity_index_path: Path


def collect_official_query_coverage(
    *,
    preparation: FinalTestPreparation,
    authorization: FinalTestAuthorization,
    contract: FinalActionSourceContract,
    output_root: Path,
    transport: OfficialQueryTransport,
    policy: RetryPolicy,
    identity_transport: OfficialQueryTransport | None = None,
    max_scopes: int | None = None,
) -> QueryCollectionResult:
    """Collect exact public query coverage without changing attempt state."""
    if max_scopes is not None and (
        not isinstance(max_scopes, int)
        or isinstance(max_scopes, bool)
        or max_scopes <= 0
    ):
        raise ValueError("official query maximum scopes is invalid")
    _assert_authorization(authorization)
    if authorization.attempt_id != preparation.attempt_id:
        raise ValueError("official query preparation differs from authorization")
    final_root = _final_root_from_preparation(preparation)
    expected_root = expected_output_root(final_root, authorization.attempt_id)
    if output_root.absolute() != expected_root.absolute():
        raise ValueError("official query output root differs from the attempt root")

    with FinalRootBinding.open(final_root) as root_binding:
        verified = verify_preparation(
            final_root,
            attempt_id=authorization.attempt_id,
            authorization=authorization,
            root_binding=root_binding,
        )
        if preparation != verified:
            raise ValueError("official query preparation identity differs")
        symbols = load_bound_symbol_scope(verified)
        if not symbols:
            raise ValueError("official query preparation symbol scope is empty")
        with open_evidence_root(
            root_binding,
            attempt_id=authorization.attempt_id,
        ) as evidence:
            with collection_lock(evidence.root_fd):
                write_or_verify_collection_manifest(
                    evidence.root_fd,
                    preparation=verified,
                    authorization=authorization,
                    contract=contract,
                )
                identities = collect_cninfo_security_identities(
                    identities_fd=evidence.identities_fd,
                    identity_root=evidence.output_root / IDENTITIES_DIRECTORY,
                    symbols=symbols,
                    preparation=verified,
                    authorization=authorization,
                    transport=(
                        transport if identity_transport is None else identity_transport
                    ),
                    policy=policy,
                )
                scopes = expected_scopes(verified, contract, identities=identities)
                assert_directory_identities(
                    root_binding,
                    evidence,
                    attempt_id=authorization.attempt_id,
                )
                progress = collect_coverage(
                    coverage_fd=evidence.coverage_fd,
                    coverage_root=evidence.output_root / COVERAGE_DIRECTORY,
                    scopes=scopes,
                    transport=transport,
                    policy=policy,
                    max_scopes=max_scopes,
                    created_at=authorization.registered_at,
                )
                assert_directory_identities(
                    root_binding,
                    evidence,
                    attempt_id=authorization.attempt_id,
                )
                return QueryCollectionResult(
                    root=evidence.output_root / COVERAGE_DIRECTORY,
                    completed_scopes=progress.completed_scopes,
                    total_scopes=len(scopes),
                    index_path=progress.index_path,
                    binding_path=evidence.output_root / COLLECTION_MANIFEST,
                    identity_index_path=identities.index_path,
                )


def verify_official_query_collection_binding(
    output_root: Path,
    *,
    preparation: FinalTestPreparation,
    authorization: FinalTestAuthorization,
    contract: FinalActionSourceContract,
) -> Path:
    """Verify the immutable query-root identity before consuming its cache."""
    _assert_authorization(authorization)
    final_root = _final_root_from_preparation(preparation)
    expected_root = expected_output_root(final_root, authorization.attempt_id)
    if output_root.absolute() != expected_root.absolute():
        raise ValueError("official query output root differs from the attempt root")
    with opened_safe_directory(expected_root, label="official query evidence root") as fd:
        verify_collection_manifest(
            fd,
            preparation=preparation,
            authorization=authorization,
            contract=contract,
        )
        coverage_fd = open_directory_at(
            fd,
            COVERAGE_DIRECTORY,
            label="official query coverage root",
        )
        os.close(coverage_fd)
    return expected_root / COVERAGE_DIRECTORY


def expected_scopes(
    preparation: FinalTestPreparation,
    contract: FinalActionSourceContract,
    *,
    identities: VerifiedCNInfoSecurityIdentityIndex,
) -> tuple[OfficialQueryScope, ...]:
    """Derive the immutable two-category scope set from a verified preparation."""
    if tuple(category for category, _query in contract.official_query_categories) != _CATEGORIES:
        raise ValueError("official query categories differ from the frozen contract")
    symbols = load_bound_symbol_scope(preparation)
    if not symbols:
        raise ValueError("official query preparation symbol scope is empty")
    return tuple(
        sorted(
            (
                official_query_scope(
                    contract,
                    symbol=symbol,
                    category=category,
                    org_id=identities.org_id_for(symbol, market_for_symbol(symbol)),
                )
                for category in _CATEGORIES
                for symbol in symbols
            ),
            key=_scope_key,
        )
    )


def _assert_authorization(authorization: FinalTestAuthorization) -> None:
    if not isinstance(authorization, FinalTestAuthorization):
        raise TypeError("official query authorization is invalid")
    if authorization.test_period != (date(2022, 1, 1), date(2025, 12, 31)):
        raise ValueError("official query authorization period differs from final test")


def _final_root_from_preparation(preparation: FinalTestPreparation) -> Path:
    if not isinstance(preparation, FinalTestPreparation):
        raise TypeError("official query preparation is invalid")
    root = preparation.root
    if (
        root.name != preparation.attempt_id
        or root.parent.name != "preparations"
        or root.parent.parent.name != "final_test"
    ):
        raise ValueError("official query preparation root is not canonical")
    return root.parent.parent


def _scope_key(scope: OfficialQueryScope) -> tuple[str, str, str, str]:
    return scope.category, scope.market, scope.symbol, scope.query_category
