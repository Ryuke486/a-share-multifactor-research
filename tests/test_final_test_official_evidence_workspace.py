from __future__ import annotations

from dataclasses import dataclass
from http.client import RemoteDisconnected
import json
from pathlib import Path

import polars as pl
import pytest

from ashare_multifactor.final_test.action_source_contract import (
    load_action_source_contract,
)
from ashare_multifactor.final_test.official_query_client import RetryPolicy
from ashare_multifactor.final_test.preparation import verify_preparation
from ashare_multifactor.final_test.resume import load_registered_authorization
from test_final_test_resume import PreparedAttempt


pytest_plugins = ("test_final_test_resume",)


@dataclass(frozen=True)
class EvidenceInputs:
    preparation: object
    authorization: object
    contract: object
    output_root: Path
    index_path: Path


class AnnouncementTransport:
    def __init__(self, *, url: str | None = None) -> None:
        self.url = url

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
        symbol = form["stock"].split(",", maxsplit=1)[0]
        url = self.url or f"finalpage/2022-08-01/{symbol}.PDF"
        return (
            "{"
            '"totalpages":1,'
            '"totalAnnouncement":1,'
            '"announcements":[{'
            f'"announcementId":"{form["stock"]}-announcement",'
            '"announcementTitle":"公开披露公告",'
            '"announcementTime":1659312000000,'
            f'"adjunctUrl":"{url}"'
            "}]}"
        ).encode()


class DocumentTransport:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def fetch(self, url: str, *, timeout_seconds: float) -> bytes:
        del timeout_seconds
        self.urls.append(url)
        return f"official document for {url}".encode()


class FailIfCalledDocumentTransport:
    def fetch(self, url: str, *, timeout_seconds: float) -> bytes:
        del url, timeout_seconds
        raise AssertionError("immutable cached document must not be fetched again")


class ScriptedDocumentTransport:
    def __init__(self, responses: list[bytes | BaseException]) -> None:
        self.responses = iter(responses)
        self.urls: list[str] = []

    def fetch(self, url: str, *, timeout_seconds: float) -> bytes:
        del timeout_seconds
        self.urls.append(url)
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


def _complete_query_coverage(
    attempt: PreparedAttempt,
    *,
    url: str | None = None,
) -> EvidenceInputs:
    from ashare_multifactor.final_test.official_query_collector import (
        collect_official_query_coverage,
    )

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
    output_root = (
        attempt.data_root / "processed/final_test_evidence" / attempt.attempt_id
    )
    result = collect_official_query_coverage(
        preparation=preparation,
        authorization=authorization,
        contract=load_action_source_contract(
            attempt.code_root / "configs/final_execution_sources.yaml"
        ),
        output_root=output_root,
        transport=AnnouncementTransport(url=url),
        policy=RetryPolicy(attempts=1, timeout_seconds=0.1, minimum_interval_seconds=0),
    )
    assert result.index_path is not None
    return EvidenceInputs(
        preparation=preparation,
        authorization=authorization,
        contract=load_action_source_contract(
            attempt.code_root / "configs/final_execution_sources.yaml"
        ),
        output_root=output_root,
        index_path=result.index_path,
    )


