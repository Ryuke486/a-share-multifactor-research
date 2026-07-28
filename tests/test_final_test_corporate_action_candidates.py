from __future__ import annotations

from datetime import date
import json

import polars as pl
import pytest

from ashare_multifactor.final_test.action_source_contract import (
    load_action_source_contract,
)
from ashare_multifactor.final_test.corporate_action_candidates import (
    collect_baostock_corporate_action_candidates,
    collect_corporate_action_candidates,
)
from ashare_multifactor.final_test.preparation import verify_preparation
from ashare_multifactor.final_test.resume import load_registered_authorization
from test_final_test_resume import PreparedAttempt


pytest_plugins = ("test_final_test_resume",)

_FIELDS = [
    "code",
    "dividOperateDate",
    "dividPayDate",
    "dividStockMarketDate",
    "dividCashPsBeforeTax",
    "dividStocksPs",
    "dividReserveToStockPs",
]


class CandidateQuery:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str]] = []

    def __call__(
        self,
        code: str,
        year: int,
        year_type: str,
    ) -> tuple[list[str], list[list[str]]]:
        self.calls.append((code, year, year_type))
        if code == "sz.000001" and year == 2022:
            return _FIELDS, [
                [
                    code,
                    "2022-06-01",
                    "2022-06-08",
                    "",
                    "0.10",
                    "0",
                    "0",
                ]
            ]
        return _FIELDS, []


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
            attempt.data_root / "processed/final_test_evidence" / attempt.attempt_id
        ),
    }


def test_candidate_collection_publishes_exact_resumable_snapshot(
    prepared_attempt: PreparedAttempt,
) -> None:
    inputs = _inputs(prepared_attempt)
    query = CandidateQuery()

    partial = collect_corporate_action_candidates(
        **inputs,
        query=query,
        max_queries=3,
    )

    assert partial.completed_queries == 3
    assert partial.total_queries == 8
    assert partial.manifest_path is None
    assert len(query.calls) == 3

    complete = collect_corporate_action_candidates(**inputs, query=query)

    assert complete.completed_queries == complete.total_queries == 8
    assert complete.manifest_path is not None
    assert len(query.calls) == 8
    assert len(list((complete.root / "queries").glob("*"))) == 8
    coverage = pl.read_parquet(complete.query_coverage_path)
    assert coverage.select("symbol", "year", "status", "row_count").to_dicts() == [
        {
            "symbol": symbol,
            "year": year,
            "status": "ok",
            "row_count": int(symbol == "000001" and year == 2022),
        }
        for symbol in ("000001", "600000")
        for year in range(2022, 2026)
    ]
    candidates = pl.read_parquet(complete.candidates_path)
    assert candidates.select(
        "symbol",
        "ex_date",
        "effective_date",
        "cash_per_share",
        "share_ratio",
    ).to_dicts() == [
        {
            "symbol": "000001",
            "ex_date": date(2022, 6, 1),
            "effective_date": date(2022, 6, 8),
            "cash_per_share": 0.1,
            "share_ratio": 0.0,
        }
    ]
    assert len(candidates.item(0, "candidate_id")) == 24

    recovered = collect_corporate_action_candidates(
        **inputs,
        query=lambda *_args: pytest.fail("complete query package was fetched again"),
    )
    assert recovered == complete


