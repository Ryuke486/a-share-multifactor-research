"""Content-addressed causal topology for candidate-only query repair."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re

import polars as pl

from ashare_multifactor.audit.secure_tree import (
    read_frozen_tree_at,
    write_frozen_tree_at,
)
from ashare_multifactor.final_test.action_source_contract import (
    FinalActionSourceContract,
)
from ashare_multifactor.final_test.official_candidate_query_supplement_basis import (
    file_binding,
    load_candidate_query_basis,
    load_successor_candidate_snapshot,
)
from ashare_multifactor.final_test.official_candidate_review_admission_storage import (
    parquet_bytes,
)
from ashare_multifactor.final_test.official_query_collection_root import (
    opened_safe_directory,
)
from ashare_multifactor.final_test.official_query_coverage import (
    canonical_json_bytes,
)


TOPOLOGIES_DIRECTORY = "official_candidate_query_topologies"
TOPOLOGY_MANIFEST_NAME = "topology_manifest.json"
TOPOLOGY_DECISIONS_NAME = "decisions.parquet"
TOPOLOGY_UNRESOLVED_NAME = "unresolved.parquet"

_SCHEMA = "stage9_candidate_query_causal_topology/v1"
_ROLE = "candidate_query_causal_topology"
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class VerifiedCandidateQueryTopology:
    """One replayed topology and its exact mutually exclusive partitions."""

    root: Path
    manifest_path: Path
    manifest_sha256: str
    manifest: dict[str, object]
    decisions: pl.DataFrame
    unresolved: pl.DataFrame
    query_required_candidate_ids: tuple[str, ...]
    rendition_required_candidate_ids: tuple[str, ...]
    unclassified_candidate_ids: tuple[str, ...]


def publish_candidate_query_topology(
    *,
    destination: Path,
    candidate_manifest_path: Path,
    blocked_admission_path: Path,
    contract: FinalActionSourceContract,
) -> VerifiedCandidateQueryTopology:
    """Recompute and freeze the causal classification before authorization."""
    destination = destination.absolute()
    basis = load_candidate_query_basis(
        blocked_admission_path,
        contract=contract,
    )
    successor = load_successor_candidate_snapshot(
        destination=destination,
        candidate_manifest_path=candidate_manifest_path,
    )
    _assert_candidate_identity(basis, successor.candidates)
    decisions_bytes = parquet_bytes(basis.topology.decisions)
    unresolved_bytes = parquet_bytes(basis.topology.unresolved)
    bindings = {
        **basis.bindings,
        "successor_candidate_snapshot": file_binding(
            successor.manifest_path
        ),
    }
    manifest = _manifest(
        destination=destination,
        basis=basis,
        bindings=bindings,
        decisions_bytes=decisions_bytes,
        unresolved_bytes=unresolved_bytes,
    )
    manifest_bytes = canonical_json_bytes(manifest)
    topology_id = hashlib.sha256(manifest_bytes).hexdigest()
    with opened_safe_directory(
        destination,
        label="candidate query topology destination",
    ) as root_fd:
        try:
            os.mkdir(TOPOLOGIES_DIRECTORY, mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        topologies_fd = os.open(
            TOPOLOGIES_DIRECTORY,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        try:
            write_frozen_tree_at(
                topologies_fd,
                topology_id,
                {
                    TOPOLOGY_MANIFEST_NAME: manifest_bytes,
                    TOPOLOGY_DECISIONS_NAME: decisions_bytes,
                    TOPOLOGY_UNRESOLVED_NAME: unresolved_bytes,
                },
                resumable=True,
                label="candidate query topology",
            )
        finally:
            os.close(topologies_fd)
    return load_verified_candidate_query_topology(
        destination
        / TOPOLOGIES_DIRECTORY
        / topology_id
        / TOPOLOGY_MANIFEST_NAME,
        contract=contract,
    )


def load_verified_candidate_query_topology(
    manifest_path: Path,
    *,
    contract: FinalActionSourceContract,
) -> VerifiedCandidateQueryTopology:
    """Replay the complete frozen lineage and current causal classification."""
    absolute = manifest_path.absolute()
    root = absolute.parent
    if (
        absolute.name != TOPOLOGY_MANIFEST_NAME
        or root.parent.name != TOPOLOGIES_DIRECTORY
        or _SHA256.fullmatch(root.name) is None
    ):
        raise ValueError("candidate query topology path is invalid")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        files = read_frozen_tree_at(
            descriptor,
            label="candidate query topology",
        )
    finally:
        os.close(descriptor)
    if set(files) != {
        TOPOLOGY_MANIFEST_NAME,
        TOPOLOGY_DECISIONS_NAME,
        TOPOLOGY_UNRESOLVED_NAME,
    }:
        raise ValueError("candidate query topology inventory differs")
    manifest_bytes = files[TOPOLOGY_MANIFEST_NAME]
    manifest = _canonical_object(manifest_bytes)
    if hashlib.sha256(manifest_bytes).hexdigest() != root.name:
        raise ValueError("candidate query topology identity differs")
    bindings = manifest.get("bindings")
    if not isinstance(bindings, dict):
        raise ValueError("candidate query topology bindings are invalid")
    blocked = _binding_path(bindings.get("blocked_admission"))
    successor_path = _binding_path(
        bindings.get("successor_candidate_snapshot")
    )
    basis = load_candidate_query_basis(blocked, contract=contract)
    destination = root.parent.parent
    successor = load_successor_candidate_snapshot(
        destination=destination,
        candidate_manifest_path=successor_path,
    )
    _assert_candidate_identity(basis, successor.candidates)
    expected_bindings = {
        **basis.bindings,
        "successor_candidate_snapshot": file_binding(
            successor.manifest_path
        ),
    }
    decisions_bytes = parquet_bytes(basis.topology.decisions)
    unresolved_bytes = parquet_bytes(basis.topology.unresolved)
    expected = _manifest(
        destination=destination,
        basis=basis,
        bindings=expected_bindings,
        decisions_bytes=decisions_bytes,
        unresolved_bytes=unresolved_bytes,
    )
    if (
        manifest != expected
        or files[TOPOLOGY_DECISIONS_NAME] != decisions_bytes
        or files[TOPOLOGY_UNRESOLVED_NAME] != unresolved_bytes
    ):
        raise ValueError("candidate query topology replay differs")
    partitions = manifest["partitions"]
    return VerifiedCandidateQueryTopology(
        root=root,
        manifest_path=absolute,
        manifest_sha256=root.name,
        manifest=manifest,
        decisions=basis.topology.decisions,
        unresolved=basis.topology.unresolved,
        query_required_candidate_ids=tuple(
            partitions["query_required_candidate_ids"]
        ),
        rendition_required_candidate_ids=tuple(
            partitions["rendition_required_candidate_ids"]
        ),
        unclassified_candidate_ids=tuple(
            partitions["unclassified_candidate_ids"]
        ),
    )


def _manifest(
    *,
    destination: Path,
    basis,
    bindings: dict[str, dict[str, object]],
    decisions_bytes: bytes,
    unresolved_bytes: bytes,
) -> dict[str, object]:
    topology = basis.topology
    partitions = {
        "query_required_candidate_ids": list(
            topology.query_required_candidate_ids
        ),
        "rendition_required_candidate_ids": list(
            topology.rendition_required_candidate_ids
        ),
        "unclassified_candidate_ids": list(
            topology.unclassified_candidate_ids
        ),
    }
    _assert_partition(topology.unresolved, partitions)
    return {
        "schema": _SCHEMA,
        "role": _ROLE,
        "attempt_id": destination.name,
        "basis_attempt_id": basis.root.name,
        "bindings": bindings,
        "candidate_count": int(topology.binding["candidate_count"]),
        "admitted_count": int(topology.binding["admitted_count"]),
        "unresolved_count": int(topology.binding["unresolved_count"]),
        "decisions": _payload_record(
            TOPOLOGY_DECISIONS_NAME,
            decisions_bytes,
            topology.decisions.height,
        ),
        "unresolved": _payload_record(
            TOPOLOGY_UNRESOLVED_NAME,
            unresolved_bytes,
            topology.unresolved.height,
        ),
        "partitions": partitions,
        "base_coverage_root": str(
            basis.base_coverage_path.parent.absolute()
        ),
        "final_test_strategy_outputs_read": False,
    }


def _assert_candidate_identity(basis, successor: pl.DataFrame) -> None:
    old = {
        str(row["candidate_id"]): row
        for row in basis.candidates.candidates.iter_rows(named=True)
    }
    new = {
        str(row["candidate_id"]): row
        for row in successor.iter_rows(named=True)
    }
    for candidate_id in basis.topology.binding["candidate_ids"]:
        if old.get(candidate_id) != new.get(candidate_id):
            raise ValueError("candidate query topology successor candidate differs")


def _assert_partition(
    unresolved: pl.DataFrame,
    partitions: dict[str, list[str]],
) -> None:
    values = [set(value) for value in partitions.values()]
    if (
        any(values[index].intersection(values[other])
            for index in range(len(values))
            for other in range(index + 1, len(values)))
        or set(unresolved.get_column("candidate_id"))
        != set().union(*values)
    ):
        raise ValueError("candidate query topology partition differs")


def _payload_record(
    path: str,
    payload: bytes,
    row_count: int,
) -> dict[str, object]:
    return {
        "path": path,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "row_count": row_count,
    }


def _binding_path(value: object) -> Path:
    if not isinstance(value, dict):
        raise ValueError("candidate query topology binding is invalid")
    path = Path(str(value.get("path", "")))
    if file_binding(path) != value:
        raise ValueError("candidate query topology binding changed")
    return path


def _canonical_object(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("candidate query topology manifest is invalid") from error
    if not isinstance(value, dict) or raw != canonical_json_bytes(value):
        raise ValueError("candidate query topology manifest is invalid")
    return value
