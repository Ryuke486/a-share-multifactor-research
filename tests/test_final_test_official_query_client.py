from collections.abc import Mapping
from http.client import RemoteDisconnected

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
    assert pauses == [0.5, 0.5]


def test_client_waits_after_a_successful_public_request() -> None:
    from ashare_multifactor.final_test.official_query_client import (
        RetryPolicy,
        fetch_with_retry,
    )

    pauses: list[float] = []

    result = fetch_with_retry(
        ScriptedTransport(
            [b'{"totalpages":0,"totalAnnouncement":0,"announcements":null}']
        ),
        endpoint="https://www.cninfo.com.cn/new/hisAnnouncement/query",
        form=_form(),
        policy=RetryPolicy(minimum_interval_seconds=0.75),
        sleep=pauses.append,
    )

    assert result == b'{"totalpages":0,"totalAnnouncement":0,"announcements":null}'
    assert pauses == [0.75]


def test_urllib_client_retries_remote_disconnect_as_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ashare_multifactor.final_test import official_query_client as client

    payload = b'{"totalpages":0,"totalAnnouncement":0,"announcements":null}'
    calls = 0

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return payload

    def urlopen(*_: object, **__: object) -> Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RemoteDisconnected("official endpoint closed the connection")
        return Response()

    monkeypatch.setattr(client.request, "urlopen", urlopen)
    pauses: list[float] = []

    result = client.fetch_with_retry(
        client.UrllibOfficialQueryTransport(),
        endpoint="https://www.cninfo.com.cn/new/hisAnnouncement/query",
        form=_form(),
        policy=client.RetryPolicy(
            attempts=2,
            timeout_seconds=1.0,
            minimum_interval_seconds=0.5,
        ),
        sleep=pauses.append,
    )

    assert result == payload
    assert calls == 2
    assert pauses == [0.5, 0.5]


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


def test_identity_request_accepts_only_the_public_cninfo_search_form() -> None:
    from ashare_multifactor.final_test.official_query_client import (
        OFFICIAL_SECURITY_IDENTITY_ENDPOINT,
        build_request,
    )

    request = build_request(
        OFFICIAL_SECURITY_IDENTITY_ENDPOINT,
        {"keyWord": "600000", "maxNum": "10", "plate": ""},
    )

    assert request.get_method() == "POST"
    assert request.get_header("Cookie") is None
    assert request.get_header("Authorization") is None
    assert request.data == b"keyWord=600000&maxNum=10&plate="
    with pytest.raises(ValueError, match="sensitive|identity form"):
        build_request(
            OFFICIAL_SECURITY_IDENTITY_ENDPOINT,
            {
                "keyWord": "600000",
                "maxNum": "10",
                "plate": "",
                "token": "secret",
            },
        )
