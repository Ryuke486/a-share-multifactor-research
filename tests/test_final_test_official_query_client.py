from collections.abc import Mapping

import pytest


class ScriptedTransport:
    def __init__(self, responses: list[bytes | BaseException]) -> None:
        self._responses = iter(responses)
        self.calls = 0

    def fetch(
        self,
        endpoint: str,
        form: Mapping[str, str],
        *,
        timeout_seconds: float,
    ) -> bytes:
        del endpoint, form, timeout_seconds
        self.calls += 1
        response = next(self._responses)
        if isinstance(response, BaseException):
            raise response
        return response


def _form() -> dict[str, str]:
    return {
        "category": "",
        "column": "szse",
        "plate": "sz",
        "searchkey": "",
        "seDate": "2021-01-01~2021-12-31",
        "stock": "000001",
        "tabName": "fulltext",
        "trade": "",
        "pageNum": "1",
        "pageSize": "30",
    }


def test_client_retries_transient_failure_and_returns_exact_raw_bytes() -> None:
    from ashare_multifactor.final_test.official_query_client import (
        OfficialQueryTransientError,
        RetryPolicy,
        fetch_with_retry,
    )

    transport = ScriptedTransport(
        [
            OfficialQueryTransientError("HTTP 429"),
            b'{"totalpages":0,"totalAnnouncement":0,"announcements":null}',
        ]
    )
    pauses: list[float] = []

    result = fetch_with_retry(
        transport,
        endpoint="https://www.cninfo.com.cn/new/hisAnnouncement/query",
        form=_form(),
        policy=RetryPolicy(attempts=2, timeout_seconds=1.0, minimum_interval_seconds=0.5),
        sleep=pauses.append,
    )

    assert result == b'{"totalpages":0,"totalAnnouncement":0,"announcements":null}'
    assert transport.calls == 2
    assert pauses == [0.5]


def test_client_rejects_sensitive_form_keys_before_transport() -> None:
    from ashare_multifactor.final_test.official_query_client import (
        RetryPolicy,
        fetch_with_retry,
    )

    form = _form() | {"Cookie": "secret"}
    transport = ScriptedTransport([b"unreachable"])

    with pytest.raises(ValueError, match="sensitive|form"):
        fetch_with_retry(
            transport,
            endpoint="https://www.cninfo.com.cn/new/hisAnnouncement/query",
            form=form,
            policy=RetryPolicy(),
        )

    assert transport.calls == 0


def test_client_rejects_non_retryable_http_failure_without_sleeping() -> None:
    from ashare_multifactor.final_test.official_query_client import (
        OfficialQueryHttpError,
        RetryPolicy,
        fetch_with_retry,
    )

    transport = ScriptedTransport([OfficialQueryHttpError(400, "bad request")])
    pauses: list[float] = []

    with pytest.raises(OfficialQueryHttpError, match="400"):
        fetch_with_retry(
            transport,
            endpoint="https://www.cninfo.com.cn/new/hisAnnouncement/query",
            form=_form(),
            policy=RetryPolicy(attempts=3),
            sleep=pauses.append,
        )

    assert transport.calls == 1
    assert pauses == []


def test_request_has_only_public_headers_and_urlencoded_form() -> None:
    from ashare_multifactor.final_test.official_query_client import build_request

    request = build_request(
        "https://www.cninfo.com.cn/new/hisAnnouncement/query",
        _form(),
    )

    assert request.get_method() == "POST"
    assert request.get_header("Cookie") is None
    assert request.get_header("Authorization") is None
    assert request.get_header("Content-type") == "application/x-www-form-urlencoded"
    assert request.data == b"category=&column=szse&plate=sz&searchkey=&seDate=2021-01-01~2021-12-31&stock=000001&tabName=fulltext&trade=&pageNum=1&pageSize=30"
