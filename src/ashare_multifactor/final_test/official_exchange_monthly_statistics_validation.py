"""Pure parsing and bounded canonical checks for SZSE monthly statistics."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re

from ashare_multifactor.final_test.official_candidate_pdf_evidence import (
    CandidatePdfEvidence,
    inspect_candidate_pdf,
)
from ashare_multifactor.final_test.official_document_validation import (
    validate_official_document,
)
from ashare_multifactor.final_test.official_evidence_workspace import (
    DOCUMENTS_DIRECTORY,
    EvidenceWorkspace,
    VerifiedReviewQueue,
    load_cached_document,
)
from ashare_multifactor.final_test.official_exchange_monthly_statistics_authorization import (
    AuthorizedStatisticsRequest,
)
from ashare_multifactor.final_test.official_query_coverage import canonical_json_bytes


_CAPTION_MONTH = re.compile(
    r"DIVIDEND,BONUSANDRIGHTSISSUES-[（(]"
    r"(?P<year>\d{4})\.(?P<month>\d{2})[）)]",
    re.I,
)
_STRONG_REASON_PREFIX = "corporate_action_strong_implementation:"
_RECORD_DATE = re.compile(
    r"(?:股权登记日|股权日)(?:为)?[:：]?\s*"
    r"(?P<year>\d{4})(?:年|[-/.])(?P<month>\d{1,2})(?:月|[-/.])"
    r"(?P<day>\d{1,2})(?:日)?"
)
_DATE_SECTION_HEADING = "三、股权登记日与除权除息日"
_NEXT_SECTION_HEADING = "四、"
_CNINFO_DATE = re.compile(r"/finalpage/(?P<value>\d{4}-\d{2}-\d{2})/")
_IMMUTABLE_ID = re.compile(r"[0-9a-f]{64}")
_MEDIA_TYPE = "text/html; charset=GBK"


@dataclass(frozen=True)
class ParsedStatisticsRow:
    """The sole 14-column row for the authorized security."""

    symbol: str
    ex_date: date
    record_date: date
    cash_per_share: Decimal
    share_ratio: Decimal
    cells: tuple[str, ...]
    source_period_end: date


class _StatisticsHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[tuple[str, list[list[str]]]] = []
        self._depth = 0
        self._table_text: list[str] = []
        self._rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag == "table":
            if self._depth == 0:
                self._table_text = []
                self._rows = []
            self._depth += 1
        elif self._depth == 1 and tag == "tr":
            self._row = []
        elif self._depth == 1 and self._row is not None and tag in {"td", "th"}:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._depth:
            self._table_text.append(data)
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._depth == 1 and tag in {"td", "th"} and self._cell is not None:
            if self._row is None:
                raise ValueError("exchange statistics HTML cell is malformed")
            self._row.append(_compact("".join(self._cell)))
            self._cell = None
        elif self._depth == 1 and tag == "tr" and self._row is not None:
            self._rows.append(self._row)
            self._row = None
        elif tag == "table" and self._depth:
            self._depth -= 1
            if self._depth == 0:
                self.tables.append((_compact("".join(self._table_text)), self._rows))


def parse_unique_statistics_row(payload: bytes, *, symbol: str) -> ParsedStatisticsRow:
    """Decode exact GBK HTML and return one symbol-local 14-column row."""
    try:
        text = payload.decode("gbk", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("exchange statistics HTML is not GBK") from error
    if (
        not payload
        or text.encode("gbk") != payload
    ):
        raise ValueError("exchange statistics HTML metadata differs")
    parser = _StatisticsHtmlParser()
    try:
        parser.feed(text)
        parser.close()
    except (AssertionError, ValueError) as error:
        raise ValueError("exchange statistics HTML is malformed") from error
    eligible_tables = [
        (caption_match, rows)
        for table_text, rows in parser.tables
        if (caption_match := _CAPTION_MONTH.search(table_text)) is not None
    ]
    if len(eligible_tables) != 1:
        raise ValueError("exchange statistics table title differs")
    caption_match, rows = eligible_tables[0]
    try:
        source_period_end = date(
            int(caption_match.group("year")),
            int(caption_match.group("month")),
            monthrange(
                int(caption_match.group("year")),
                int(caption_match.group("month")),
            )[1],
        )
    except (ValueError, IndexError) as error:
        raise ValueError("exchange statistics table month differs") from error
    if not rows or len(rows[0]) != 14:
        raise ValueError("exchange statistics table schema differs")
    header = rows[0]
    expected_header = (
        "代码Code",
        "证券简称Securities",
        "红股数量Bonus(Shs)",
        "送股率BPS",
        "现金息(元)CashDiv.",
        "每股派息DPS",
        "配股数RtsIssues",
        "配股率RPS",
        "配股价Pla.Pri.",
        "集资金额FundsRaised",
        "除净日期Ex-Date",
        "股权日Reg.Date",
        "除权报价Ex-Price",
        "前收市Pre-Closing",
    )
    if tuple(header) != expected_header or any(
        len(row) != 14 for row in rows[1:]
    ):
        raise ValueError("exchange statistics table schema differs")
    matches = [row for row in rows[1:] if row[0] == symbol]
    if len(matches) != 1:
        raise ValueError("exchange statistics target row is not unique")
    row = matches[0]
    return ParsedStatisticsRow(
        symbol=symbol,
        ex_date=_slash_date(row[10]),
        record_date=_slash_date(row[11]),
        cash_per_share=_decimal(row[5]),
        share_ratio=_decimal(row[3]),
        cells=tuple(row),
        source_period_end=source_period_end,
    )


def build_verified_statistics_record(
    *,
    workspace: EvidenceWorkspace,
    queue: VerifiedReviewQueue,
    authorization_payload: dict[str, object],
    authorization_sha256: str,
    request: AuthorizedStatisticsRequest,
    html_bytes: bytes,
    receipt_bytes: bytes,
) -> dict[str, object]:
    """Validate source row, receipt, candidate fields, and one causal PDF."""
    candidate = authorization_payload["candidate"]
    if not isinstance(candidate, dict):
        raise ValueError("exchange statistics candidate binding differs")
    row = parse_unique_statistics_row(
        html_bytes,
        symbol=str(candidate["symbol"]),
    )
    expected_cash = _decimal(str(candidate["cash_per_share"]))
    expected_share = _decimal(str(candidate["share_ratio"]))
    if (
        row.source_period_end.isoformat()
        != authorization_payload.get("source_period_end")
        or
        row.ex_date.isoformat() != candidate.get("ex_date")
        or row.cash_per_share != expected_cash
        or row.share_ratio != expected_share
    ):
        raise ValueError("exchange statistics row differs from candidate")
    _validate_receipt(
        receipt_bytes,
        html_bytes=html_bytes,
        authorization_sha256=authorization_sha256,
        request=request,
    )
    canonical = _unique_canonical_announcement(
        workspace,
        queue,
        related_catalog_ids=tuple(authorization_payload["related_catalog_ids"]),
        symbol=row.symbol,
        candidate_ex_date=row.ex_date,
        record_date=row.record_date,
        cash_per_share=row.cash_per_share,
        share_ratio=row.share_ratio,
    )
    return {
        "candidate_id": str(candidate["candidate_id"]),
        "candidate": dict(candidate),
        "source": "shenzhen_stock_exchange",
        "source_policy": "szse_monthly_market_statistics_v1",
        "source_url": request.source_url,
        "source_period_end": authorization_payload["source_period_end"],
        "asset_path_date": authorization_payload["asset_path_date"],
        "post_event": True,
        "row": {
            "symbol": row.symbol,
            "ex_date": row.ex_date.isoformat(),
            "record_date": row.record_date.isoformat(),
            "cash_per_share": _decimal_token(row.cash_per_share),
            "share_ratio": _decimal_token(row.share_ratio),
            "column_count": len(row.cells),
            "row_sha256": hashlib.sha256(
                canonical_json_bytes(list(row.cells))
            ).hexdigest(),
        },
        "canonical_announcement": canonical,
        "document": {
            "path": "statistics.html",
            "sha256": hashlib.sha256(html_bytes).hexdigest(),
            "size_bytes": len(html_bytes),
            "media_type": _MEDIA_TYPE,
        },
        "receipt": {
            "path": "statistics.receipt.json",
            "sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "size_bytes": len(receipt_bytes),
        },
    }


def validate_verified_statistics_record(
    record: object,
    *,
    workspace: EvidenceWorkspace,
    queue: VerifiedReviewQueue,
    authorization_payload: dict[str, object],
    authorization_sha256: str,
    request: AuthorizedStatisticsRequest,
    files: dict[str, bytes],
) -> None:
    """Rebuild a frozen statistics record from its exact source bytes."""
    if not isinstance(record, dict) or set(files) != {
        "statistics.html",
        "statistics.receipt.json",
        "statistics_manifest.json",
    }:
        raise ValueError("exchange statistics evidence inventory differs")
    expected = build_verified_statistics_record(
        workspace=workspace,
        queue=queue,
        authorization_payload=authorization_payload,
        authorization_sha256=authorization_sha256,
        request=request,
        html_bytes=files["statistics.html"],
        receipt_bytes=files["statistics.receipt.json"],
    )
    if record != expected:
        raise ValueError("exchange statistics evidence record differs")


def _unique_canonical_announcement(
    workspace: EvidenceWorkspace,
    queue: VerifiedReviewQueue,
    *,
    related_catalog_ids: tuple[str, ...],
    symbol: str,
    candidate_ex_date: date,
    record_date: date,
    cash_per_share: Decimal,
    share_ratio: Decimal,
) -> dict[str, object]:
    related = queue.frame.filter(
        queue.frame["catalog_id"].is_in(related_catalog_ids)
    ).to_dicts()
    if len(related) != len(related_catalog_ids):
        raise ValueError("exchange statistics canonical scope differs")
    matches: list[dict[str, object]] = []
    descriptor = os.open(
        workspace.root / DOCUMENTS_DIRECTORY,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        for anchored in related:
            if (
                anchored["symbol"] != symbol
                or anchored["route"] != "candidate"
                or anchored["candidate_type"] != "corporate_action"
                or not str(anchored["routing_reason"]).startswith(
                    _STRONG_REASON_PREFIX
                )
                or anchored["document_status"] != "cached"
            ):
                continue
            publication_date = _publication_date(str(anchored["source_url"]))
            if publication_date > candidate_ex_date:
                continue
            cached = load_cached_document(
                descriptor,
                source_url=str(anchored["source_url"]),
            )
            if cached is None:
                continue
            payload_path = workspace.root / cached.cache_path
            cache_relative = tuple(Path(cached.cache_path).parts)
            if (
                len(cache_relative) != 3
                or cache_relative[0] != DOCUMENTS_DIRECTORY
                or _IMMUTABLE_ID.fullmatch(cache_relative[1]) is None
                or cache_relative[2] != "document.bin"
                or payload_path.is_symlink()
                or not payload_path.is_file()
                or payload_path.resolve()
                != (
                    workspace.root.resolve()
                    / DOCUMENTS_DIRECTORY
                    / cache_relative[1]
                    / "document.bin"
                )
                or any(
                    ancestor.is_symlink()
                    for ancestor in (
                        payload_path.parent,
                        payload_path.parent.parent,
                        workspace.root,
                    )
                )
            ):
                raise ValueError("exchange statistics canonical path is invalid")
            payload = payload_path.read_bytes()
            document = validate_official_document(
                payload,
                source_url=cached.source_url,
            )
            evidence = inspect_candidate_pdf(payload)
            if (
                cached.cache_path != anchored["document_cache_path"]
                or document.sha256 != anchored["document_sha256"]
                or document.size_bytes != anchored["document_size_bytes"]
                or document.page_count != anchored["document_page_count"]
                or document.media_type != anchored["document_media_type"]
                or evidence.strong_reason is None
                or not evidence.has_unique_leading_symbol(symbol)
                or _bounded_record_dates(evidence) != (record_date,)
                or not _distribution_matches(
                    evidence,
                    cash_per_share=cash_per_share,
                    share_ratio=share_ratio,
                )
            ):
                continue
            matches.append(
                {
                    "catalog_id": str(anchored["catalog_id"]),
                    "announcement_id": str(anchored["announcement_id"]),
                    "symbol": symbol,
                    "source_url": str(anchored["source_url"]),
                    "publication_date": publication_date.isoformat(),
                    "document_sha256": document.sha256,
                    "strong_reason": evidence.strong_reason,
                    "record_date": record_date.isoformat(),
                    "distribution_terms": list(evidence.distribution_terms),
                }
            )
    finally:
        os.close(descriptor)
    if len(matches) != 1:
        raise ValueError("exchange statistics canonical announcement is not unique")
    return matches[0]


def _distribution_matches(
    evidence: CandidatePdfEvidence,
    *,
    cash_per_share: Decimal,
    share_ratio: Decimal,
) -> bool:
    values: dict[str, Decimal] = {}
    for token in evidence.distribution_terms:
        name, separator, raw = token.partition("=")
        if not separator:
            return False
        values[name] = _decimal(raw)
    bonus = values.get("bonus_per_10", Decimal(0))
    transfer = values.get("transfer_per_10", Decimal(0))
    return values.get("cash_per_10") == cash_per_share * 10 and (
        bonus + transfer == share_ratio * 10
    )


def _bounded_record_dates(evidence: CandidatePdfEvidence) -> tuple[date, ...]:
    text = evidence.text
    headings = [
        match.start()
        for match in re.finditer(re.escape(_DATE_SECTION_HEADING), text)
    ]
    if len(headings) != 1:
        return ()
    section_start = headings[0] + len(_DATE_SECTION_HEADING)
    section_end = text.find(_NEXT_SECTION_HEADING, section_start)
    if section_end < 0:
        return ()
    section = text[section_start:section_end]
    values: list[date] = []
    for match in _RECORD_DATE.finditer(section):
        try:
            values.append(
                date(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                )
            )
        except ValueError:
            return ()
    return tuple(values) if len(values) == 1 else ()


def _validate_receipt(
    raw: bytes,
    *,
    html_bytes: bytes,
    authorization_sha256: str,
    request: AuthorizedStatisticsRequest,
) -> None:
    try:
        receipt = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("exchange statistics receipt differs") from error
    request_record = receipt.get("request") if isinstance(receipt, dict) else None
    document = receipt.get("document") if isinstance(receipt, dict) else None
    attempt_count = (
        request_record.get("attempt_count")
        if isinstance(request_record, dict)
        else None
    )
    if (
        not isinstance(receipt, dict)
        or raw != canonical_json_bytes(receipt)
        or receipt.get("schema")
        != "stage9_exchange_monthly_statistics_get_receipt/v1"
        or receipt.get("role") != "exchange_monthly_statistics_http_get_receipt"
        or receipt.get("network_authorization_sha256") != authorization_sha256
        or receipt.get("final_test_strategy_outputs_read") is not False
        or not isinstance(attempt_count, int)
        or isinstance(attempt_count, bool)
        or not 1 <= attempt_count <= request.max_attempts
        or request_record
        != {
            "purpose": "exchange_monthly_statistics",
            "method": "GET",
            "source_url": request.source_url,
            "request_sha256": request.request_sha256,
            "attempt_count": attempt_count,
            "http_status": 200,
            "final_url": request.source_url,
            "redirect_followed_count": 0,
        }
        or document
        != {
            "sha256": hashlib.sha256(html_bytes).hexdigest(),
            "size_bytes": len(html_bytes),
            "media_type": _MEDIA_TYPE,
        }
    ):
        raise ValueError("exchange statistics receipt differs")


def _publication_date(source_url: str) -> date:
    match = _CNINFO_DATE.search(source_url)
    if match is None:
        raise ValueError("exchange statistics canonical URL differs")
    return date.fromisoformat(match.group("value"))


def _slash_date(value: str) -> date:
    if re.fullmatch(r"\d{4}/\d{2}/\d{2}", value) is None:
        raise ValueError("exchange statistics date cell differs")
    return date.fromisoformat(value.replace("/", "-"))


def _decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("exchange statistics decimal cell differs") from error
    if not parsed.is_finite():
        raise ValueError("exchange statistics decimal cell differs")
    return parsed


def _decimal_token(value: Decimal) -> str:
    rendered = format(value.normalize(), "f")
    return "0" if value == 0 else rendered


def _compact(value: str) -> str:
    return "".join(character for character in value if not character.isspace())
