"""Resumable, preparation-bound BaoStock corporate-action candidates."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path

import polars as pl

from ashare_multifactor.audit.secure_tree import (
    read_frozen_tree_at,
    write_frozen_tree_at,
)
from ashare_multifactor.final_test.action_source_contract import FinalActionSourceContract
from ashare_multifactor.final_test.corporate_action_candidate_contract import (
    normalize_corporate_action_candidates,
)
from ashare_multifactor.final_test.corporate_action_candidate_packages import (
    CandidateQueryScope,
    candidate_query_package_exists,
    candidate_query_package_id,
    expected_candidate_query_scopes,
    read_candidate_query_package,
    validate_candidate_query_response,
    write_candidate_query_package,
)
from ashare_multifactor.final_test.final_root_binding import FinalRootBinding
from ashare_multifactor.final_test.gate import (
    FINAL_TEST_END,
    FINAL_TEST_START,
    FinalTestAuthorization,
)
from ashare_multifactor.final_test.official_query_collection_root import (
    CANDIDATES_DIRECTORY,
    assert_directory_identities,
    collection_lock,
    expected_output_root,
    open_evidence_root,
    write_or_verify_collection_manifest,
)
from ashare_multifactor.final_test.official_query_coverage import canonical_json_bytes
from ashare_multifactor.final_test.preparation import (
    FinalTestPreparation,
    verify_preparation,
)
from ashare_multifactor.final_test.recovery_secure_fs import open_directory_at


QueryDividend = Callable[[str, int, str], tuple[list[str], list[list[str]]]]

QUERIES_DIRECTORY = "queries"
SNAPSHOT_DIRECTORY = "snapshot"
RAW_NAME = "raw_candidates.parquet"
COVERAGE_NAME = "query_coverage.parquet"
CANDIDATES_NAME = "candidates.parquet"
MANIFEST_NAME = "manifest.json"

_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class CorporateActionCandidateCollection:
    """Progress or a complete immutable candidate snapshot."""

    root: Path
    completed_queries: int
    total_queries: int
    manifest_path: Path | None
    raw_path: Path | None
    query_coverage_path: Path | None
    candidates_path: Path | None


def collect_corporate_action_candidates(
    *,
    preparation: FinalTestPreparation,
    authorization: FinalTestAuthorization,
    contract: FinalActionSourceContract,
    output_root: Path,
    query: QueryDividend,
    max_queries: int | None = None,
) -> CorporateActionCandidateCollection:
    """Validate and freeze each symbol-year response before publishing a snapshot."""
    if max_queries is not None and (
        not isinstance(max_queries, int)
        or isinstance(max_queries, bool)
        or max_queries <= 0
    ):
        raise ValueError("corporate-action candidate maximum queries is invalid")
    _assert_authorization(authorization)
    if authorization.attempt_id != preparation.attempt_id:
        raise ValueError("candidate preparation differs from authorization")
    final_root = _final_root_from_preparation(preparation)
    expected_root = expected_output_root(final_root, authorization.attempt_id)
    if output_root.absolute() != expected_root.absolute():
        raise ValueError("candidate output root differs from the attempt root")

    with FinalRootBinding.open(final_root) as root_binding:
        verified = verify_preparation(
            final_root,
            attempt_id=authorization.attempt_id,
            authorization=authorization,
            root_binding=root_binding,
        )
        if preparation != verified:
            raise ValueError("candidate preparation identity differs")
        symbols = _load_bound_symbol_scope(verified)
        scopes = expected_candidate_query_scopes(symbols, contract)
        with open_evidence_root(
            root_binding,
            attempt_id=authorization.attempt_id,
            include_candidates=True,
        ) as evidence:
            with collection_lock(evidence.root_fd):
                write_or_verify_collection_manifest(
                    evidence.root_fd,
                    preparation=verified,
                    authorization=authorization,
                    contract=contract,
                )
                if evidence.candidates_fd is None:
                    raise AssertionError("candidate collection descriptor is absent")
                result = _collect_bound(
                    evidence.candidates_fd,
                    collection_root=evidence.output_root / CANDIDATES_DIRECTORY,
                    scopes=scopes,
                    symbols=symbols,
                    preparation=verified,
                    authorization=authorization,
                    contract=contract,
                    query=query,
                    max_queries=max_queries,
                )
                assert_directory_identities(
                    root_binding,
                    evidence,
                    attempt_id=authorization.attempt_id,
                )
                return result


def collect_baostock_corporate_action_candidates(
    *,
    preparation: FinalTestPreparation,
    authorization: FinalTestAuthorization,
    contract: FinalActionSourceContract,
    output_root: Path,
    max_queries: int | None = None,
) -> CorporateActionCandidateCollection:
    """Use the frozen BaoStock API contract to collect candidate facts."""
    try:
        import baostock as bs
    except ImportError as error:  # pragma: no cover - environment failure
        raise RuntimeError("baostock is required for corporate-action candidates") from error
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login.error_msg}")

    def query(
        code: str,
        year: int,
        year_type: str,
    ) -> tuple[list[str], list[list[str]]]:
        result = bs.query_dividend_data(
            code=code,
            year=str(year),
            yearType=year_type,
        )
        if result.error_code != "0":
            raise RuntimeError(
                f"BaoStock dividend query failed: {code} {year} {result.error_msg}"
            )
        rows: list[list[str]] = []
        while result.next():
            rows.append(result.get_row_data())
        return list(result.fields), rows

    try:
        return collect_corporate_action_candidates(
            preparation=preparation,
            authorization=authorization,
            contract=contract,
            output_root=output_root,
            query=query,
            max_queries=max_queries,
        )
    finally:
        bs.logout()


def _collect_bound(
    collection_fd: int,
    *,
    collection_root: Path,
    scopes: tuple[CandidateQueryScope, ...],
    symbols: list[str],
    preparation: FinalTestPreparation,
    authorization: FinalTestAuthorization,
    contract: FinalActionSourceContract,
    query: QueryDividend,
    max_queries: int | None,
) -> CorporateActionCandidateCollection:
    queries_fd = _open_or_create_directory(
        collection_fd,
        QUERIES_DIRECTORY,
        label="corporate-action candidate queries",
    )
    try:
        packages: list[tuple[CandidateQueryScope, list[str], list[list[str]]]] = []
        known_fields: list[str] | None = None
        fetched = 0
        for scope in scopes:
            package_id = candidate_query_package_id(scope)
            if candidate_query_package_exists(queries_fd, package_id):
                fields, rows = read_candidate_query_package(
                    queries_fd,
                    package_id=package_id,
                    scope=scope,
                )
            else:
                if max_queries is not None and fetched >= max_queries:
                    break
                fields, rows = query(scope.code, scope.year, scope.year_type)
                validate_candidate_query_response(fields, rows, scope=scope)
                write_candidate_query_package(
                    queries_fd,
                    package_id=package_id,
                    scope=scope,
                    fields=fields,
                    rows=rows,
                )
                fetched += 1
            if known_fields is None:
                known_fields = fields
            elif fields != known_fields:
                raise ValueError("BaoStock dividend response schema changed")
            packages.append((scope, fields, rows))
        completed = len(packages)
        if completed != len(scopes):
            return _result(
                collection_root,
                completed=completed,
                total=len(scopes),
                complete=False,
            )
        files = _snapshot_files(
            packages,
            symbols=symbols,
            preparation=preparation,
            authorization=authorization,
            contract=contract,
        )
        write_frozen_tree_at(
            collection_fd,
            SNAPSHOT_DIRECTORY,
            files,
            resumable=True,
            label="corporate-action candidate snapshot",
        )
        _validate_snapshot(
            collection_fd,
            files=files,
            symbols=symbols,
            scopes=scopes,
        )
        return _result(
            collection_root,
            completed=completed,
            total=len(scopes),
            complete=True,
        )
    finally:
        os.close(queries_fd)


def _snapshot_files(
    packages: list[tuple[CandidateQueryScope, list[str], list[list[str]]]],
    *,
    symbols: list[str],
    preparation: FinalTestPreparation,
    authorization: FinalTestAuthorization,
    contract: FinalActionSourceContract,
) -> dict[str, bytes]:
    fields = packages[0][1]
    raw_rows = [
        {
            **dict(zip(package_fields, values, strict=True)),
            "query_year": scope.year,
            "query_year_type": scope.year_type,
        }
        for scope, package_fields, rows in packages
        for values in rows
    ]
    schema = {field: pl.String for field in fields}
    schema.update({"query_year": pl.Int64, "query_year_type": pl.String})
    raw = pl.DataFrame(raw_rows, schema=schema, strict=False)
    coverage = pl.DataFrame(
        [
            {
                "symbol": scope.symbol,
                "year": scope.year,
                "status": "ok",
                "row_count": len(rows),
            }
            for scope, _package_fields, rows in packages
        ],
        schema={
            "symbol": pl.String,
            "year": pl.Int64,
            "status": pl.String,
            "row_count": pl.Int64,
        },
    ).sort("symbol", "year")
    candidates = normalize_corporate_action_candidates(raw, symbols=symbols)
    data = {
        RAW_NAME: _parquet_bytes(raw),
        COVERAGE_NAME: _parquet_bytes(coverage),
        CANDIDATES_NAME: _parquet_bytes(candidates),
    }
    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "role": "baostock_corporate_action_candidate_snapshot",
        "status": "complete",
        "attempt_id": authorization.attempt_id,
        "approval_id": authorization.approval_id,
        "git": {"commit": authorization.git_commit, "tree": authorization.git_tree},
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "robustness_release": authorization.robustness_release,
        "prepare_manifest_sha256": preparation.manifest_sha256,
        "period": [contract.start.isoformat(), contract.end.isoformat()],
        "provider": contract.provider,
        "query_year_type": contract.query_year_type,
        "query_years": list(contract.query_years),
        "symbol_count": len(symbols),
        "symbols_sha256": preparation.symbols_sha256,
        "successful_query_count": len(packages),
        "raw_row_count": raw.height,
        "candidate_count": candidates.height,
        "files": [
            {
                "path": name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
            for name, payload in sorted(data.items())
        ],
    }
    return {**data, MANIFEST_NAME: canonical_json_bytes(manifest)}


def _validate_snapshot(
    collection_fd: int,
    *,
    files: dict[str, bytes],
    symbols: list[str],
    scopes: tuple[CandidateQueryScope, ...],
) -> None:
    snapshot_fd = open_directory_at(
        collection_fd,
        SNAPSHOT_DIRECTORY,
        label="corporate-action candidate snapshot",
    )
    try:
        actual = read_frozen_tree_at(
            snapshot_fd,
            label="corporate-action candidate snapshot",
        )
    finally:
        os.close(snapshot_fd)
    if actual != files:
        raise ValueError("corporate-action candidate snapshot changed")
    try:
        manifest = json.loads(actual[MANIFEST_NAME])
        coverage = pl.read_parquet(BytesIO(actual[COVERAGE_NAME]))
        raw = pl.read_parquet(BytesIO(actual[RAW_NAME]))
        candidates = pl.read_parquet(BytesIO(actual[CANDIDATES_NAME]))
    except (KeyError, json.JSONDecodeError, pl.exceptions.PolarsError) as error:
        raise ValueError("corporate-action candidate snapshot is invalid") from error
    expected_pairs = pl.DataFrame(
        {
            "symbol": [scope.symbol for scope in scopes],
            "year": [scope.year for scope in scopes],
        }
    )
    if (
        manifest.get("status") != "complete"
        or manifest.get("successful_query_count") != len(scopes)
        or coverage.select(pl.struct("symbol", "year").is_duplicated().any()).item()
        or coverage.join(expected_pairs, on=["symbol", "year"], how="anti").height
        or expected_pairs.join(coverage, on=["symbol", "year"], how="anti").height
        or coverage.filter(
            (pl.col("status") != "ok") | (pl.col("row_count") < 0)
        ).height
    ):
        raise ValueError("corporate-action candidate query coverage is invalid")
    expected_candidates = normalize_corporate_action_candidates(raw, symbols=symbols)
    if not candidates.equals(expected_candidates):
        raise ValueError("corporate-action candidates differ from raw provider rows")


def _result(
    root: Path,
    *,
    completed: int,
    total: int,
    complete: bool,
) -> CorporateActionCandidateCollection:
    snapshot = root / SNAPSHOT_DIRECTORY
    return CorporateActionCandidateCollection(
        root=root,
        completed_queries=completed,
        total_queries=total,
        manifest_path=snapshot / MANIFEST_NAME if complete else None,
        raw_path=snapshot / RAW_NAME if complete else None,
        query_coverage_path=snapshot / COVERAGE_NAME if complete else None,
        candidates_path=snapshot / CANDIDATES_NAME if complete else None,
    )


def _parquet_bytes(frame: pl.DataFrame) -> bytes:
    stream = BytesIO()
    frame.write_parquet(stream)
    return stream.getvalue()


def _open_or_create_directory(parent_fd: int, name: str, *, label: str) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    return open_directory_at(parent_fd, name, label=label)


def _assert_authorization(authorization: FinalTestAuthorization) -> None:
    if not isinstance(authorization, FinalTestAuthorization):
        raise TypeError("candidate authorization is invalid")
    if authorization.test_period != (FINAL_TEST_START, FINAL_TEST_END):
        raise ValueError("candidate authorization period differs from final test")


def _load_bound_symbol_scope(preparation: FinalTestPreparation) -> list[str]:
    try:
        frame = pl.read_parquet(preparation.symbol_scope_path)
    except pl.exceptions.PolarsError as error:
        raise ValueError("invalid candidate preparation symbol scope") from error
    if frame.columns != ["symbol"] or frame.get_column("symbol").null_count():
        raise ValueError("invalid candidate preparation symbol scope")
    symbols = frame.get_column("symbol").cast(pl.String).to_list()
    digest = hashlib.sha256(("\n".join(symbols) + "\n").encode()).hexdigest()
    if (
        len(symbols) != preparation.symbol_count
        or symbols != sorted(set(symbols))
        or digest != preparation.symbols_sha256
    ):
        raise ValueError("candidate preparation symbol scope identity changed")
    return symbols


def _final_root_from_preparation(preparation: FinalTestPreparation) -> Path:
    if not isinstance(preparation, FinalTestPreparation):
        raise TypeError("candidate preparation is invalid")
    root = preparation.root
    if (
        root.name != preparation.attempt_id
        or root.parent.name != "preparations"
        or root.parent.parent.name != "final_test"
    ):
        raise ValueError("candidate preparation root is not canonical")
    return root.parent.parent
