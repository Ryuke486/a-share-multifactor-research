"""Immutable CNInfo security-identity package format and storage helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
import re
from typing import Any

from ashare_multifactor.audit.secure_tree import (
    read_frozen_tree_at,
    write_frozen_tree_at,
)
from ashare_multifactor.data.security import market_for_symbol
from ashare_multifactor.final_test.official_query_client import (
    OFFICIAL_SECURITY_IDENTITY_ENDPOINT,
)
from ashare_multifactor.final_test.official_query_collection_root import entry_exists
from ashare_multifactor.final_test.official_query_coverage import canonical_json_bytes
from ashare_multifactor.final_test.recovery_secure_fs import open_directory_at


_PACKAGES_DIRECTORY = "packages"
_PACKAGE_NAME = "identity-package"
_ORG_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class CNInfoSecurityIdentity:
    """One CNInfo organisation identity for one frozen A-share code."""

    symbol: str
    market: str
    org_id: str


def identity_form(symbol: str) -> dict[str, str]:
    """Return the single permitted public CNInfo identity-search form."""
    market_for_symbol(symbol)
    return {"keyWord": symbol, "maxNum": "10", "plate": ""}


def load_identity_package(
    identities_fd: int,
    *,
    symbol: str,
    market: str,
) -> tuple[CNInfoSecurityIdentity, dict[str, object]]:
    """Read one immutable identity package, creating only empty parents."""
    parent_fd = _open_identity_parent(identities_fd, market=market, symbol=symbol)
    try:
        package_fd = open_directory_at(
            parent_fd,
            _PACKAGE_NAME,
            label="CNInfo security identity package",
        )
        try:
            files = read_frozen_tree_at(
                package_fd,
                label="CNInfo security identity package",
            )
        finally:
            os.close(package_fd)
    finally:
        os.close(parent_fd)
    return _validate_identity_package(files, symbol=symbol, market=market)


def publish_identity_package(
    identities_fd: int,
    *,
    symbol: str,
    market: str,
    response: bytes,
    created_at: str,
) -> tuple[CNInfoSecurityIdentity, dict[str, object]]:
    """Publish exact public identity bytes once, never overwriting a package."""
    parent_fd = _open_identity_parent(identities_fd, market=market, symbol=symbol)
    try:
        if entry_exists(parent_fd, _PACKAGE_NAME):
            return _load_identity_package_at(parent_fd, symbol=symbol, market=market)
        identity = _identity_from_response(response, symbol=symbol, market=market)
        write_frozen_tree_at(
            parent_fd,
            _PACKAGE_NAME,
            _identity_package_files(identity, response=response, created_at=created_at),
            resumable=False,
            label="CNInfo security identity package",
        )
        return _load_identity_package_at(parent_fd, symbol=symbol, market=market)
    finally:
        os.close(parent_fd)


def package_relative_path(market: str, symbol: str) -> str:
    """Return the only permitted relative path for one identity package."""
    return f"{_PACKAGES_DIRECTORY}/{market}/{symbol}/{_PACKAGE_NAME}"


def _load_identity_package_at(
    parent_fd: int,
    *,
    symbol: str,
    market: str,
) -> tuple[CNInfoSecurityIdentity, dict[str, object]]:
    package_fd = open_directory_at(
        parent_fd,
        _PACKAGE_NAME,
        label="CNInfo security identity package",
    )
    try:
        files = read_frozen_tree_at(
            package_fd,
            label="CNInfo security identity package",
        )
    finally:
        os.close(package_fd)
    return _validate_identity_package(files, symbol=symbol, market=market)


def _identity_from_response(
    raw: bytes,
    *,
    symbol: str,
    market: str,
) -> CNInfoSecurityIdentity:
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("CNInfo security identity response is invalid") from error
    if not isinstance(payload, list):
        raise ValueError("CNInfo security identity response is invalid")
    matches = [
        item
        for item in payload
        if isinstance(item, dict) and item.get("code") == symbol
    ]
    if len(matches) != 1:
        raise ValueError("CNInfo security identity is missing or ambiguous")
    org_id = matches[0].get("orgId")
    if not isinstance(org_id, str) or _ORG_ID.fullmatch(org_id) is None:
        raise ValueError("CNInfo security identity orgId is invalid")
    if market_for_symbol(symbol) != market:
        raise ValueError("CNInfo security identity market differs from symbol")
    return CNInfoSecurityIdentity(symbol=symbol, market=market, org_id=org_id)


def _identity_package_files(
    identity: CNInfoSecurityIdentity,
    *,
    response: bytes,
    created_at: str,
) -> dict[str, bytes]:
    request = {
        "schema_version": _SCHEMA_VERSION,
        "endpoint": OFFICIAL_SECURITY_IDENTITY_ENDPOINT,
        "method": "POST",
        "form": identity_form(identity.symbol),
    }
    request_bytes = canonical_json_bytes(request)
    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "role": "cninfo_security_identity",
        "identity": {
            "symbol": identity.symbol,
            "market": identity.market,
            "org_id": identity.org_id,
        },
        "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
        "response_sha256": hashlib.sha256(response).hexdigest(),
        "response_size_bytes": len(response),
        "completed": True,
        "created_at": created_at,
    }
    return {
        "request.json": request_bytes,
        "response.json": response,
        "identity_manifest.json": canonical_json_bytes(manifest),
    }


def _validate_identity_package(
    files: Mapping[str, bytes],
    *,
    symbol: str,
    market: str,
) -> tuple[CNInfoSecurityIdentity, dict[str, object]]:
    if set(files) != {"request.json", "response.json", "identity_manifest.json"}:
        raise ValueError("CNInfo security identity package inventory is invalid")
    request = _canonical_object(files["request.json"], label="CNInfo identity request")
    expected_request = {
        "schema_version": _SCHEMA_VERSION,
        "endpoint": OFFICIAL_SECURITY_IDENTITY_ENDPOINT,
        "method": "POST",
        "form": identity_form(symbol),
    }
    if request != expected_request:
        raise ValueError("CNInfo security identity request differs from symbol")
    identity = _identity_from_response(files["response.json"], symbol=symbol, market=market)
    manifest = _canonical_object(
        files["identity_manifest.json"],
        label="CNInfo security identity manifest",
    )
    expected_manifest = {
        "schema_version": _SCHEMA_VERSION,
        "role": "cninfo_security_identity",
        "identity": {
            "symbol": identity.symbol,
            "market": identity.market,
            "org_id": identity.org_id,
        },
        "request_sha256": hashlib.sha256(files["request.json"]).hexdigest(),
        "response_sha256": hashlib.sha256(files["response.json"]).hexdigest(),
        "response_size_bytes": len(files["response.json"]),
        "completed": True,
        "created_at": manifest.get("created_at"),
    }
    if not isinstance(manifest.get("created_at"), str) or not manifest["created_at"]:
        raise ValueError("CNInfo security identity manifest is invalid")
    if manifest != expected_manifest:
        raise ValueError("CNInfo security identity manifest differs from package")
    return identity, {
        "symbol": identity.symbol,
        "market": identity.market,
        "org_id": identity.org_id,
        "relative_path": package_relative_path(identity.market, identity.symbol),
        "request_sha256": hashlib.sha256(files["request.json"]).hexdigest(),
        "response_sha256": hashlib.sha256(files["response.json"]).hexdigest(),
        "manifest_sha256": hashlib.sha256(files["identity_manifest.json"]).hexdigest(),
    }


def _open_identity_parent(identities_fd: int, *, market: str, symbol: str) -> int:
    descriptor = identities_fd
    opened: list[int] = []
    try:
        for name in (_PACKAGES_DIRECTORY, market, symbol):
            try:
                os.mkdir(name, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = open_directory_at(
                descriptor,
                name,
                label="CNInfo security identity parent",
            )
            if descriptor != identities_fd:
                opened.append(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        if descriptor != identities_fd:
            os.close(descriptor)
        raise
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


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
