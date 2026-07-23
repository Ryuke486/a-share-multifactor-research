from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from ashare_multifactor.final_test.action_source_contract import (
    load_action_source_contract,
    official_query_scope,
)
from ashare_multifactor.final_test.official_query_client import RetryPolicy
from ashare_multifactor.final_test.official_query_coverage import (
    OFFICIAL_QUERY_ENDPOINT,
    validate_official_query_package,
)
from ashare_multifactor.final_test.official_query_index import (
    validate_official_query_coverage_index,
)
from ashare_multifactor.final_test.preparation import verify_preparation
from ashare_multifactor.final_test.resume import load_registered_authorization
from test_final_test_resume import PreparedAttempt


pytest_plugins = ("test_final_test_resume",)


class ZeroResultTransport:
    """Deterministic public-query substitute; it never reaches the network."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def fetch(
        self,
        endpoint: str,
        form: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> bytes:
        self.calls.append((endpoint, dict(form), timeout_seconds))
        if endpoint.endswith("/information/topSearch/query"):
            symbol = form["keyWord"]
            return json.dumps(
                [{"code": symbol, "orgId": f"fixture-{symbol}"}],
                separators=(",", ":"),
            ).encode()
        return b'{"totalpages":0,"totalAnnouncement":0,"announcements":null}'


class FailIfCalled:
    def fetch(
        self,
        endpoint: str,
        form: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> bytes:
        del endpoint, form, timeout_seconds
        raise AssertionError("collector must reject identity drift before networking")


class TwoPageTransport:
    def __init__(self, *, inconsistent_second_page: bool = False) -> None:
        self.inconsistent_second_page = inconsistent_second_page
        self.calls: list[str] = []

    def fetch(
        self,
        endpoint: str,
        form: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> bytes:
        del timeout_seconds
        if endpoint.endswith("/information/topSearch/query"):
            symbol = form["keyWord"]
            return json.dumps(
                [{"code": symbol, "orgId": f"fixture-{symbol}"}],
                separators=(",", ":"),
            ).encode()
        page = form["pageNum"]
        self.calls.append(page)
        total = 3 if page == "2" and self.inconsistent_second_page else 2
        return json.dumps(
            {
                "totalpages": total,
                "totalAnnouncement": 2,
                "announcements": [{"announcementId": f"announcement-{page}"}],
            },
            separators=(",", ":"),
        ).encode()


def _prepared_inputs(attempt: PreparedAttempt) -> dict[str, object]:
    authorization = load_registered_authorization(
        code_root=attempt.code_root,
        data_root=attempt.data_root,
        attempt_id=attempt.attempt_id,
        approval_key=attempt.approval_key,
    )
    preparation = verify_preparation(
        attempt.data_root / "processed/final_test",
        attempt_id=attempt.attempt_id,
        authorization=authorization,
    )
    return {
        "preparation": preparation,
        "authorization": authorization,
        "contract": load_action_source_contract(
            attempt.code_root / "configs/final_execution_sources.yaml"
        ),
        "output_root": (
            attempt.data_root
            / "processed/final_test_evidence"
            / attempt.attempt_id
        ),
        "policy": RetryPolicy(attempts=1, timeout_seconds=0.1, minimum_interval_seconds=0),
    }


def _expected_scopes(inputs: dict[str, object]):
    preparation = inputs["preparation"]
    contract = inputs["contract"]
    assert hasattr(preparation, "symbol_scope_path")
    assert hasattr(contract, "official_query_categories")
    symbols = ["000001", "600000"]
    return [
        official_query_scope(contract, symbol=symbol, category=category)
        for category, _query_category in contract.official_query_categories
        for symbol in symbols
    ]


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_collector_derives_exact_two_category_scope_from_verified_preparation(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_query_collector import (
        collect_official_query_coverage,
    )

    inputs = _prepared_inputs(prepared_attempt)
    transport = ZeroResultTransport()
    result = collect_official_query_coverage(**inputs, transport=transport)

    expected_scopes = _expected_scopes(inputs)
    assert result.total_scopes == len(expected_scopes) == 4
    assert result.completed_scopes == result.total_scopes
    assert result.index_path is not None
    assert validate_official_query_coverage_index(
        result.index_path,
        expected_scopes=expected_scopes,
    ).packages
    query_calls = [
        call
        for call in transport.calls
        if call[0] == OFFICIAL_QUERY_ENDPOINT
    ]
    assert len(query_calls) == result.total_scopes
    assert len(transport.calls) == result.total_scopes + 2


def test_collector_partial_run_has_no_index_and_resume_preserves_packages(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_query_collector import (
        collect_official_query_coverage,
    )

    inputs = _prepared_inputs(prepared_attempt)
    first = collect_official_query_coverage(
        **inputs,
        transport=ZeroResultTransport(),
        max_scopes=1,
    )
    first_tree = _tree_bytes(first.root / "packages")
    second_transport = ZeroResultTransport()
    second = collect_official_query_coverage(
        **inputs,
        transport=second_transport,
    )

    assert first.index_path is None
    assert first.completed_scopes == 1
    assert second.index_path is not None
    second_tree = _tree_bytes(second.root / "packages")
    for path, payload in first_tree.items():
        assert second_tree[path] == payload
    assert len(second_transport.calls) == second.total_scopes - first.completed_scopes


def test_collector_rejects_preparation_or_authorization_drift_before_network(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_query_collector import (
        collect_official_query_coverage,
    )

    inputs = _prepared_inputs(prepared_attempt)
    preparation = inputs["preparation"]
    authorization = inputs["authorization"]

    with pytest.raises(ValueError, match="preparation|authorization|identity"):
        collect_official_query_coverage(
            **{**inputs, "preparation": replace(preparation, symbols_sha256="0" * 64)},
            transport=FailIfCalled(),
        )
    with pytest.raises(ValueError, match="preparation|authorization|identity"):
        collect_official_query_coverage(
            **{**inputs, "authorization": replace(authorization, attempt_id="foreign")},
            transport=FailIfCalled(),
        )


def test_collector_rejects_a_foreign_evidence_root_before_network(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_query_collector import (
        collect_official_query_coverage,
    )

    inputs = _prepared_inputs(prepared_attempt)
    with pytest.raises(ValueError, match="output root"):
        collect_official_query_coverage(
            **{
                **inputs,
                "output_root": prepared_attempt.data_root / "foreign-evidence-root",
            },
            transport=FailIfCalled(),
        )


def test_collector_paginates_before_publishing_one_immutable_package(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_query_collector import (
        collect_official_query_coverage,
    )

    inputs = _prepared_inputs(prepared_attempt)
    transport = TwoPageTransport()
    result = collect_official_query_coverage(
        **inputs,
        transport=transport,
        max_scopes=1,
    )

    package = result.root / "packages/corporate_actions/sh/600000/query-package"
    scope = next(
        scope
        for scope in _expected_scopes(inputs)
        if (scope.category, scope.market, scope.symbol)
        == ("corporate_actions", "sh", "600000")
    )
    assert transport.calls == ["1", "2"]
    assert result.index_path is None
    assert validate_official_query_package(package, expected_scope=scope).total_results == 2


def test_collector_rejects_inconsistent_pagination_without_publishing_package(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_query_collector import (
        collect_official_query_coverage,
    )

    inputs = _prepared_inputs(prepared_attempt)
    with pytest.raises(ValueError, match="totals"):
        collect_official_query_coverage(
            **inputs,
            transport=TwoPageTransport(inconsistent_second_page=True),
            max_scopes=1,
        )

    package = (
        Path(inputs["output_root"])
        / "official_query_coverage/packages/corporate_actions/sh/600000/query-package"
    )
    assert not package.exists()


def test_collector_resumes_a_completed_staging_package_without_network(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ashare_multifactor.final_test import official_query_collector as collector
    from ashare_multifactor.final_test import official_query_packages as packages

    inputs = _prepared_inputs(prepared_attempt)
    original_rename = packages.atomic_rename_no_replace_at

    def interrupt_before_publish(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt("injected before package publication")

    monkeypatch.setattr(
        packages,
        "atomic_rename_no_replace_at",
        interrupt_before_publish,
    )
    with pytest.raises(KeyboardInterrupt, match="before package publication"):
        collector.collect_official_query_coverage(
            **inputs,
            transport=ZeroResultTransport(),
            max_scopes=1,
        )
    monkeypatch.setattr(
        packages,
        "atomic_rename_no_replace_at",
        original_rename,
    )

    result = collector.collect_official_query_coverage(
        **inputs,
        transport=FailIfCalled(),
        max_scopes=1,
    )

    assert result.completed_scopes == 1
    assert result.index_path is None


def test_collection_manifest_binds_authorization_and_preparation(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_query_collector import (
        collect_official_query_coverage,
        verify_official_query_collection_binding,
    )

    inputs = _prepared_inputs(prepared_attempt)
    result = collect_official_query_coverage(
        **inputs,
        transport=ZeroResultTransport(),
    )

    binding_args = {
        key: inputs[key]
        for key in ("output_root", "preparation", "authorization", "contract")
    }
    assert verify_official_query_collection_binding(**binding_args) == result.root
    with pytest.raises(ValueError, match="identity"):
        verify_official_query_collection_binding(
            **{
                **binding_args,
                "preparation": replace(
                    inputs["preparation"],
                    manifest_sha256="0" * 64,
                ),
            }
        )
