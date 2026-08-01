from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import date, datetime, time
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from ashare_multifactor.audit.records import sha256_file
from ashare_multifactor.final_test.action_source_contract import (
    load_action_source_contract,
)
from ashare_multifactor.final_test.official_announcement_catalog import (
    build_announcement_catalog,
    load_verified_announcement_catalog,
)
from ashare_multifactor.final_test.official_candidate_query_supplements import (
    CandidateQuerySupplementPackage,
    collect_authorized_candidate_query_package,
    discover_candidate_query_supplement,
    load_verified_candidate_query_supplement,
    merge_candidate_query_supplement_rows,
    publish_candidate_query_supplement,
)
from ashare_multifactor.final_test.official_candidate_query_authorization import (
    CANDIDATE_QUERY_COVERAGE_DIRECTORY,
)
from ashare_multifactor.final_test.official_candidate_query_topology import (
    load_verified_candidate_query_topology,
    publish_candidate_query_topology,
)
from ashare_multifactor.final_test.official_candidate_query_supplement_basis import (
    load_candidate_query_basis,
)
from ashare_multifactor.final_test.official_candidate_review_admission import (
    CandidateReviewAdmissionError,
    require_candidate_review_admission,
)
from ashare_multifactor.final_test.official_query_client import RetryPolicy
from ashare_multifactor.final_test.official_query_coverage import (
    OfficialQueryScope,
    canonical_json_bytes,
)
from ashare_multifactor.final_test.official_query_packages import (
    official_query_request_sha256,
    package_relative_path,
)
from ashare_multifactor.final_test.preparation import verify_preparation
from ashare_multifactor.final_test.resume import load_registered_authorization
from test_final_test_official_candidate_review_admission import (
    _DATE_RULE_PATH,
    _admission_inputs,
    _pdf_bytes,
)
from test_final_test_resume import PreparedAttempt


_CONTRACT_PATH = (
    Path(__file__).parents[1] / "configs/final_execution_sources.yaml"
)
_REAL_04S_ADMISSION = Path(
    "/Users/mikasa/本科时期/Projects/repository1/processed/"
    "final_test_evidence/stage9-final-20260730-a23e854/"
    "candidate_review_admissions/"
    "3db25cc4c39b0e2a7bccf102648bbf6e7fd5b96cb3ba80b7a45cb193f9623e05/"
    "candidate_review_admission.json"
)
pytest_plugins = ("test_final_test_resume",)


@dataclass(frozen=True)
class _Basis:
    source_root: Path
    root: Path
    candidate_manifest: Path
    base_coverage: Path
    base_catalog: Path
    blocked_admission: Path
    candidate_id: str
    symbol: str
    market: str
    ex_date: date


