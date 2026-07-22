"""Bounded public transport for CNInfo announcement queries."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.client import RemoteDisconnected
import time
from typing import Protocol
from urllib import error, parse, request

from ashare_multifactor.final_test.official_query_coverage import (
    OFFICIAL_QUERY_ENDPOINT,
)


_PAGE_FORM_FIELDS = frozenset(
    {
        "category",
        "column",
        "pageNum",
        "pageSize",
        "plate",
        "searchkey",
        "seDate",
        "stock",
        "tabName",
        "trade",
    }
)
_SENSITIVE_FORM_FIELDS = frozenset(
    {"authorization", "approval_key", "cookie", "token"}
)


class OfficialQueryError(RuntimeError):
    """Base error for public official-query transport failures."""


class OfficialQueryTransientError(OfficialQueryError):
    """A retryable transport failure."""


class OfficialQueryHttpError(OfficialQueryError):
    """A non-retryable official-query HTTP response."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(f"official query HTTP {status}: {message}")


class OfficialQueryTransport(Protocol):
    """Fetch exact public response bytes for one page request."""

    def fetch(
        self,
        endpoint: str,
        form: Mapping[str, str],
        *,
        timeout_seconds: float,
    ) -> bytes:
        """Return raw response bytes or raise an official-query error."""


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retry and inter-request interval policy."""

    attempts: int = 3
    timeout_seconds: float = 20.0
    minimum_interval_seconds: float = 1.0

    def __post_init__(self) -> None:
        if (
            self.attempts <= 0
            or self.timeout_seconds <= 0
            or self.minimum_interval_seconds < 0
        ):
            raise ValueError("official query retry policy is invalid")


class UrllibOfficialQueryTransport:
    """CNInfo POST transport with no credential, cookie, or cache behavior."""

    def fetch(
        self,
        endpoint: str,
        form: Mapping[str, str],
        *,
        timeout_seconds: float,
    ) -> bytes:
        query = build_request(endpoint, form)
        try:
            with request.urlopen(query, timeout=timeout_seconds) as response:
                return response.read()
        except error.HTTPError as failure:
            if failure.code == 429 or 500 <= failure.code <= 599:
                raise OfficialQueryTransientError(
                    f"official query HTTP {failure.code}"
                ) from failure
            raise OfficialQueryHttpError(failure.code, failure.reason) from failure
        except (RemoteDisconnected, TimeoutError, error.URLError) as failure:
            raise OfficialQueryTransientError("official query transport failed") from failure


def build_request(endpoint: str, form: Mapping[str, str]) -> request.Request:
    """Create the sole permitted unauthenticated public CNInfo POST request."""
    if endpoint != OFFICIAL_QUERY_ENDPOINT:
        raise ValueError("official query endpoint is invalid")
    normalized = _validate_page_form(form)
    return request.Request(
        endpoint,
        data=parse.urlencode(normalized).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "ashare-multifactor-research/1.0",
        },
        method="POST",
    )


def fetch_with_retry(
    transport: OfficialQueryTransport,
    *,
    endpoint: str,
    form: Mapping[str, str],
    policy: RetryPolicy,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """Fetch one page with bounded retry; keep response bytes unparsed."""
    _validate_page_form(form)
    for attempt in range(policy.attempts):
        try:
            payload = transport.fetch(
                endpoint,
                form,
                timeout_seconds=policy.timeout_seconds,
            )
        except OfficialQueryTransientError:
            if attempt + 1 == policy.attempts:
                raise
            sleep(policy.minimum_interval_seconds * (2**attempt))
            continue
        if not payload:
            raise ValueError("official query response is empty")
        # Calls are sequential. Sleeping after every successful request
        # establishes the minimum interval before the next public request.
        sleep(policy.minimum_interval_seconds)
        return payload
    raise RuntimeError("official query retry loop terminated unexpectedly")


def _validate_page_form(form: Mapping[str, str]) -> dict[str, str]:
    normalized = dict(form)
    fields = {str(field) for field in normalized}
    if fields.intersection(_SENSITIVE_FORM_FIELDS):
        raise ValueError("official query form contains a sensitive field")
    if fields != _PAGE_FORM_FIELDS or any(
        not isinstance(field, str) or not isinstance(value, str)
        for field, value in normalized.items()
    ):
        raise ValueError("official query form is invalid")
    return normalized
