from __future__ import annotations

import json

import pytest

from ashare_multifactor.final_test.action_source_contract import (
    load_action_source_contract,
)
from ashare_multifactor.final_test.official_query_client import RetryPolicy
from ashare_multifactor.final_test.preparation import verify_preparation
from ashare_multifactor.final_test.resume import load_registered_authorization
from test_final_test_official_query_collector import ZeroResultTransport
from test_final_test_resume import PreparedAttempt


pytest_plugins = ("test_final_test_resume",)


class IdentityTransport:
    """Deterministic CNInfo search substitute with official-shaped records."""

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
        symbol = form["keyWord"]
        org_id = {
            "000001": "gssz0000001",
            "600000": "gssh0600000",
        }[symbol]
        return json.dumps(
            [
                {
                    "category": "A股",
                    "code": symbol,
                    "orgId": org_id,
                    "zwjc": f"证券{symbol}",
                }
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")


class AmbiguousIdentityTransport:
    def fetch(
        self,
        endpoint: str,
        form: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> bytes:
        del endpoint, timeout_seconds
        symbol = form["keyWord"]
        return json.dumps(
            [
                {"code": symbol, "orgId": "first"},
                {"code": symbol, "orgId": "second"},
            ],
            separators=(",", ":"),
        ).encode("utf-8")


class FailIfCalled:
    def fetch(
        self,
        endpoint: str,
        form: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> bytes:
        del endpoint, form, timeout_seconds
        raise AssertionError("tampered identity cache must stop before networking")


def _inputs(attempt: PreparedAttempt) -> dict[str, object]:
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


def test_collector_binds_each_query_to_one_cninfo_org_id(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_query_collector import (
        collect_official_query_coverage,
    )

    inputs = _inputs(prepared_attempt)
    identities = IdentityTransport()
    queries = ZeroResultTransport()

    result = collect_official_query_coverage(
        **inputs,
        identity_transport=identities,
        transport=queries,
    )

    assert result.index_path is not None
    assert result.identity_index_path.is_file()
    assert [form for _endpoint, form, _timeout in identities.calls] == [
        {"keyWord": "000001", "maxNum": "10", "plate": ""},
        {"keyWord": "600000", "maxNum": "10", "plate": ""},
    ]
    assert {form["stock"] for _endpoint, form, _timeout in queries.calls} == {
        "000001,gssz0000001",
        "600000,gssh0600000",
    }
    request = json.loads(
        (
            result.root
            / "packages/corporate_actions/sh/600000/query-package/request.json"
        ).read_text(encoding="utf-8")
    )
    assert request["scope"]["org_id"] == "gssh0600000"
    assert request["form"]["stock"] == "600000,gssh0600000"


def test_identity_ambiguity_stops_before_any_announcement_query(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_query_collector import (
        collect_official_query_coverage,
    )

    queries = ZeroResultTransport()
    with pytest.raises(ValueError, match="identity|orgId|ambiguous"):
        collect_official_query_coverage(
            **_inputs(prepared_attempt),
            identity_transport=AmbiguousIdentityTransport(),
            transport=queries,
        )

    assert queries.calls == []


def test_tampered_identity_index_stops_before_any_announcement_query(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_query_collector import (
        collect_official_query_coverage,
    )

    inputs = _inputs(prepared_attempt)
    first = collect_official_query_coverage(
        **inputs,
        identity_transport=IdentityTransport(),
        transport=ZeroResultTransport(),
        max_scopes=1,
    )
    first.identity_index_path.write_bytes(
        first.identity_index_path.read_bytes() + b" "
    )
    queries = ZeroResultTransport()

    with pytest.raises(ValueError, match="identity.*index|identity"):
        collect_official_query_coverage(
            **inputs,
            identity_transport=FailIfCalled(),
            transport=queries,
        )

    assert queries.calls == []
