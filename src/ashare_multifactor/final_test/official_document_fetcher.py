"""Fetch only catalog-approved official documents into an immutable cache."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol
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


DEFAULT_DOCUMENT_TIMEOUT_SECONDS = 30.0


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
        with opener.open(request, timeout=timeout_seconds) as response:  # noqa: S310
            return response.read()


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
) -> EvidenceWorkspace:
    """Cache bounded public documents and publish an unready review snapshot."""
    if max_documents is not None and (
        not isinstance(max_documents, int)
        or isinstance(max_documents, bool)
        or max_documents <= 0
    ):
        raise ValueError("official document maximum is invalid")
    catalog = load_verified_announcement_catalog(catalog_path)
    _assert_catalog_belongs_to_destination(catalog, destination)
    urls = _catalog_urls(catalog)
    selected = urls if max_documents is None else urls[:max_documents]
    with open_workspace(destination) as (
        workspace_root,
        _workspace_fd,
        documents_fd,
        sessions_fd,
    ):
        cached = {
            url: _obtain_document(
                documents_fd,
                url=url,
                transport=transport,
            )
            for url in selected
        }
        for url in urls:
            existing = load_cached_document(documents_fd, source_url=url)
            if existing is not None:
                cached[url] = existing
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
) -> CachedOfficialDocument:
    cached = load_cached_document(documents_fd, source_url=url)
    if cached is not None:
        return cached
    payload = transport.fetch(url, timeout_seconds=DEFAULT_DOCUMENT_TIMEOUT_SECONDS)
    return publish_document_cache(documents_fd, source_url=url, payload=payload)


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
