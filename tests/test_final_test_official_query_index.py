from datetime import date
import hashlib
import json
from pathlib import Path

import pytest

from ashare_multifactor.final_test.official_query_coverage import (
    OfficialQueryScope,
    canonical_json_bytes,
    validate_official_query_package,
)
from test_final_test_official_query_coverage import _write_package


def _scope(symbol: str, category: str) -> OfficialQueryScope:
    return OfficialQueryScope(
        symbol=symbol,
        market="sz",
        category=category,
        query_category="",
        start=date(2022, 1, 1),
        end=date(2025, 12, 31),
    )


def _write_index(root: Path, scopes: list[OfficialQueryScope]) -> Path:
    records = []
    for scope in sorted(
        scopes,
        key=lambda value: (
            value.category,
            value.market,
            value.symbol,
            value.query_category,
        ),
    ):
        package = _write_package(
            root / "packages" / scope.category / scope.market / scope.symbol,
            scope=scope,
        )
        verified = validate_official_query_package(package, expected_scope=scope)
        records.append(
            {
                "symbol": scope.symbol,
                "market": scope.market,
                "org_id": verified.scope.org_id,
                "category": scope.category,
                "query_category": scope.query_category,
                "relative_path": package.relative_to(root).as_posix(),
                "request_sha256": verified.request_sha256,
                "pages_sha256": verified.pages_sha256,
                "manifest_sha256": verified.manifest_sha256,
                "total_pages": verified.total_pages,
                "total_results": verified.total_results,
            }
        )
    index = root / "official_query_coverage.json"
    index.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": "1",
                "role": "official_query_coverage",
                "packages": records,
            }
        )
    )
    return index


def test_official_query_index_binds_each_expected_scope_to_one_package(
    tmp_path: Path,
) -> None:
    scopes = [
        _scope("000001", "corporate_actions"),
        _scope("000001", "security_events"),
    ]
    index = _write_index(tmp_path, scopes)

    from ashare_multifactor.final_test.official_query_index import (
        validate_official_query_coverage_index,
    )

    verified = validate_official_query_coverage_index(index, expected_scopes=scopes)

    assert verified.index_sha256 == hashlib.sha256(index.read_bytes()).hexdigest()
    assert verified.packages == tuple(
        validate_official_query_package(
            index.parent / record,
            expected_scope=scope,
        )
        for scope, record in zip(
            scopes,
            (
                "packages/corporate_actions/sz/000001/query-package",
                "packages/security_events/sz/000001/query-package",
            ),
            strict=True,
        )
    )


def test_official_query_index_rejects_missing_zero_event_query_package(
    tmp_path: Path,
) -> None:
    scopes = [
        _scope("000001", "corporate_actions"),
        _scope("000001", "security_events"),
    ]
    index = _write_index(tmp_path, scopes[:1])

    from ashare_multifactor.final_test.official_query_index import (
        validate_official_query_coverage_index,
    )

    with pytest.raises(ValueError, match="scope|package|exact"):
        validate_official_query_coverage_index(index, expected_scopes=scopes)


def test_official_query_index_rejects_record_hash_drift(
    tmp_path: Path,
) -> None:
    scopes = [_scope("000001", "corporate_actions")]
    index = _write_index(tmp_path, scopes)
    payload = json.loads(index.read_text(encoding="utf-8"))
    payload["packages"][0]["manifest_sha256"] = "0" * 64
    index.write_bytes(canonical_json_bytes(payload))

    from ashare_multifactor.final_test.official_query_index import (
        validate_official_query_coverage_index,
    )

    with pytest.raises(ValueError, match="identity|hash"):
        validate_official_query_coverage_index(index, expected_scopes=scopes)
