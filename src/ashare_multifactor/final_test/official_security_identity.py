"""Resolve and bind CNInfo ``orgId`` values for a prepared security scope."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Any
from uuid import uuid4

from ashare_multifactor.data.security import market_for_symbol
from ashare_multifactor.final_test.gate import FinalTestAuthorization
from ashare_multifactor.final_test.official_collection_progress import (
    OfficialCollectionProgressObserver,
)
from ashare_multifactor.final_test.official_query_client import (
    OFFICIAL_SECURITY_IDENTITY_ENDPOINT,
    OfficialQueryTransientError,
    OfficialQueryTransport,
    RetryPolicy,
)
from ashare_multifactor.final_test.official_query_collection_root import entry_exists
from ashare_multifactor.final_test.official_query_coverage import canonical_json_bytes
from ashare_multifactor.final_test.official_security_identity_package import (
    CNInfoSecurityIdentity,
    identity_form,
    load_identity_package,
    package_relative_path,
    publish_identity_package,
)
from ashare_multifactor.final_test.preparation import FinalTestPreparation
from ashare_multifactor.final_test.recovery_secure_fs import (
    atomic_rename_no_replace_at,
    read_bytes_at,
    write_bytes_exclusive_at,
)


IDENTITY_INDEX_NAME = "official_security_identities.json"
_TEMP_INDEX = re.compile(r"\.security-identities\.[0-9a-f]{32}\.tmp")
_ORG_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SCHEMA_VERSION = "1"
_INDEX_ROLE = "cninfo_security_identity_coverage"


@dataclass(frozen=True)
class VerifiedCNInfoSecurityIdentityIndex:
    """Immutable organisation identities, with no mutable cache paths exposed."""

    index_path: Path
    index_sha256: str
    identities: tuple[CNInfoSecurityIdentity, ...]

    def org_id_for(self, symbol: str, market: str) -> str:
        for identity in self.identities:
            if identity.symbol == symbol and identity.market == market:
                return identity.org_id
        raise ValueError("CNInfo security identity is missing from frozen scope")


def collect_cninfo_security_identities(
    *,
    identities_fd: int,
    identity_root: Path,
    symbols: Iterable[str],
    preparation: FinalTestPreparation,
    authorization: FinalTestAuthorization,
    transport: OfficialQueryTransport,
    policy: RetryPolicy,
    sleep: Callable[[float], None] = time.sleep,
    progress_observer: OfficialCollectionProgressObserver | None = None,
) -> VerifiedCNInfoSecurityIdentityIndex:
    """Publish one immutable identity index before any announcement query runs."""
    normalized = _normalize_symbols(symbols)
    _assert_binding(preparation, authorization)
    _recover_orphan_index_temps(identities_fd)
    if entry_exists(identities_fd, IDENTITY_INDEX_NAME):
        return load_verified_cninfo_security_identities(
            identities_fd=identities_fd,
            identity_root=identity_root,
            symbols=normalized,
            preparation=preparation,
            authorization=authorization,
        )

    records = []
    for completed, symbol in enumerate(normalized):
        market = market_for_symbol(symbol)
        identity, record = _collect_one_identity(
            identities_fd,
            symbol=symbol,
            market=market,
            transport=transport,
            policy=policy,
            created_at=authorization.registered_at,
            sleep=sleep,
            progress_observer=progress_observer,
            completed=completed,
            total=len(normalized),
        )
        if identity.symbol != symbol or identity.market != market:
            raise ValueError("CNInfo security identity differs from prepared scope")
        records.append(record)
        if progress_observer is not None:
            progress_observer.identity_completed(
                symbol=symbol,
                completed=completed + 1,
                total=len(normalized),
            )
    payload = _index_payload(
        records,
        preparation=preparation,
        authorization=authorization,
    )
    _publish_index(identities_fd, canonical_json_bytes(payload))
    return load_verified_cninfo_security_identities(
        identities_fd=identities_fd,
        identity_root=identity_root,
        symbols=normalized,
        preparation=preparation,
        authorization=authorization,
    )


def load_verified_cninfo_security_identities(
    *,
    identities_fd: int,
    identity_root: Path,
    symbols: Iterable[str],
    preparation: FinalTestPreparation,
    authorization: FinalTestAuthorization,
) -> VerifiedCNInfoSecurityIdentityIndex:
    """Verify all cached CNInfo identities against one prepared attempt."""
    normalized = _normalize_symbols(symbols)
    _assert_binding(preparation, authorization)
    raw = read_bytes_at(
        identities_fd,
        IDENTITY_INDEX_NAME,
        label="CNInfo security identity index",
    )
    payload = _canonical_object(raw, label="CNInfo security identity index")
    records = _validate_index_payload(
        payload,
        symbols=normalized,
        preparation=preparation,
        authorization=authorization,
    )
    identities = []
    for record in records:
        identity, actual = load_identity_package(
            identities_fd,
            symbol=str(record["symbol"]),
            market=str(record["market"]),
        )
        if actual != record:
            raise ValueError("CNInfo security identity package differs from index")
        identities.append(identity)
    return VerifiedCNInfoSecurityIdentityIndex(
        index_path=identity_root / IDENTITY_INDEX_NAME,
        index_sha256=hashlib.sha256(raw).hexdigest(),
        identities=tuple(identities),
    )


def import_verified_cninfo_security_identities(
    *,
    source_fd: int,
    source_root: Path,
    source_preparation: FinalTestPreparation,
    source_authorization: FinalTestAuthorization,
    destination_fd: int,
    destination_root: Path,
    destination_preparation: FinalTestPreparation,
    destination_authorization: FinalTestAuthorization,
    symbols: Iterable[str],
) -> VerifiedCNInfoSecurityIdentityIndex:
    """Revalidate public responses and bind a new index without network access."""
    normalized = _normalize_symbols(symbols)
    source = load_verified_cninfo_security_identities(
        identities_fd=source_fd,
        identity_root=source_root,
        symbols=normalized,
        preparation=source_preparation,
        authorization=source_authorization,
    )
    records: list[dict[str, object]] = []
    for identity in source.identities:
        response = _identity_response(
            source_fd,
            symbol=identity.symbol,
            market=identity.market,
        )
        imported, record = publish_identity_package(
            destination_fd,
            symbol=identity.symbol,
            market=identity.market,
            response=response,
            created_at=destination_authorization.registered_at,
        )
        if imported != identity:
            raise ValueError("imported CNInfo security identity differs from source")
        records.append(record)
    _publish_index(
        destination_fd,
        canonical_json_bytes(
            _index_payload(
                records,
                preparation=destination_preparation,
                authorization=destination_authorization,
            )
        ),
    )
    return load_verified_cninfo_security_identities(
        identities_fd=destination_fd,
        identity_root=destination_root,
        symbols=normalized,
        preparation=destination_preparation,
        authorization=destination_authorization,
    )


def _collect_one_identity(
    identities_fd: int,
    *,
    symbol: str,
    market: str,
    transport: OfficialQueryTransport,
    policy: RetryPolicy,
    created_at: str,
    sleep: Callable[[float], None],
    progress_observer: OfficialCollectionProgressObserver | None,
    completed: int,
    total: int,
) -> tuple[CNInfoSecurityIdentity, dict[str, object]]:
    try:
        return load_identity_package(identities_fd, symbol=symbol, market=market)
    except FileNotFoundError:
        response = _fetch_identity_response(
            transport,
            symbol=symbol,
            policy=policy,
            sleep=sleep,
            progress_observer=progress_observer,
            completed=completed,
            total=total,
        )
        return publish_identity_package(
            identities_fd,
            symbol=symbol,
            market=market,
            response=response,
            created_at=created_at,
        )


def _fetch_identity_response(
    transport: OfficialQueryTransport,
    *,
    symbol: str,
    policy: RetryPolicy,
    sleep: Callable[[float], None],
    progress_observer: OfficialCollectionProgressObserver | None,
    completed: int,
    total: int,
) -> bytes:
    form = identity_form(symbol)
    for attempt in range(policy.attempts):
        try:
            if progress_observer is not None:
                progress_observer.identity_request(
                    symbol=symbol,
                    completed=completed,
                    total=total,
                )
            payload = transport.fetch(
                OFFICIAL_SECURITY_IDENTITY_ENDPOINT,
                form,
                timeout_seconds=policy.timeout_seconds,
            )
        except OfficialQueryTransientError:
            if attempt + 1 == policy.attempts:
                raise
            sleep(policy.minimum_interval_seconds * (2**attempt))
            continue
        if not payload:
            raise ValueError("CNInfo security identity response is empty")
        sleep(policy.minimum_interval_seconds)
        return payload
    raise RuntimeError("CNInfo security identity retry loop terminated unexpectedly")


def _index_payload(
    records: list[dict[str, object]],
    *,
    preparation: FinalTestPreparation,
    authorization: FinalTestAuthorization,
) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "role": _INDEX_ROLE,
        "attempt_id": authorization.attempt_id,
        "approval_id": authorization.approval_id,
        "git": {"commit": authorization.git_commit, "tree": authorization.git_tree},
        "stage8": {
            "run_id": authorization.robustness_release,
            "manifest_sha256": authorization.robustness_manifest_sha256,
            "lineage_sha256": authorization.robustness_lineage_sha256,
            "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        },
        "period": [
            authorization.test_period[0].isoformat(),
            authorization.test_period[1].isoformat(),
        ],
        "prepare_manifest_sha256": preparation.manifest_sha256,
        "symbols_sha256": preparation.symbols_sha256,
        "identity_endpoint": OFFICIAL_SECURITY_IDENTITY_ENDPOINT,
        "identity_method": "POST",
        "records": sorted(records, key=lambda record: (record["market"], record["symbol"])),
    }


def _validate_index_payload(
    payload: Mapping[str, object],
    *,
    symbols: tuple[str, ...],
    preparation: FinalTestPreparation,
    authorization: FinalTestAuthorization,
) -> tuple[dict[str, object], ...]:
    expected = _index_payload(
        [], preparation=preparation, authorization=authorization
    )
    required = set(expected)
    if set(payload) != required or any(
        payload.get(key) != value for key, value in expected.items() if key != "records"
    ):
        raise ValueError("CNInfo security identity index differs from attempt")
    records = payload.get("records")
    if not isinstance(records, list) or len(records) != len(symbols):
        raise ValueError("CNInfo security identity index scope is incomplete")
    expected_keys = {(market_for_symbol(symbol), symbol) for symbol in symbols}
    normalized: list[dict[str, object]] = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "symbol",
            "market",
            "org_id",
            "relative_path",
            "request_sha256",
            "response_sha256",
            "manifest_sha256",
        }:
            raise ValueError("CNInfo security identity index record is invalid")
        symbol = record.get("symbol")
        market = record.get("market")
        org_id = record.get("org_id")
        if (
            not isinstance(symbol, str)
            or not isinstance(market, str)
            or not isinstance(org_id, str)
            or (market, symbol) not in expected_keys
            or _ORG_ID.fullmatch(org_id) is None
            or record.get("relative_path") != package_relative_path(market, symbol)
            or any(
                not isinstance(record.get(field), str)
                or _SHA256.fullmatch(str(record[field])) is None
                for field in ("request_sha256", "response_sha256", "manifest_sha256")
            )
        ):
            raise ValueError("CNInfo security identity index record is invalid")
        normalized.append(record)
    if {(str(record["market"]), str(record["symbol"])) for record in normalized} != expected_keys:
        raise ValueError("CNInfo security identity index scope is incomplete")
    ordered = tuple(sorted(normalized, key=lambda record: (record["market"], record["symbol"])))
    if records != list(ordered):
        raise ValueError("CNInfo security identity index is not stably ordered")
    return ordered


def _publish_index(identities_fd: int, payload: bytes) -> None:
    try:
        existing = read_bytes_at(
            identities_fd,
            IDENTITY_INDEX_NAME,
            label="CNInfo security identity index",
        )
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if existing != payload:
            raise ValueError("CNInfo security identity index differs from collection")
        return
    temporary = f".security-identities.{uuid4().hex}.tmp"
    try:
        write_bytes_exclusive_at(identities_fd, temporary, payload)
        atomic_rename_no_replace_at(
            identities_fd,
            temporary,
            identities_fd,
            IDENTITY_INDEX_NAME,
        )
        os.fsync(identities_fd)
    except FileExistsError:
        existing = read_bytes_at(
            identities_fd,
            IDENTITY_INDEX_NAME,
            label="CNInfo security identity index",
        )
        if existing != payload:
            raise ValueError("CNInfo security identity index differs from collection")
    finally:
        try:
            os.unlink(temporary, dir_fd=identities_fd)
        except FileNotFoundError:
            pass


def _recover_orphan_index_temps(identities_fd: int) -> None:
    removed = False
    for name in sorted(os.listdir(identities_fd)):
        if _TEMP_INDEX.fullmatch(name) is None:
            continue
        metadata = os.stat(name, dir_fd=identities_fd, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("CNInfo security identity index temp is unsafe")
        os.unlink(name, dir_fd=identities_fd)
        removed = True
    if removed:
        os.fsync(identities_fd)


def _identity_response(
    identities_fd: int,
    *,
    symbol: str,
    market: str,
) -> bytes:
    descriptor = identities_fd
    opened: list[int] = []
    try:
        for name in package_relative_path(market, symbol).split("/"):
            child = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            if descriptor != identities_fd:
                opened.append(descriptor)
            descriptor = child
        return read_bytes_at(
            descriptor,
            "response.json",
            label="CNInfo security identity response",
        )
    finally:
        if descriptor != identities_fd:
            os.close(descriptor)
        for item in reversed(opened):
            os.close(item)


def _normalize_symbols(symbols: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(sorted(set(symbols)))
    if not normalized or any(
        market_for_symbol(symbol) not in {"sh", "sz"} for symbol in normalized
    ):
        raise ValueError("CNInfo security identity scope is invalid")
    return normalized


def _assert_binding(
    preparation: FinalTestPreparation,
    authorization: FinalTestAuthorization,
) -> None:
    if (
        not isinstance(preparation, FinalTestPreparation)
        or not isinstance(authorization, FinalTestAuthorization)
        or preparation.attempt_id != authorization.attempt_id
        or authorization.test_period != (date(2022, 1, 1), date(2025, 12, 31))
    ):
        raise ValueError("CNInfo security identity attempt binding is invalid")


def _canonical_object(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{label} is invalid") from error
    if not isinstance(payload, dict) or raw != canonical_json_bytes(payload):
        raise ValueError(f"{label} is invalid")
    return payload


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate JSON key")
        payload[key] = value
    return payload