def test_baostock_candidate_collection_recovers_after_peer_eof(
    prepared_attempt: PreparedAttempt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import baostock as bs
    import baostock.common.context as context
    import baostock.util.socketutil as socketutil
    import socket

    inputs = _inputs(prepared_attempt)
    query_attempts = 0
    connection_timeouts: list[float] = []

    class PeerEofSocket:
        def __init__(self) -> None:
            self.recv_count = 0

        def settimeout(self, _seconds: float) -> None:
            pass

        def send(self, payload: bytes) -> int:
            return len(payload)

        def recv(self, _size: int) -> bytes:
            self.recv_count += 1
            if self.recv_count > 2:
                raise TimeoutError("test guard stopped the EOF busy loop")
            return b""

        def close(self) -> None:
            pass

    class Result:
        def __init__(
            self,
            *,
            error_code: str = "0",
            error_msg: str = "",
        ) -> None:
            self.error_code = error_code
            self.error_msg = error_msg
            self.fields = _FIELDS

        def next(self) -> bool:
            return False

        def get_row_data(self) -> list[str]:
            raise AssertionError("empty result has no rows")

    def create_connection(
        _address: tuple[str, int],
        timeout: float,
    ) -> PeerEofSocket:
        connection_timeouts.append(timeout)
        return PeerEofSocket()

    def unbounded_connect_must_not_run(_self: object) -> None:
        raise AssertionError("BaoStock's unbounded connect path was used")

    def login() -> Result:
        socketutil.SocketUtil().connect()
        return Result()

    def query_dividend_data(
        *,
        code: str,
        year: str,
        yearType: str,
    ) -> Result:
        nonlocal query_attempts
        del code, year, yearType
        query_attempts += 1
        if query_attempts == 1:
            assert socketutil.send_msg("peer-eof-probe") is None
            return Result(
                error_code="10002007",
                error_msg="network receive error",
            )
        return Result()

    def logout() -> Result:
        context.default_socket.close()
        return Result()

    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(
        socketutil.SocketUtil,
        "connect",
        unbounded_connect_must_not_run,
    )
    monkeypatch.setattr(bs, "login", login)
    monkeypatch.setattr(bs, "query_dividend_data", query_dividend_data)
    monkeypatch.setattr(bs, "logout", logout)

    result = collect_baostock_corporate_action_candidates(
        **inputs,
        max_queries=1,
    )

    assert result.completed_queries == 1
    assert query_attempts == 2
    assert connection_timeouts == [20.0, 20.0]


def test_candidate_collection_rejects_response_outside_exact_query_scope(
    prepared_attempt: PreparedAttempt,
) -> None:
    inputs = _inputs(prepared_attempt)

    def invalid_query(
        code: str,
        year: int,
        year_type: str,
    ) -> tuple[list[str], list[list[str]]]:
        del year, year_type
        return _FIELDS, [
            [code, "2023-06-01", "2023-06-08", "", "0.1", "0", "0"]
        ]

    with pytest.raises(ValueError, match="operate-year response contract"):
        collect_corporate_action_candidates(
            **inputs,
            query=invalid_query,
            max_queries=1,
        )

    query_root = (
        inputs["output_root"] / "corporate_action_candidate_collection" / "queries"
    )
    assert not query_root.exists() or not any(query_root.iterdir())


def test_candidate_collection_rejects_changed_provider_schema(
    prepared_attempt: PreparedAttempt,
) -> None:
    inputs = _inputs(prepared_attempt)
    calls = 0

    def changed_schema(
        code: str,
        year: int,
        year_type: str,
    ) -> tuple[list[str], list[list[str]]]:
        nonlocal calls
        del code, year, year_type
        calls += 1
        fields = _FIELDS if calls == 1 else [*_FIELDS, "newField"]
        return fields, []

    with pytest.raises(ValueError, match="response schema changed"):
        collect_corporate_action_candidates(
            **inputs,
            query=changed_schema,
            max_queries=2,
        )


def test_candidate_collection_preserves_missing_payment_date_for_review(
    prepared_attempt: PreparedAttempt,
) -> None:
    inputs = _inputs(prepared_attempt)

    def incomplete_cash_query(
        code: str,
        year: int,
        year_type: str,
    ) -> tuple[list[str], list[list[str]]]:
        del year_type
        if code == "sz.000001" and year == 2025:
            return _FIELDS, [
                [code, "2025-06-12", "", "", "0.10", "0", "0"]
            ]
        return _FIELDS, []

    result = collect_corporate_action_candidates(
        **inputs,
        query=incomplete_cash_query,
    )

    candidates = pl.read_parquet(result.candidates_path)
    assert candidates.select(
        "symbol",
        "ex_date",
        "effective_date",
        "cash_per_share",
        "share_ratio",
    ).to_dicts() == [
        {
            "symbol": "000001",
            "ex_date": date(2025, 6, 12),
            "effective_date": None,
            "cash_per_share": 0.1,
            "share_ratio": 0.0,
        }
    ]
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "2"
    assert manifest["missing_effective_date_candidate_count"] == 1
