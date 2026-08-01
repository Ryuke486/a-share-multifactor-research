"""Byte-only replay of a candidate-query supplement for catalog loading."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from ashare_multifactor.audit.secure_tree import read_frozen_tree_at
from ashare_multifactor.final_test.official_announcement_catalog_schema import (
    SUPPLEMENT_CATALOG_NAME,
    SUPPLEMENT_MANIFEST_NAME,
    SUPPLEMENTS_DIRECTORY,
    supplement_inventory,
)


_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class VerifiedCandidateQuerySupplementSnapshot:
    """Exact supplement bytes and external anchors, without workflow imports."""

    root: Path
    manifest_path: Path
    manifest_sha256: str
    manifest: dict[str, object]
    files: dict[str, bytes]
    inventory_sha256: str
    inventory_file_count: int
    inventory_size_bytes: int


def load_verified_candidate_query_supplement_snapshot(
    destination: Path,
    *,
    manifest_sha256: str,
) -> VerifiedCandidateQuerySupplementSnapshot:
    """Replay the complete copied tree and every explicit external binding."""
    if _SHA256.fullmatch(manifest_sha256) is None:
        raise ValueError("candidate query supplement identity is invalid")
    root = destination / SUPPLEMENTS_DIRECTORY / manifest_sha256
    manifest_path = root / SUPPLEMENT_MANIFEST_NAME
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        complete = read_frozen_tree_at(
            descriptor,
            label="candidate query supplement",
        )
    finally:
        os.close(descriptor)
    manifest_bytes = complete.get(SUPPLEMENT_MANIFEST_NAME, b"")
    manifest = _canonical_object(manifest_bytes)
    if (
        hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha256
        or manifest.get("schema_version") != "1"
        or manifest.get("role") != "official_candidate_query_supplement"
        or manifest.get("attempt_id") != destination.name
    ):
        raise ValueError("candidate query supplement identity is invalid")
    files = {
        path: payload
        for path, payload in complete.items()
        if path != SUPPLEMENT_MANIFEST_NAME
    }
    inventory = supplement_inventory(files)
    if manifest.get("inventory") != inventory:
        raise ValueError("candidate query supplement inventory differs")
    if SUPPLEMENT_CATALOG_NAME not in files:
        raise ValueError("candidate query supplement inventory differs")
    _validate_external_bindings(manifest.get("bindings"))
    return VerifiedCandidateQuerySupplementSnapshot(
        root=root,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        manifest=manifest,
        files=files,
        inventory_sha256=str(inventory["sha256"]),
        inventory_file_count=int(inventory["file_count"]),
        inventory_size_bytes=int(inventory["size_bytes"]),
    )


def _validate_external_bindings(value: object) -> None:
    if not isinstance(value, dict) or not value:
        raise ValueError("candidate query supplement external bindings are invalid")
    for name, record in sorted(value.items()):
        if not isinstance(name, str) or not isinstance(record, dict) or set(record) != {
            "path",
            "sha256",
            "size_bytes",
        }:
            raise ValueError("candidate query supplement external binding is invalid")
        path = Path(str(record["path"]))
        if (
            not path.is_absolute()
            or path.is_symlink()
            or not path.is_file()
            or not stat.S_ISREG(path.stat().st_mode)
            or _SHA256.fullmatch(str(record["sha256"])) is None
            or not isinstance(record["size_bytes"], int)
            or isinstance(record["size_bytes"], bool)
            or record["size_bytes"] < 0
            or any(ancestor.is_symlink() for ancestor in path.parents)
        ):
            raise ValueError("candidate query supplement external binding is unsafe")
        payload = path.read_bytes()
        if (
            len(payload) != record["size_bytes"]
            or hashlib.sha256(payload).hexdigest() != record["sha256"]
        ):
            raise ValueError("candidate query supplement external binding changed")


def _canonical_object(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("candidate query supplement manifest is invalid") from error
    canonical = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    if not isinstance(value, dict) or raw != canonical:
        raise ValueError("candidate query supplement manifest is invalid")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("candidate query supplement manifest has duplicate keys")
        value[key] = item
    return value
