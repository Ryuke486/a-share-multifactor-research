"""Fetch only catalog-approved official documents into an immutable cache."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from http.client import RemoteDisconnected
from pathlib import Path
import time
from typing import Protocol
from urllib import error
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ashare_multifactor.final_test.official_announcement_catalog import (
    VerifiedAnnouncementCatalog,
    load_verified_announcement_catalog,
)
from ashare_multifactor.final_test.official_evidence_workspace import (
    CachedOfficialDocument,
    EvidenceWorkspace,
    load_cached_document,
    open_workspace,
    publish_document_cache,
    publish_review_workspace,
)



@dataclass(frozen=True)
class DocumentFetchPolicy:
    """Bounded retry and spacing rules for public document downloads."""

    attempts: int = 3
    timeout_seconds: float = 30.0
    minimum_interval_seconds: float = 1.0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.attempts, int)
            or isinstance(self.attempts, bool)
            or self.attempts <= 0
            or not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
            or not isinstance(self.minimum_interval_seconds, (int, float))
            or isinstance(self.minimum_interval_seconds, bool)
            or self.minimum_interval_seconds < 0
        ):
            raise ValueError("official document fetch policy is invalid")


DEFAULT_DOCUMENT_TIMEOUT_SECONDS = DocumentFetchPolicy().timeout_seconds


class OfficialDocumentError(RuntimeError):
    """Base error for one public official-document request."""


class OfficialDocumentTransientError(OfficialDocumentError):
    """A document request that can be retried within the fixed policy."""


class OfficialDocumentHttpError(OfficialDocumentError):
    """A non-retryable public document HTTP response."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(f"official document HTTP {status}: {message}")


class OfficialDocumentTransport(Protocol):
    """Narrow public document transport; implementations receive no credentials."""

    def fetch(self, url: str, *, timeout_seconds: float) -> bytes:
        """Return the exact public document bytes for one already-approved URL."""


class UrllibOfficialDocumentTransport:
    """Public GET transport with no cookies, authorization, or persisted headers."""

    def fetch(self, url: str, *, timeout_seconds: float) -> bytes:
        request = Request(
            url,
            method="GET",
            headers={"User-Agent": "ashare-multifactor-research/1.0"},
        )
        opener = build_opener(_RejectRedirect())
        try:
            with opener.open(request, timeout=timeout_seconds) as response:  # noqa: S310
                return response.read()
        except error.HTTPError as failure:
            if failure.code == 429 or 500 <= failure.code <= 599:
                raise OfficialDocumentTransientError(
                    f"official document HTTP {failure.code}"
                ) from failure
            raise OfficialDocumentHttpError(failure.code, failure.reason) from failure
        except (RemoteDisconnected, TimeoutError, error.URLError) as failure:
            raise OfficialDocumentTransientError(
                "official document transport failed"
            ) from failure


class _RejectRedirect(HTTPRedirectHandler):
    """Refuse redirects so an approved URL cannot silently leave its source."""

    def redirect_request(self, *args: object, **kwargs: object) -> Request | None:
        del args, kwargs
        return None


def fetch_official_documents(
    catalog_path: Path,
    *,
    destination: Path,
    transport: OfficialDocumentTransport,
    max_documents: int | None = None,
    policy: DocumentFetchPolicy | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> EvidenceWorkspace:
    """Cache bounded public documents and publish an unready review snapshot."""
    if max_documents is not None and (
        not isinstance(max_documents, int)
        or isinstance(max_documents, bool)
        or max_documents <= 0
    ):
        raise ValueError("official document maximum is invalid")
    active_policy = DocumentFetchPolicy() if policy is None else policy
    if not isinstance(active_policy, DocumentFetchPolicy):
        raise TypeError("official document fetch policy is invalid")
    if not callable(sleep):
        raise TypeError("official document sleep function is invalid")
    catalog = load_verified_announcement_catalog(catalog_path)
    _assert_catalog_belongs_to_destination(catalog, destination)
    urls = _catalog_urls(catalog)
    with open_workspace(destination) as (
        workspace_root,
        _workspace_fd,
        documents_fd,
        sessions_fd,
    ):
        cached = {
            url: document
            for url in urls
            if (
                document := load_cached_document(documents_fd, source_url=url)
            )
            is not None
        }
        uncached = [url for url in urls if url not in cached]
        selected = uncached if max_documents is None else uncached[:max_documents]
        for url in selected:
            cached[url] = _obtain_document(
                documents_fd,
                url=url,
                transport=transport,
                policy=active_policy,
                sleep=sleep,
            )
        return publish_review_workspace(
            workspace_root=workspace_root,
            sessions_fd=sessions_fd,
            catalog=catalog,
            cached_documents=cached,
        )


def _obtain_document(
    documents_fd: int,
    *,
    url: str,
    transport: OfficialDocumentTransport,
    policy: DocumentFetchPolicy,
    sleep: Callable[[float], None],
) -> CachedOfficialDocument:
    cached = load_cached_document(documents_fd, source_url=url)
    if cached is not None:
        return cached
    payload = _fetch_document_with_retry(
        transport,
        url=url,
        policy=policy,
        sleep=sleep,
    )
    return publish_document_cache(documents_fd, source_url=url, payload=payload)


def _fetch_document_with_retry(
    transport: OfficialDocumentTransport,
    *,
    url: str,
    policy: DocumentFetchPolicy,
    sleep: Callable[[float], None],
) -> bytes:
    """Fetch a cache miss with bounded retries and a post-success interval."""
    for attempt in range(policy.attempts):
        try:
            payload = transport.fetch(url, timeout_seconds=policy.timeout_seconds)
        except OfficialDocumentTransientError:
            if attempt + 1 == policy.attempts:
                raise
            sleep(policy.minimum_interval_seconds * (2**attempt))
            continue
        if not isinstance(payload, bytes) or not payload:
            raise ValueError("official document response is empty")
        # The collector is sequential. Sleeping after every successful request
        # enforces the fixed minimum interval before another public download.
        sleep(policy.minimum_interval_seconds)
        return payload
    raise RuntimeError("official document retry loop terminated unexpectedly")


def _catalog_urls(catalog: VerifiedAnnouncementCatalog) -> list[str]:
    urls = catalog.frame.get_column("source_url").unique().sort().to_list()
    if any(not isinstance(url, str) or not url for url in urls):
        raise ValueError("official document catalog URL is invalid")
    return urls


def _assert_catalog_belongs_to_destination(
    catalog: VerifiedAnnouncementCatalog,
    destination: Path,
) -> None:
    expected = destination.absolute() / "official_announcement_catalog"
    if catalog.root.absolute() != expected:
        raise ValueError("official document catalog differs from evidence destination")