def test_catalog_preserves_query_provenance_without_creating_event_facts(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_announcement_catalog import (
        build_announcement_catalog,
    )

    inputs = _complete_query_coverage(prepared_attempt)
    catalog = build_announcement_catalog(
        inputs.index_path,
        preparation=inputs.preparation,
        authorization=inputs.authorization,
        contract=inputs.contract,
        destination=inputs.output_root,
    )

    row = pl.read_parquet(catalog).row(0, named=True)
    assert {
        "announcement_id",
        "source_response",
        "source_url",
        "symbol",
        "market",
    } <= row.keys()
    assert "effective_date" not in row
    assert "cash_per_share" not in row


def test_unreviewed_documents_block_ready_coverage(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_announcement_catalog import (
        build_announcement_catalog,
    )
    from ashare_multifactor.final_test.official_document_fetcher import (
        fetch_official_documents,
    )
    from ashare_multifactor.final_test.official_evidence_workspace import (
        validate_review_submission,
    )

    inputs = _complete_query_coverage(prepared_attempt)
    catalog = build_announcement_catalog(
        inputs.index_path,
        preparation=inputs.preparation,
        authorization=inputs.authorization,
        contract=inputs.contract,
        destination=inputs.output_root,
    )
    workspace = fetch_official_documents(
        catalog,
        destination=inputs.output_root,
        transport=DocumentTransport(),
    )

    assert workspace.ready is False
    assert (
        pl.read_parquet(workspace.review_queue_path)
        .get_column("review_status")
        .unique()
        .to_list()
        == ["needs_review"]
    )
    with pytest.raises(ValueError, match="review|ready|official"):
        validate_review_submission(workspace, workspace.review_queue_path)


def test_document_cache_is_immutable(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_announcement_catalog import (
        build_announcement_catalog,
    )
    from ashare_multifactor.final_test.official_document_fetcher import (
        fetch_official_documents,
    )

    inputs = _complete_query_coverage(prepared_attempt)
    catalog = build_announcement_catalog(
        inputs.index_path,
        preparation=inputs.preparation,
        authorization=inputs.authorization,
        contract=inputs.contract,
        destination=inputs.output_root,
    )
    first = fetch_official_documents(
        catalog,
        destination=inputs.output_root,
        transport=DocumentTransport(),
    )
    cached = sorted(
        (inputs.output_root / "official_document_workspace/documents").rglob("*.bin")
    )
    bytes_before = {path.name: path.read_bytes() for path in cached}

    second = fetch_official_documents(
        catalog,
        destination=inputs.output_root,
        transport=FailIfCalledDocumentTransport(),
    )

    assert second.review_queue_path == first.review_queue_path
    assert {path.name: path.read_bytes() for path in cached} == bytes_before


def test_document_fetcher_retries_and_waits_after_success(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_announcement_catalog import (
        build_announcement_catalog,
    )
    from ashare_multifactor.final_test.official_document_fetcher import (
        DocumentFetchPolicy,
        OfficialDocumentTransientError,
        fetch_official_documents,
    )

    inputs = _complete_query_coverage(prepared_attempt)
    catalog = build_announcement_catalog(
        inputs.index_path,
        preparation=inputs.preparation,
        authorization=inputs.authorization,
        contract=inputs.contract,
        destination=inputs.output_root,
    )
    pauses: list[float] = []
    transport = ScriptedDocumentTransport(
        [OfficialDocumentTransientError("temporary"), b"official bytes"]
    )

    workspace = fetch_official_documents(
        catalog,
        destination=inputs.output_root,
        transport=transport,
        max_documents=1,
        policy=DocumentFetchPolicy(
            attempts=2,
            timeout_seconds=0.1,
            minimum_interval_seconds=0.5,
        ),
        sleep=pauses.append,
    )

    assert workspace.ready is False
    assert len(transport.urls) == 2
    assert pauses == [0.5, 0.5]


def test_document_fetcher_resumes_with_the_next_uncached_url(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_announcement_catalog import (
        build_announcement_catalog,
    )
    from ashare_multifactor.final_test.official_document_fetcher import (
        DocumentFetchPolicy,
        fetch_official_documents,
    )

    inputs = _complete_query_coverage(prepared_attempt)
    catalog = build_announcement_catalog(
        inputs.index_path,
        preparation=inputs.preparation,
        authorization=inputs.authorization,
        contract=inputs.contract,
        destination=inputs.output_root,
    )
    transport = DocumentTransport()
    policy = DocumentFetchPolicy(
        attempts=1,
        timeout_seconds=0.1,
        minimum_interval_seconds=0,
    )

    first = fetch_official_documents(
        catalog,
        destination=inputs.output_root,
        transport=transport,
        max_documents=1,
        policy=policy,
    )
    second = fetch_official_documents(
        catalog,
        destination=inputs.output_root,
        transport=transport,
        max_documents=1,
        policy=policy,
    )

    assert first.ready is second.ready is False
    assert len(transport.urls) == 2
    assert transport.urls[0] != transport.urls[1]


def test_urllib_document_transport_classifies_remote_disconnect_as_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ashare_multifactor.final_test import official_document_fetcher as fetcher

    class Opener:
        def open(self, *_: object, **__: object) -> object:
            raise RemoteDisconnected("official document endpoint closed connection")

    monkeypatch.setattr(fetcher, "build_opener", lambda *_: Opener())

    with pytest.raises(fetcher.OfficialDocumentTransientError, match="transport"):
        fetcher.UrllibOfficialDocumentTransport().fetch(
            "https://static.cninfo.com.cn/finalpage/2022-01-01/notice.PDF",
            timeout_seconds=0.1,
        )



def test_catalog_rejects_non_official_urls(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_announcement_catalog import (
        build_announcement_catalog,
    )

    bad_inputs = _complete_query_coverage(
        prepared_attempt,
        url="https://example.invalid/document.pdf",
    )
    with pytest.raises(ValueError, match="official.*URL|URL"):
        build_announcement_catalog(
            bad_inputs.index_path,
            preparation=bad_inputs.preparation,
            authorization=bad_inputs.authorization,
            contract=bad_inputs.contract,
            destination=bad_inputs.output_root,
        )


def test_catalog_rejects_official_prefix_url_with_query_or_fragment(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_announcement_catalog import (
        build_announcement_catalog,
    )

    inputs = _complete_query_coverage(
        prepared_attempt,
        url="finalpage/2022-08-01/1210000000.PDF?redirect=outside",
    )

    with pytest.raises(ValueError, match="official evidence URL|invalid official"):
        build_announcement_catalog(
            inputs.index_path,
            preparation=inputs.preparation,
            authorization=inputs.authorization,
            contract=inputs.contract,
            destination=inputs.output_root,
        )


def test_document_fetcher_rechecks_query_collection_binding_before_network(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_announcement_catalog import (
        build_announcement_catalog,
    )
    from ashare_multifactor.final_test.official_document_fetcher import (
        fetch_official_documents,
    )

    inputs = _complete_query_coverage(prepared_attempt)
    catalog = build_announcement_catalog(
        inputs.index_path,
        preparation=inputs.preparation,
        authorization=inputs.authorization,
        contract=inputs.contract,
        destination=inputs.output_root,
    )
    collection = inputs.output_root / "official_query_collection.json"
    collection.write_bytes(collection.read_bytes() + b" ")

    with pytest.raises(ValueError, match="query collection|identity"):
        fetch_official_documents(
            catalog,
            destination=inputs.output_root,
            transport=FailIfCalledDocumentTransport(),
        )


def test_document_fetcher_rechecks_identity_binding_before_network(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_announcement_catalog import (
        build_announcement_catalog,
    )
    from ashare_multifactor.final_test.official_document_fetcher import (
        fetch_official_documents,
    )

    inputs = _complete_query_coverage(prepared_attempt)
    catalog = build_announcement_catalog(
        inputs.index_path,
        preparation=inputs.preparation,
        authorization=inputs.authorization,
        contract=inputs.contract,
        destination=inputs.output_root,
    )
    identities = (
        inputs.output_root
        / "official_security_identities"
        / "official_security_identities.json"
    )
    identities.write_bytes(identities.read_bytes() + b" ")

    with pytest.raises(ValueError, match="identity|catalog"):
        fetch_official_documents(
            catalog,
            destination=inputs.output_root,
            transport=FailIfCalledDocumentTransport(),
        )