class _OneAnnouncementTransport:
    def __init__(
        self,
        announcement: dict[str, object],
        *,
        authorization_path: Path,
    ) -> None:
        self.announcement = announcement
        self.authorization_path = authorization_path
        self.call_count = 0

    def fetch(
        self,
        endpoint: str,
        form: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> bytes:
        del endpoint, form, timeout_seconds
        assert self.authorization_path.is_file()
        self.call_count += 1
        return json.dumps(
            {
                "totalpages": 1,
                "totalAnnouncement": 1,
                "announcements": [self.announcement],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()


def _basis(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
    *,
    candidate_title: str = "2024年第三季度报告",
    candidate_payload: bytes | None = None,
) -> _Basis:
    workspace, queue, candidates = _admission_inputs(
        prepared_attempt,
        titles={
            "000001": candidate_title,
            "600000": "2024年第三季度报告",
        },
        announcement_dates={
            "000001": date(2024, 5, 28),
            "600000": date(2024, 6, 18),
        },
        document_payloads={
            "000001": candidate_payload or _pdf_bytes("2023-06-01"),
            "600000": _pdf_bytes("2024-07-01"),
        },
        candidate_symbols={"000001"},
    )
    with pytest.raises(CandidateReviewAdmissionError) as raised:
        require_candidate_review_admission(
            workspace=workspace,
            queue=queue,
            candidates=candidates,
            date_rule_path=_DATE_RULE_PATH,
        )
    source_root = workspace.root.parent
    basis_root = tmp_path / "basis" / source_root.name
    basis_root.parent.mkdir(parents=True)
    shutil.copytree(source_root, basis_root)
    candidate = candidates.candidates.row(0, named=True)
    return _Basis(
        source_root=source_root,
        root=basis_root,
        candidate_manifest=(
            basis_root
            / candidates.manifest_path.relative_to(source_root)
        ),
        base_coverage=(
            basis_root
            / "official_query_coverage/official_query_coverage.json"
        ),
        base_catalog=(
            basis_root / workspace.catalog_path.relative_to(source_root)
        ),
        blocked_admission=(
            basis_root
            / raised.value.manifest_path.relative_to(source_root)
        ),
        candidate_id=str(candidate["candidate_id"]),
        symbol=str(candidate["symbol"]),
        market="sz",
        ex_date=candidate["ex_date"],
    )


def _default_announcement() -> dict[str, object]:
    publication_date = date(2024, 5, 30)
    timestamp = int(
        datetime.combine(
            publication_date,
            time.min,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ).timestamp()
        * 1000
    )
    return {
        "announcementId": "1214041769",
        "announcementTitle": "2023年年度权益分派实施公告",
        "announcementTime": timestamp,
        "adjunctUrl": "finalpage/2024-05-30/1214041769.PDF",
    }


def _announcement_from_base(
    basis: _Basis,
    *,
    drift_title: bool,
) -> dict[str, object]:
    row = (
        load_verified_announcement_catalog(basis.base_catalog)
        .frame.filter(pl.col("symbol") == basis.symbol)
        .row(0, named=True)
    )
    return {
        "announcementId": row["announcement_id"],
        "announcementTitle": (
            "different title"
            if drift_title
            else row["announcement_title"]
        ),
        "announcementTime": int(row["announcement_time_raw"]),
        "adjunctUrl": str(row["source_url"]).removeprefix(
            "https://static.cninfo.com.cn/"
        ),
    }


def _package(
    *,
    basis: _Basis,
    destination: Path,
    candidate_manifest_path: Path,
    authorization_path: Path,
    request_id: str,
    scope: OfficialQueryScope,
    coverage_root: Path,
    announcement: dict[str, object] | None = None,
) -> tuple[CandidateQuerySupplementPackage, _OneAnnouncementTransport]:
    transport = _OneAnnouncementTransport(
        announcement or _default_announcement(),
        authorization_path=authorization_path,
    )
    descriptor = os.open(
        coverage_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        package = collect_authorized_candidate_query_package(
            destination=destination,
            candidate_manifest_path=candidate_manifest_path,
            blocked_admission_path=basis.blocked_admission,
            network_authorization_path=authorization_path,
            request_id=request_id,
            coverage_fd=descriptor,
            coverage_root=coverage_root,
            transport=transport,
            policy=RetryPolicy(
                attempts=1,
                timeout_seconds=1,
                minimum_interval_seconds=0,
            ),
            created_at="2026-07-30T00:00:00Z",
            contract=load_action_source_contract(_CONTRACT_PATH),
        )
    finally:
        os.close(descriptor)
    assert package.package_path == coverage_root / package_relative_path(scope)
    return package, transport


def _scope_record(scope: OfficialQueryScope) -> dict[str, object]:
    return {
        "symbol": scope.symbol,
        "market": scope.market,
        "category": scope.category,
        "query_category": scope.query_category,
        "start": scope.start.isoformat(),
        "end": scope.end.isoformat(),
        "org_id": scope.org_id,
    }


def _request_id(
    *,
    candidate_ids: tuple[str, ...],
    scope: OfficialQueryScope,
    request_sha256: str,
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "candidate_ids": list(candidate_ids),
                "scope": _scope_record(scope),
                "request_sha256": request_sha256,
            }
        )
    ).hexdigest()


def _authorization(
    tmp_path: Path,
    *,
    name: str,
    destination: Path,
    basis: _Basis,
    candidate_manifest_path: Path,
    scope: OfficialQueryScope,
    candidate_ids: tuple[str, ...],
    coverage_root: Path,
    topology_manifest_path: Path,
    mutate: callable | None = None,
) -> tuple[Path, str]:
    admission = json.loads(basis.blocked_admission.read_text())
    request_sha256 = official_query_request_sha256(scope)
    request_id = _request_id(
        candidate_ids=candidate_ids,
        scope=scope,
        request_sha256=request_sha256,
    )
    payload = {
        "schema": "stage9_candidate_query_network_authorization/v1",
        "role": "stage9_candidate_query_network_authorization",
        "attempt_id": destination.name,
        "candidate_snapshot_manifest_sha256": sha256_file(
            candidate_manifest_path
        ),
        "basis": {
            "attempt_id": basis.root.name,
            "candidate_snapshot_manifest_sha256": sha256_file(
                basis.candidate_manifest
            ),
            "base_coverage_sha256": sha256_file(basis.base_coverage),
            "base_catalog_sha256": sha256_file(basis.base_catalog),
            "blocked_admission_manifest_sha256": sha256_file(
                basis.blocked_admission
            ),
            "blocked_unresolved_sha256": admission[
                "unresolved_candidates"
            ]["sha256"],
        },
        "causal_topology": {
            "path": str(topology_manifest_path.absolute()),
            "sha256": sha256_file(topology_manifest_path),
            "size_bytes": topology_manifest_path.stat().st_size,
        },
        "coverage_root": str(coverage_root.absolute()),
        "method": "POST",
        "max_attempts_per_request": 3,
        "requests": [
            {
                "request_id": request_id,
                "candidate_ids": list(candidate_ids),
                "scope": _scope_record(scope),
                "request_sha256": request_sha256,
            }
        ],
        "final_test_strategy_outputs_read": False,
    }
    if mutate is not None:
        mutate(payload)
    path = tmp_path / f"authorization-{name}.json"
    path.write_bytes(canonical_json_bytes(payload))
    return path, request_id


def _destination_candidate_snapshot(
    basis: _Basis,
    destination: Path,
    *,
    mutate_candidate: callable | None = None,
) -> Path:
    source = basis.candidate_manifest.parent
    target = destination / "corporate_action_candidate_collection/snapshot"
    if target.exists():
        return target / "manifest.json"
    shutil.copytree(source, target)
    candidates_path = target / "candidates.parquet"
    if mutate_candidate is not None:
        candidates = mutate_candidate(pl.read_parquet(candidates_path))
        candidates.write_parquet(candidates_path, compression="zstd")
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["attempt_id"] = destination.name
    for record in manifest["files"]:
        path = target / record["path"]
        record["sha256"] = sha256_file(path)
        record["size_bytes"] = path.stat().st_size
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    return manifest_path


def _publish_from_basis(
    tmp_path: Path,
    *,
    name: str,
    basis: _Basis,
    candidate_ids: tuple[str, ...] | None = None,
    mutate_authorization: callable | None = None,
    mutate_candidate: callable | None = None,
    announcement: dict[str, object] | None = None,
    destination: Path | None = None,
):
    target = destination or tmp_path / f"stage9-final-successor-{name}"
    target.mkdir(exist_ok=True)
    candidate_manifest_path = _destination_candidate_snapshot(
        basis,
        target,
        mutate_candidate=mutate_candidate,
    )
    scope = OfficialQueryScope(
        symbol=basis.symbol,
        market=basis.market,
        category="announcements",
        query_category="",
        start=date(2024, 5, 1),
        end=date(2024, 6, 30),
        org_id=f"fixture-{basis.symbol}",
    )
    bound_ids = candidate_ids or (basis.candidate_id,)
    coverage_root = target / CANDIDATE_QUERY_COVERAGE_DIRECTORY
    coverage_root.mkdir()
    topology = publish_candidate_query_topology(
        destination=target,
        candidate_manifest_path=candidate_manifest_path,
        blocked_admission_path=basis.blocked_admission,
        contract=load_action_source_contract(_CONTRACT_PATH),
    )
    authorization_path, request_id = _authorization(
        tmp_path,
        name=name,
        destination=target,
        basis=basis,
        candidate_manifest_path=candidate_manifest_path,
        scope=scope,
        candidate_ids=bound_ids,
        coverage_root=coverage_root,
        topology_manifest_path=topology.manifest_path,
        mutate=mutate_authorization,
    )
    package, transport = _package(
        basis=basis,
        destination=target,
        candidate_manifest_path=candidate_manifest_path,
        authorization_path=authorization_path,
        request_id=request_id,
        scope=scope,
        coverage_root=coverage_root,
        announcement=announcement,
    )
    supplement = publish_candidate_query_supplement(
        destination=target,
        candidate_manifest_path=candidate_manifest_path,
        blocked_admission_path=basis.blocked_admission,
        network_authorization_path=authorization_path,
        packages=(package,),
        contract=load_action_source_contract(_CONTRACT_PATH),
    )
    return supplement, target, authorization_path, transport


def test_candidate_query_topology_is_content_addressed_and_replayed(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    basis = _basis(prepared_attempt, tmp_path)
    destination = tmp_path / "stage9-final-topology"
    destination.mkdir()
    candidate_manifest = _destination_candidate_snapshot(basis, destination)

    topology = publish_candidate_query_topology(
        destination=destination,
        candidate_manifest_path=candidate_manifest,
        blocked_admission_path=basis.blocked_admission,
        contract=load_action_source_contract(_CONTRACT_PATH),
    )
    replay = load_verified_candidate_query_topology(
        topology.manifest_path,
        contract=load_action_source_contract(_CONTRACT_PATH),
    )

    assert topology.manifest_path.parent.name == topology.manifest_sha256
    assert replay.manifest_sha256 == topology.manifest_sha256
    assert replay.query_required_candidate_ids == (basis.candidate_id,)
    assert replay.rendition_required_candidate_ids == ()
    assert replay.unclassified_candidate_ids == ()
    assert set(replay.unresolved.get_column("candidate_id")) == (
        set(replay.query_required_candidate_ids)
        | set(replay.rendition_required_candidate_ids)
        | set(replay.unclassified_candidate_ids)
    )


def test_candidate_query_authorization_rejects_unclassified_topology(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    basis = _basis(
        prepared_attempt,
        tmp_path,
        candidate_title="关于2024年度权益分派的公告",
        candidate_payload=_pdf_bytes("weak metadata and no implementation"),
    )

    with pytest.raises(ValueError, match="authorization basis"):
        _publish_from_basis(
            tmp_path,
            name="unclassified",
            basis=basis,
        )


def test_candidate_query_supplement_revalidates_semantic_basis_and_packages(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    basis = _basis(prepared_attempt, tmp_path)
    supplement, _, _, transport = _publish_from_basis(
        tmp_path,
        name="valid",
        basis=basis,
    )
    assert transport.call_count == 1

    assert supplement.manifest_path.parent.name == supplement.manifest_sha256
    assert supplement.manifest["package_count"] == 1
    assert supplement.manifest["derived_row_count"] == 1
    assert supplement.manifest["packages"][0]["candidate_ids"] == [
        basis.candidate_id
    ]
    assert supplement.catalog.get_column("symbol").to_list() == [
        basis.symbol
    ]

    replay = load_verified_candidate_query_supplement(
        supplement.manifest_path,
        contract=load_action_source_contract(_CONTRACT_PATH),
    )
    discovered = discover_candidate_query_supplement(
        supplement.root.parent.parent,
        contract=load_action_source_contract(_CONTRACT_PATH),
    )
    assert replay.manifest_sha256 == supplement.manifest_sha256
    assert replay.catalog.equals(supplement.catalog)
    assert discovered is not None
    assert discovered.manifest_sha256 == supplement.manifest_sha256


def test_candidate_query_supplement_rejects_bound_file_drift(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    basis = _basis(prepared_attempt, tmp_path)
    supplement, _, authorization, _ = _publish_from_basis(
        tmp_path,
        name="drift",
        basis=basis,
    )
    authorization.write_bytes(b"changed")

    with pytest.raises(ValueError, match="binding"):
        load_verified_candidate_query_supplement(
            supplement.manifest_path,
            contract=load_action_source_contract(_CONTRACT_PATH),
        )


def test_candidate_query_supplement_rejects_nonsemantic_authorization(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    mutations = (
        (
            ("f" * 24,),
            None,
        ),
        (
            None,
            lambda value: value["basis"].__setitem__(
                "candidate_snapshot_manifest_sha256",
                "0" * 64,
            ),
        ),
        (
            None,
            lambda value: value.__setitem__("method", "GET"),
        ),
        (
            None,
            lambda value: value.__setitem__(
                "max_attempts_per_request",
                0,
            ),
        ),
        (
            None,
            lambda value: value.__setitem__(
                "final_test_strategy_outputs_read",
                True,
            ),
        ),
        (
            None,
            lambda value: value["requests"][0]["scope"].__setitem__(
                "market",
                "sh",
            ),
        ),
        (
            None,
            lambda value: value["requests"][0].__setitem__(
                "request_sha256",
                "0" * 64,
            ),
        ),
    )
    for index, (candidate_ids, mutation) in enumerate(mutations):
        basis = _basis(prepared_attempt, tmp_path / f"case-{index}")
        with pytest.raises(ValueError, match="candidate|authorization|scope"):
            _publish_from_basis(
                tmp_path / f"case-{index}",
                name=f"invalid-{index}",
                basis=basis,
                candidate_ids=candidate_ids,
                mutate_authorization=mutation,
            )


def test_candidate_query_authorization_is_checked_before_transport(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    basis = _basis(prepared_attempt, tmp_path)
    destination = tmp_path / "stage9-final-preauthorized"
    destination.mkdir()
    candidate_manifest = _destination_candidate_snapshot(
        basis,
        destination,
    )
    scope = OfficialQueryScope(
        symbol=basis.symbol,
        market=basis.market,
        category="announcements",
        query_category="",
        start=date(2024, 5, 1),
        end=date(2024, 6, 30),
        org_id=f"fixture-{basis.symbol}",
    )
    coverage_root = destination / CANDIDATE_QUERY_COVERAGE_DIRECTORY
    coverage_root.mkdir()
    topology = publish_candidate_query_topology(
        destination=destination,
        candidate_manifest_path=candidate_manifest,
        blocked_admission_path=basis.blocked_admission,
        contract=load_action_source_contract(_CONTRACT_PATH),
    )
    authorization_path, request_id = _authorization(
        tmp_path,
        name="preauthorized",
        destination=destination,
        basis=basis,
        candidate_manifest_path=candidate_manifest,
        scope=scope,
        candidate_ids=(basis.candidate_id,),
        coverage_root=coverage_root,
        topology_manifest_path=topology.manifest_path,
        mutate=lambda value: value.__setitem__("method", "GET"),
    )
    transport = _OneAnnouncementTransport(
        _default_announcement(),
        authorization_path=authorization_path,
    )
    descriptor = os.open(
        coverage_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        with pytest.raises(ValueError, match="authorization"):
            collect_authorized_candidate_query_package(
                destination=destination,
                candidate_manifest_path=candidate_manifest,
                blocked_admission_path=basis.blocked_admission,
                network_authorization_path=authorization_path,
                request_id=request_id,
                coverage_fd=descriptor,
                coverage_root=coverage_root,
                transport=transport,
                policy=RetryPolicy(
                    attempts=1,
                    timeout_seconds=1,
                    minimum_interval_seconds=0,
                ),
                created_at="2026-07-30T00:00:00Z",
                contract=load_action_source_contract(_CONTRACT_PATH),
            )
    finally:
        os.close(descriptor)
    assert transport.call_count == 0


def test_candidate_query_authorization_binds_coverage_root_before_transport(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    basis = _basis(prepared_attempt, tmp_path)
    destination = tmp_path / "stage9-final-coverage-root"
    destination.mkdir()
    candidate_manifest = _destination_candidate_snapshot(basis, destination)
    scope = OfficialQueryScope(
        symbol=basis.symbol,
        market=basis.market,
        category="announcements",
        query_category="",
        start=date(2024, 5, 1),
        end=date(2024, 6, 30),
        org_id=f"fixture-{basis.symbol}",
    )
    authorized_root = destination / CANDIDATE_QUERY_COVERAGE_DIRECTORY
    authorized_root.mkdir()
    different_root = tmp_path / "different-coverage"
    different_root.mkdir()
    authorization_path, request_id = _authorization(
        tmp_path,
        name="coverage-root",
        destination=destination,
        basis=basis,
        candidate_manifest_path=candidate_manifest,
        scope=scope,
        candidate_ids=(basis.candidate_id,),
        coverage_root=authorized_root,
        topology_manifest_path=publish_candidate_query_topology(
            destination=destination,
            candidate_manifest_path=candidate_manifest,
            blocked_admission_path=basis.blocked_admission,
            contract=load_action_source_contract(_CONTRACT_PATH),
        ).manifest_path,
    )
    transport = _OneAnnouncementTransport(
        _default_announcement(),
        authorization_path=authorization_path,
    )
    descriptor = os.open(
        different_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        with pytest.raises(ValueError, match="coverage.*descriptor"):
            collect_authorized_candidate_query_package(
                destination=destination,
                candidate_manifest_path=candidate_manifest,
                blocked_admission_path=basis.blocked_admission,
                network_authorization_path=authorization_path,
                request_id=request_id,
                coverage_fd=descriptor,
                coverage_root=authorized_root,
                transport=transport,
                policy=RetryPolicy(
                    attempts=1,
                    timeout_seconds=1,
                    minimum_interval_seconds=0,
                ),
                created_at="2026-07-30T00:00:00Z",
                contract=load_action_source_contract(_CONTRACT_PATH),
            )
    finally:
        os.close(descriptor)
    assert transport.call_count == 0


def test_candidate_query_basis_rejects_collection_coverage_root_drift(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    basis = _basis(prepared_attempt, tmp_path)
    collection = basis.root / "official_query_collection.json"
    payload = json.loads(collection.read_bytes())
    payload["coverage_relative_path"] = "different"
    collection.write_bytes(canonical_json_bytes(payload))

    with pytest.raises(ValueError, match="collection.*(coverage|identity)"):
        load_candidate_query_basis(
            basis.blocked_admission,
            contract=load_action_source_contract(_CONTRACT_PATH),
        )


def test_candidate_query_supplement_rejects_new_attempt_candidate_drift(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    basis = _basis(prepared_attempt, tmp_path)

    def drift(frame: pl.DataFrame) -> pl.DataFrame:
        return frame.with_columns(
            pl.when(pl.col("candidate_id") == basis.candidate_id)
            .then(pl.lit(9.99))
            .otherwise(pl.col("cash_per_share"))
            .alias("cash_per_share")
        )

    with pytest.raises(ValueError, match="candidate"):
        _publish_from_basis(
            tmp_path,
            name="candidate-drift",
            basis=basis,
            mutate_candidate=drift,
        )


def test_candidate_query_supplement_rejects_uncollected_authorized_request(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    basis = _basis(prepared_attempt, tmp_path)

    def add_request(value: dict[str, object]) -> None:
        request = dict(value["requests"][0])
        scope_record = dict(request["scope"])
        scope_record["start"] = "2024-05-02"
        scope = OfficialQueryScope(
            symbol=str(scope_record["symbol"]),
            market=str(scope_record["market"]),
            category=str(scope_record["category"]),
            query_category=str(scope_record["query_category"]),
            start=date.fromisoformat(str(scope_record["start"])),
            end=date.fromisoformat(str(scope_record["end"])),
            org_id=str(scope_record["org_id"]),
        )
        request_sha256 = official_query_request_sha256(scope)
        request["scope"] = scope_record
        request["request_sha256"] = request_sha256
        request["request_id"] = _request_id(
            candidate_ids=tuple(request["candidate_ids"]),
            scope=scope,
            request_sha256=request_sha256,
        )
        value["requests"].append(request)
        value["requests"].sort(key=lambda item: item["request_id"])

    with pytest.raises(ValueError, match="authorization|package"):
        _publish_from_basis(
            tmp_path,
            name="extra-request",
            basis=basis,
            mutate_authorization=add_request,
        )


def test_candidate_query_supplement_merge_binds_exact_unmerged_base_bytes(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    basis = _basis(prepared_attempt, tmp_path)
    supplement, _, network_authorization, _ = _publish_from_basis(
        tmp_path,
        name="base-identity",
        basis=basis,
    )
    base = load_verified_announcement_catalog(basis.base_catalog).frame

    merged, added_count = merge_candidate_query_supplement_rows(
        base,
        supplement,
    )
    assert added_count == 1
    assert merged.height == base.height + 1

    different_base = base.with_columns(
        pl.lit("different/base/page.json").alias("source_response")
    )
    with pytest.raises(ValueError, match="base catalog identity"):
        merge_candidate_query_supplement_rows(
            different_base,
            supplement,
        )


def test_candidate_query_supplement_deduplicates_without_replacing_base(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    basis = _basis(prepared_attempt, tmp_path)
    supplement, _, _, _ = _publish_from_basis(
        tmp_path,
        name="duplicate",
        basis=basis,
        announcement=_announcement_from_base(
            basis,
            drift_title=False,
        ),
    )
    base = load_verified_announcement_catalog(basis.base_catalog).frame

    merged, added_count = merge_candidate_query_supplement_rows(
        base,
        supplement,
    )
    assert added_count == 0
    assert merged.equals(base)


def test_candidate_query_supplement_rejects_same_key_semantic_drift(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    basis = _basis(prepared_attempt, tmp_path)
    supplement, _, _, _ = _publish_from_basis(
        tmp_path,
        name="semantic-drift",
        basis=basis,
        announcement=_announcement_from_base(
            basis,
            drift_title=True,
        ),
    )
    base = load_verified_announcement_catalog(basis.base_catalog).frame

    with pytest.raises(ValueError, match="semantic"):
        merge_candidate_query_supplement_rows(base, supplement)


def test_candidate_query_supplement_must_precede_fixed_catalog(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    basis = _basis(prepared_attempt, tmp_path)
    destination = tmp_path / "stage9-final-wrong-order"
    (destination / "official_announcement_catalog").mkdir(parents=True)

    with pytest.raises(ValueError, match="before.*catalog|already"):
        _publish_from_basis(
            tmp_path,
            name="wrong-order",
            basis=basis,
            destination=destination,
        )


def test_announcement_catalog_publishes_once_after_candidate_supplement(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    basis = _basis(prepared_attempt, tmp_path)
    destination = basis.source_root
    shutil.rmtree(destination / "official_announcement_catalog")
    supplement, _, network_authorization, _ = _publish_from_basis(
        tmp_path,
        name="correct-order",
        basis=basis,
        destination=destination,
    )
    authorization = load_registered_authorization(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        attempt_id=prepared_attempt.attempt_id,
        approval_key=prepared_attempt.approval_key,
    )
    preparation = verify_preparation(
        prepared_attempt.data_root / "processed/final_test",
        attempt_id=prepared_attempt.attempt_id,
        authorization=authorization,
    )
    catalog_path = build_announcement_catalog(
        destination
        / "official_query_coverage/official_query_coverage.json",
        preparation=preparation,
        authorization=authorization,
        contract=load_action_source_contract(_CONTRACT_PATH),
        destination=destination,
        supplement=supplement,
    )
    catalog = load_verified_announcement_catalog(catalog_path)

    binding = catalog.manifest["candidate_query_supplement"]
    assert binding["manifest_sha256"] == supplement.manifest_sha256
    assert binding["added_row_count"] == 1
    copied_page = next((supplement.root / "packages").rglob("page-*.json"))
    original_page = copied_page.read_bytes()
    copied_page.write_bytes(b'{"changed":true}')
    with pytest.raises(ValueError, match="supplement.*(tree|inventory)"):
        load_verified_announcement_catalog(catalog.path)
    copied_page.write_bytes(original_page)
    assert load_verified_announcement_catalog(catalog.path).catalog_sha256 == (
        catalog.catalog_sha256
    )
    copied_page.unlink()
    with pytest.raises(ValueError, match="supplement.*inventory"):
        load_verified_announcement_catalog(catalog.path)
    copied_page.write_bytes(original_page)
    extra_page = copied_page.with_name("page-9999.json")
    extra_page.write_bytes(b"{}")
    with pytest.raises(ValueError, match="supplement.*inventory"):
        load_verified_announcement_catalog(catalog.path)
    extra_page.unlink()
    for external in (network_authorization, basis.blocked_admission):
        original_external = external.read_bytes()
        external.write_bytes(b"changed")
        with pytest.raises(ValueError, match="supplement.*external binding"):
            load_verified_announcement_catalog(catalog.path)
        external.write_bytes(original_external)
    snapshot_leaf = next(
        Path(str(record["path"]))
        for name, record in supplement.manifest["bindings"].items()
        if name.startswith("tree:candidate_snapshot:")
    )
    original_snapshot_leaf = snapshot_leaf.read_bytes()
    snapshot_leaf.write_bytes(original_snapshot_leaf + b"drift")
    with pytest.raises(ValueError, match="supplement.*external binding"):
        load_verified_announcement_catalog(catalog.path)
    snapshot_leaf.write_bytes(original_snapshot_leaf)
    manifest_path = catalog.root / "catalog_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["candidate_query_supplement"]["relative_path"] = str(
        supplement.manifest_path
    )
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    with pytest.raises(ValueError, match="path is not canonical"):
        load_verified_announcement_catalog(catalog.path)


def test_candidate_query_supplement_has_no_catalog_import_cycle() -> None:
    directory = (
        Path(__file__).parents[1]
        / "src/ashare_multifactor/final_test"
    )
    modules = {
        "official_candidate_query_supplements.py": (
            "ashare_multifactor.final_test.official_announcement_catalog"
        ),
        "official_announcement_catalog.py": (
            "ashare_multifactor.final_test.official_candidate_query_supplements"
        ),
    }
    for filename, forbidden in modules.items():
        imports = {
            node.module
            for node in ast.walk(ast.parse((directory / filename).read_text()))
            if isinstance(node, ast.ImportFrom)
        }
        assert forbidden not in imports


def test_candidate_query_modules_import_in_clean_interpreters() -> None:
    root = Path(__file__).parents[1]
    environment = {**os.environ, "PYTHONPATH": str(root / "src")}
    modules = (
        "ashare_multifactor.final_test.official_announcement_catalog",
        "ashare_multifactor.final_test.official_candidate_query_supplements",
    )
    for order in (modules, tuple(reversed(modules))):
        subprocess.run(
            [
                sys.executable,
                "-c",
                ";".join(f"import {module}" for module in order),
            ],
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )


@pytest.mark.skipif(
    not _REAL_04S_ADMISSION.is_file(),
    reason="immutable Session 04S evidence is not present",
)
def test_real_04s_causal_topology_authorizes_only_query_needed_candidate() -> None:
    basis = load_candidate_query_basis(
        _REAL_04S_ADMISSION,
        contract=load_action_source_contract(_CONTRACT_PATH),
    )

    assert basis.topology.binding["candidate_count"] == 24
    assert basis.topology.binding["admitted_count"] == 22
    assert basis.topology.binding["unresolved_count"] == 2
    assert basis.topology.query_required_candidate_ids == (
        "aedf15a9988572af8bc51b1c",
    )
    assert basis.topology.rendition_required_candidate_ids == (
        "911723770204ecb1cbec0bc2",
    )
    assert basis.topology.unclassified_candidate_ids == ()
    assert set(basis.topology.unresolved.get_column("candidate_id")) == (
        set(basis.topology.query_required_candidate_ids)
        | set(basis.topology.rendition_required_candidate_ids)
        | set(basis.topology.unclassified_candidate_ids)
    )
