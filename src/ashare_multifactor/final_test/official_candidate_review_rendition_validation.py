"""Validation policy for one candidate-review publisher rendition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from ashare_multifactor.final_test.official_candidate_pdf_evidence import (
    CandidatePdfEvidence,
    inspect_candidate_pdf,
)
from ashare_multifactor.final_test.official_candidate_review_rendition_authorization import (
    AuthorizedRenditionRequest,
)
from ashare_multifactor.final_test.official_document_validation import (
    ValidatedOfficialDocument,
    validate_official_document,
)
from ashare_multifactor.final_test.official_query_coverage import canonical_json_bytes


_CNINFO_PATH = re.compile(r"/finalpage/(\d{4}-\d{2}-\d{2})/[^/]+\.PDF")
_STCN_PATH = re.compile(
    r"/att/(\d{4})(\d{2})/(\d{2})/[A-Za-z0-9_-]+_eBook\.pdf"
)


@dataclass(frozen=True)
class CandidateReviewRenditionInput:
    """One downloaded rendition plus pre-event publisher-authority evidence."""

    canonical_catalog_id: str
    source_url: str
    publication_date: date
    document_path: Path
    receipt_path: Path
    authority_source_url: str
    authority_publication_date: date
    authority_document_path: Path
    authority_receipt_path: Path


def build_rendition_record(
    item: CandidateReviewRenditionInput,
    *,
    anchored: dict[str, object],
    canonical_payload: bytes,
    authorization_sha256: str,
    rendition_request: AuthorizedRenditionRequest,
    authority_request: AuthorizedRenditionRequest,
) -> tuple[dict[str, object], dict[str, bytes]]:
    """Validate source files and derive one immutable rendition record."""
    symbol = str(anchored["symbol"])
    _validate_input_request(
        item,
        rendition_request=rendition_request,
        authority_request=authority_request,
    )
    canonical_url = str(anchored["source_url"])
    canonical_date = _validate_cninfo_url(canonical_url)
    _validate_stcn_url(item.source_url, item.publication_date)
    if item.publication_date != canonical_date:
        raise ValueError(
            "candidate review rendition publication differs from same announcement"
        )
    _validate_cninfo_url(
        item.authority_source_url,
        expected_date=item.authority_publication_date,
    )
    if item.authority_publication_date >= item.publication_date:
        raise ValueError("candidate review rendition authority is not pre-event")
    canonical_document = validate_official_document(
        canonical_payload,
        source_url=canonical_url,
    )
    canonical_evidence = inspect_candidate_pdf(canonical_payload)
    if _canonical_document_record(anchored) != {
        "cache_path": anchored["document_cache_path"],
        "sha256": canonical_document.sha256,
        "size_bytes": canonical_document.size_bytes,
        "page_count": canonical_document.page_count,
        "media_type": canonical_document.media_type,
    }:
        raise ValueError("candidate review canonical document differs")
    document, document_bytes, evidence = _validated_pdf(
        item.document_path,
        source_url=item.source_url,
    )
    if (
        evidence.strong_reason is None
        or not evidence.has_unique_leading_symbol(symbol)
        or not evidence.labelled_dates
    ):
        raise ValueError("candidate review rendition lacks single-document evidence")
    linkage = _same_announcement_linkage(
        canonical_evidence,
        evidence,
        symbol=symbol,
        publication_date=canonical_date,
    )
    receipt_bytes, receipt_sha256 = _validated_receipt(
        item.receipt_path,
        source_url=item.source_url,
        authorization_sha256=authorization_sha256,
        document=document,
        request=rendition_request,
    )
    authority, authority_bytes, authority_evidence = _validated_pdf(
        item.authority_document_path,
        source_url=item.authority_source_url,
    )
    authority_clause = _publisher_authority_clause(
        authority_evidence,
        symbol=symbol,
    )
    if authority_clause is None:
        raise ValueError("candidate review rendition authority evidence differs")
    authority_receipt_bytes, authority_receipt_sha256 = _validated_receipt(
        item.authority_receipt_path,
        source_url=item.authority_source_url,
        authorization_sha256=authorization_sha256,
        document=authority,
        request=authority_request,
    )
    record = {
        "canonical_announcement": {
            "catalog_id": item.canonical_catalog_id,
            "announcement_id": anchored["announcement_id"],
            "source_url": anchored["source_url"],
            "symbol": symbol,
            "publication_date": canonical_date.isoformat(),
            "document": _canonical_document_record(anchored),
        },
        "publisher": "securities_times",
        "scope": "candidate_review_admission_only",
        "source_url": item.source_url,
        "publication_date": item.publication_date.isoformat(),
        "document": _document_record(document),
        "receipt": _receipt_record(receipt_sha256, receipt_bytes),
        "strong_reason": evidence.strong_reason,
        "labelled_dates": [
            value.isoformat() for value in evidence.labelled_dates
        ],
        "same_announcement_linkage": linkage,
        "authority": {
            "source_url": item.authority_source_url,
            "publication_date": item.authority_publication_date.isoformat(),
            "document": _document_record(authority),
            "receipt": _receipt_record(
                authority_receipt_sha256,
                authority_receipt_bytes,
            ),
            "designation_clause_sha256": hashlib.sha256(
                authority_clause.encode("utf-8")
            ).hexdigest(),
            "designation_clause_size_bytes": len(
                authority_clause.encode("utf-8")
            ),
            "bounded_block_sha256": hashlib.sha256(
                authority_evidence.leading_block.encode("utf-8")
            ).hexdigest(),
        },
    }
    files = {
        str(record["document"]["path"]): document_bytes,
        str(record["receipt"]["path"]): receipt_bytes,
        str(record["authority"]["document"]["path"]): authority_bytes,
        str(record["authority"]["receipt"]["path"]): authority_receipt_bytes,
    }
    return record, files


def validate_rendition_record(
    record: object,
    *,
    files: dict[str, bytes],
    anchored: dict[str, object],
    canonical_payload: bytes,
    authorization_sha256: str,
    rendition_request: AuthorizedRenditionRequest,
    authority_request: AuthorizedRenditionRequest,
) -> set[str]:
    """Revalidate one frozen record and return its exact inventory."""
    if not isinstance(record, dict):
        raise ValueError("candidate review rendition record is invalid")
    canonical = record.get("canonical_announcement")
    document_record = record.get("document")
    receipt_record = record.get("receipt")
    authority = record.get("authority")
    if (
        not isinstance(canonical, dict)
        or not isinstance(document_record, dict)
        or not isinstance(receipt_record, dict)
        or not isinstance(authority, dict)
        or record.get("publisher") != "securities_times"
        or record.get("scope") != "candidate_review_admission_only"
    ):
        raise ValueError("candidate review rendition record is invalid")
    canonical_url = str(anchored["source_url"])
    canonical_date = _validate_cninfo_url(canonical_url)
    canonical_document = validate_official_document(
        canonical_payload,
        source_url=canonical_url,
    )
    expected_canonical = {
        "catalog_id": anchored["catalog_id"],
        "announcement_id": anchored["announcement_id"],
        "source_url": canonical_url,
        "symbol": anchored["symbol"],
        "publication_date": canonical_date.isoformat(),
        "document": _canonical_document_record(anchored),
    }
    if (
        canonical != expected_canonical
        or _canonical_document_record(anchored)
        != {
            "cache_path": anchored["document_cache_path"],
            "sha256": canonical_document.sha256,
            "size_bytes": canonical_document.size_bytes,
            "page_count": canonical_document.page_count,
            "media_type": canonical_document.media_type,
        }
    ):
        raise ValueError("candidate review canonical document differs")
    publication_date = _parse_date(record.get("publication_date"))
    source_url = str(record.get("source_url", ""))
    _validate_stcn_url(source_url, publication_date)
    if publication_date != canonical_date:
        raise ValueError(
            "candidate review rendition publication differs from same announcement"
        )
    document_bytes = _bound_file(files, document_record, prefix="documents/")
    document = validate_official_document(document_bytes, source_url=source_url)
    evidence = inspect_candidate_pdf(document_bytes)
    linkage = _same_announcement_linkage(
        inspect_candidate_pdf(canonical_payload),
        evidence,
        symbol=str(anchored["symbol"]),
        publication_date=canonical_date,
    )
    if (
        _document_record(document) != document_record
        or evidence.strong_reason is None
        or evidence.strong_reason != record.get("strong_reason")
        or [value.isoformat() for value in evidence.labelled_dates]
        != record.get("labelled_dates")
        or not evidence.has_unique_leading_symbol(str(anchored["symbol"]))
        or record.get("same_announcement_linkage") != linkage
    ):
        raise ValueError("candidate review rendition document differs")
    receipt_bytes = _bound_file(files, receipt_record, prefix="receipts/")
    _validate_receipt_bytes(
        receipt_bytes,
        source_url=source_url,
        authorization_sha256=authorization_sha256,
        document=document,
        request=rendition_request,
    )
    authority_document = authority.get("document")
    authority_receipt = authority.get("receipt")
    if not isinstance(authority_document, dict) or not isinstance(
        authority_receipt,
        dict,
    ):
        raise ValueError("candidate review rendition authority is invalid")
    authority_date = _parse_date(authority.get("publication_date"))
    authority_url = str(authority.get("source_url", ""))
    _validate_cninfo_url(authority_url, expected_date=authority_date)
    if authority_date >= publication_date:
        raise ValueError("candidate review rendition authority is not pre-event")
    authority_bytes = _bound_file(
        files,
        authority_document,
        prefix="documents/",
    )
    validated_authority = validate_official_document(
        authority_bytes,
        source_url=authority_url,
    )
    authority_evidence = inspect_candidate_pdf(authority_bytes)
    authority_clause = _publisher_authority_clause(
        authority_evidence,
        symbol=str(anchored["symbol"]),
    )
    if (
        _document_record(validated_authority) != authority_document
        or authority_clause is None
        or authority.get("designation_clause_sha256")
        != hashlib.sha256(authority_clause.encode("utf-8")).hexdigest()
        or authority.get("designation_clause_size_bytes")
        != len(authority_clause.encode("utf-8"))
        or authority.get("bounded_block_sha256")
        != hashlib.sha256(
            authority_evidence.leading_block.encode("utf-8")
        ).hexdigest()
    ):
        raise ValueError("candidate review rendition authority differs")
    authority_receipt_bytes = _bound_file(
        files,
        authority_receipt,
        prefix="receipts/",
    )
    _validate_receipt_bytes(
        authority_receipt_bytes,
        source_url=authority_url,
        authorization_sha256=authorization_sha256,
        document=validated_authority,
        request=authority_request,
    )
    return {
        str(document_record["path"]),
        str(receipt_record["path"]),
        str(authority_document["path"]),
        str(authority_receipt["path"]),
    }


def _validated_pdf(
    path: Path,
    *,
    source_url: str,
) -> tuple[ValidatedOfficialDocument, bytes, CandidatePdfEvidence]:
    _assert_regular(path, label="candidate review rendition PDF")
    payload = path.read_bytes()
    return (
        validate_official_document(payload, source_url=source_url),
        payload,
        inspect_candidate_pdf(payload),
    )


def _validated_receipt(
    path: Path,
    *,
    source_url: str,
    authorization_sha256: str,
    document: ValidatedOfficialDocument,
    request: AuthorizedRenditionRequest,
) -> tuple[bytes, str]:
    _assert_regular(path, label="candidate review rendition receipt")
    raw = path.read_bytes()
    _validate_receipt_bytes(
        raw,
        source_url=source_url,
        authorization_sha256=authorization_sha256,
        document=document,
        request=request,
    )
    return raw, hashlib.sha256(raw).hexdigest()


def _validate_receipt_bytes(
    raw: bytes,
    *,
    source_url: str,
    authorization_sha256: str,
    document: ValidatedOfficialDocument,
    request: AuthorizedRenditionRequest,
) -> None:
    receipt = _canonical_object(raw)
    document_record = receipt.get("document")
    request_record = receipt.get("request")
    attempt_count = (
        request_record.get("attempt_count")
        if isinstance(request_record, dict)
        else None
    )
    if (
        receipt.get("schema") != "stage9_candidate_evidence_get_receipt/v2"
        or receipt.get("role") != "candidate_evidence_http_get_receipt"
        or receipt.get("network_authorization_sha256") != authorization_sha256
        or receipt.get("final_test_strategy_outputs_read") is not False
        or source_url != request.source_url
        or not isinstance(attempt_count, int)
        or isinstance(attempt_count, bool)
        or not 1 <= attempt_count <= request.max_attempts
        or request_record
        != {
            "purpose": request.purpose,
            "method": "GET",
            "source_url": request.source_url,
            "request_sha256": request.request_sha256,
            "attempt_count": attempt_count,
            "http_status": 200,
            "final_url": request.source_url,
            "redirect_followed_count": 0,
        }
        or document_record
        != {
            "sha256": document.sha256,
            "size_bytes": document.size_bytes,
            "page_count": document.page_count,
            "media_type": document.media_type,
        }
    ):
        raise ValueError("candidate review rendition receipt differs")


def _validate_input_request(
    item: CandidateReviewRenditionInput,
    *,
    rendition_request: AuthorizedRenditionRequest,
    authority_request: AuthorizedRenditionRequest,
) -> None:
    if (
        rendition_request.purpose != "publisher_rendition"
        or authority_request.purpose != "publisher_authority"
        or item.source_url != rendition_request.source_url
        or item.document_path.absolute()
        != rendition_request.document_path.absolute()
        or item.receipt_path.absolute()
        != rendition_request.receipt_path.absolute()
        or item.authority_source_url != authority_request.source_url
        or item.authority_document_path.absolute()
        != authority_request.document_path.absolute()
        or item.authority_receipt_path.absolute()
        != authority_request.receipt_path.absolute()
    ):
        raise ValueError("candidate review rendition request differs")


def _publisher_authority_clause(
    evidence: CandidatePdfEvidence,
    *,
    symbol: str,
) -> str | None:
    if not evidence.contains_symbol(symbol):
        return None
    for line in evidence.leading_block.splitlines():
        for clause in re.split(r"[。；;]", line):
            compact = "".join(
                character for character in clause if not character.isspace()
            )
            if (
                re.search(r"(?:本?公司|发行人)", compact)
                and re.search(r"(?:选定|指定)", compact)
                and "证券时报" in compact
                and re.search(r"信息披露.{0,12}(?:媒体|报刊)", compact)
            ):
                return compact
    return None


def _same_announcement_linkage(
    canonical: CandidatePdfEvidence,
    rendition: CandidatePdfEvidence,
    *,
    symbol: str,
    publication_date: date,
) -> dict[str, object]:
    if (
        not canonical.contains_symbol(symbol)
        or not rendition.has_unique_leading_symbol(symbol)
        or not canonical.report_periods
        or canonical.report_periods != rendition.report_periods
        or not canonical.distribution_terms
        or canonical.distribution_terms != rendition.distribution_terms
    ):
        raise ValueError(
            "candidate review rendition does not prove the same announcement"
        )
    title_semantics = {
        "event_family": "corporate_action_distribution",
        "report_periods": list(canonical.report_periods),
    }
    distribution_terms = list(canonical.distribution_terms)
    identity: dict[str, object] = {
        "policy": "same_announcement_semantics_v1",
        "publication_date": publication_date.isoformat(),
        "symbol": symbol,
        "report_periods": list(canonical.report_periods),
        "title_semantics_sha256": hashlib.sha256(
            canonical_json_bytes(title_semantics)
        ).hexdigest(),
        "distribution_terms": distribution_terms,
        "distribution_terms_sha256": hashlib.sha256(
            canonical_json_bytes(distribution_terms)
        ).hexdigest(),
        "canonical_bounded_block_sha256": hashlib.sha256(
            canonical.leading_block.encode("utf-8")
        ).hexdigest(),
        "rendition_bounded_block_sha256": hashlib.sha256(
            rendition.leading_block.encode("utf-8")
        ).hexdigest(),
    }
    return {
        **identity,
        "identity_sha256": hashlib.sha256(
            canonical_json_bytes(identity)
        ).hexdigest(),
    }


def _validate_stcn_url(source_url: str, publication_date: date) -> None:
    parsed = urlsplit(source_url)
    match = _STCN_PATH.fullmatch(parsed.path)
    try:
        path_date = (
            date(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
            )
            if match is not None
            else None
        )
    except ValueError:
        path_date = None
    if (
        parsed.scheme != "https"
        or parsed.netloc != "epaper.stcn.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or path_date != publication_date
    ):
        raise ValueError("candidate review rendition publisher URL is invalid")


def _validate_cninfo_url(
    source_url: str,
    *,
    expected_date: date | None = None,
) -> date:
    parsed = urlsplit(source_url)
    match = _CNINFO_PATH.fullmatch(parsed.path)
    try:
        path_date = date.fromisoformat(match.group(1)) if match else None
    except ValueError:
        path_date = None
    if (
        parsed.scheme != "https"
        or parsed.netloc != "static.cninfo.com.cn"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or match is None
        or (expected_date is not None and path_date != expected_date)
    ):
        raise ValueError("candidate review rendition CNInfo URL is invalid")
    if path_date is None:
        raise ValueError("candidate review rendition CNInfo URL is invalid")
    return path_date


def _document_record(
    document: ValidatedOfficialDocument,
) -> dict[str, object]:
    return {
        "path": f"documents/{document.sha256}/document.bin",
        "sha256": document.sha256,
        "size_bytes": document.size_bytes,
        "page_count": document.page_count,
        "media_type": document.media_type,
    }


def _canonical_document_record(
    anchored: dict[str, object],
) -> dict[str, object]:
    return {
        "cache_path": anchored["document_cache_path"],
        "sha256": anchored["document_sha256"],
        "size_bytes": anchored["document_size_bytes"],
        "page_count": anchored["document_page_count"],
        "media_type": anchored["document_media_type"],
    }


def _receipt_record(sha256: str, payload: bytes) -> dict[str, object]:
    return {
        "path": f"receipts/{sha256}.json",
        "sha256": sha256,
        "size_bytes": len(payload),
    }


def _bound_file(
    files: dict[str, bytes],
    record: dict[str, object],
    *,
    prefix: str,
) -> bytes:
    path = str(record.get("path", ""))
    raw = files.get(path)
    if (
        not path.startswith(prefix)
        or raw is None
        or record.get("sha256") != hashlib.sha256(raw).hexdigest()
        or record.get("size_bytes") != len(raw)
    ):
        raise ValueError("candidate review rendition file binding differs")
    return raw


def _assert_regular(path: Path, *, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is missing or unsafe")
    for ancestor in path.parents:
        if ancestor.is_symlink():
            raise ValueError(f"{label} uses a symlink")


def _canonical_object(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("candidate review rendition artifact is invalid") from error
    if not isinstance(value, dict) or raw != canonical_json_bytes(value):
        raise ValueError("candidate review rendition artifact is invalid")
    return value


def _parse_date(value: object) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise ValueError("candidate review rendition date is invalid") from error
