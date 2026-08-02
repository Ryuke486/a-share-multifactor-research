from __future__ import annotations

from datetime import date, datetime, time, timedelta
import hashlib
from io import BytesIO
import json
from pathlib import Path
from urllib.parse import urlparse
import warnings
from zoneinfo import ZoneInfo

from matplotlib.backends.backend_pdf import FigureCanvasPdf
from matplotlib.figure import Figure
import polars as pl
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from ashare_multifactor.final_test.corporate_action_candidates import (
    collect_corporate_action_candidates,
)
from ashare_multifactor.final_test.action_source_contract import (
    load_action_source_contract,
)
from ashare_multifactor.final_test.official_announcement_catalog import (
    build_announcement_catalog,
)
from ashare_multifactor.final_test.official_document_fetcher import (
    fetch_official_documents,
)
from ashare_multifactor.final_test.official_candidate_review_rendition_authorization import (
    RENDITION_CACHE_DIRECTORY,
    AuthorizedRenditionRequest,
    build_candidate_review_rendition_authorization,
    load_candidate_review_rendition_authorization,
)
from ashare_multifactor.final_test.official_candidate_query_topology import (
    publish_candidate_query_topology,
)
from ashare_multifactor.final_test.official_exchange_monthly_statistics import (
    ExchangeMonthlyStatisticsInput,
    bind_exchange_monthly_statistics,
    publish_exchange_monthly_statistics,
)
from ashare_multifactor.final_test.official_exchange_monthly_statistics_authorization import (
    STATISTICS_CACHE_DIRECTORY,
    build_exchange_monthly_statistics_authorization,
    load_exchange_monthly_statistics_authorization,
)
from ashare_multifactor.final_test.official_exchange_monthly_statistics_validation import (
    build_verified_statistics_record,
    parse_unique_statistics_row,
)
from ashare_multifactor.final_test.official_evidence_workspace import (
    load_verified_review_queue,
)
from ashare_multifactor.final_test.official_review_submission import (
    load_verified_candidate_snapshot,
)
from test_final_test_official_evidence_workspace import _complete_query_coverage
from test_final_test_resume import PreparedAttempt


pytest_plugins = ("test_final_test_resume",)

_DATE_RULE_PATH = (
    Path(__file__).parents[1]
    / "configs/evidence/stage9_candidate_review_admission_date_rule.json"
)
_CONTRACT_PATH = Path(__file__).parents[1] / "configs/final_execution_sources.yaml"
_FIELDS = [
    "code",
    "dividOperateDate",
    "dividPayDate",
    "dividStockMarketDate",
    "dividCashPsBeforeTax",
    "dividStocksPs",
    "dividReserveToStockPs",
]
_STATISTICS_HEADERS = (
    "代码Code",
    "证券简称Securities",
    "红股数量Bonus(Shs)",
    "送股率BPS",
    "现金息(元)Cash Div.",
    "每股派息DPS",
    "配股数Rts Issues",
    "配股率RPS",
    "配股价Pla. Pri.",
    "集资金额Funds Raised",
    "除净日期Ex-Date",
    "股权日Reg. Date",
    "除权报价Ex-Price",
    "前收市Pre-Closing",
)
_VALID_STATISTICS_ROW = (
    "000001",
    "平安银行",
    "0",
    "0.000",
    "1000000.00",
    "0.100",
    "0",
    "0.000",
    "0.000",
    "0",
    "2024/05/24",
    "2024/05/23",
    "10.20",
    "10.30",
)


def _statistics_html(
    rows: tuple[tuple[str, ...], ...],
    *,
    headers: tuple[str, ...] = _STATISTICS_HEADERS,
    caption: str = "DIVIDEND,BONUS AND RIGHTS ISSUES - （2024.05）",
) -> bytes:
    return (
        "<style type=\"text/css\">table{border-collapse:collapse}</style>"
        f"<table><caption>{caption}</caption><tr>"
        + "".join(f"<th>{value}</th>" for value in headers)
        + "</tr>"
        + "".join(
            "<tr>"
            + "".join(f"<td>{value}</td>" for value in row)
            + "</tr>"
            for row in rows
        )
        + "</table>"
    ).encode("gbk")


def _exchange_statistics_authorization_inputs(
    prepared_attempt: PreparedAttempt,
    *,
    canonical_lines: list[str] | None = None,
    canonical_title: str = "2023年度权益分派实施公告",
    canonical_date: date = date(2024, 5, 17),
    canonical_payload: bytes | None = None,
):
    from ashare_multifactor.final_test.official_candidate_review_admission import (
        CandidateReviewAdmissionError,
        require_candidate_review_admission,
    )

    workspace, queue, candidates = _admission_inputs(
        prepared_attempt,
        titles={
            "000001": canonical_title,
            "600000": "2024年第三季度报告",
        },
        announcement_dates={
            "000001": canonical_date,
            "600000": date(2024, 6, 18),
        },
        document_payloads={
            "000001": canonical_payload
            or _unicode_pdf_bytes(
                canonical_lines
                or [
                    "证券代码：000001",
                    "2023年度权益分派实施公告",
                    "每10股派发现金红利1元（含税），不送红股，不转增股本。",
                    "三、股权登记日与除权除息日",
                    "股权登记日：2024年5月23日；除权除息日：2023年5月24日。",
                    "四、权益分派方法",
                ]
            ),
            "600000": _pdf_bytes("2024-07-01"),
        },
        candidate_symbols={"000001"},
        candidate_ex_dates={"000001": date(2024, 5, 24)},
    )
    with pytest.raises(CandidateReviewAdmissionError) as blocked:
        require_candidate_review_admission(
            workspace=workspace,
            queue=queue,
            candidates=candidates,
            date_rule_path=_DATE_RULE_PATH,
        )
    topology = publish_candidate_query_topology(
        destination=workspace.root.parent,
        candidate_manifest_path=candidates.manifest_path,
        blocked_admission_path=blocked.value.manifest_path,
        contract=load_action_source_contract(_CONTRACT_PATH),
    )
    cache_root = workspace.root.parent / STATISTICS_CACHE_DIRECTORY
    cache_root.mkdir()
    return workspace, queue, candidates, blocked.value, topology, cache_root


def _publish_exchange_statistics_fixture(
    prepared_attempt: PreparedAttempt,
    *,
    html: bytes,
    media_type: str = "text/html; charset=GBK",
    final_url: str | None = None,
    document_sha256: str | None = None,
    canonical_lines: list[str] | None = None,
    canonical_title: str = "2023年度权益分派实施公告",
    canonical_date: date = date(2024, 5, 17),
    canonical_payload: bytes | None = None,
):
    workspace, queue, candidates, blocked, topology, cache_root = (
        _exchange_statistics_authorization_inputs(
            prepared_attempt,
            canonical_lines=canonical_lines,
            canonical_title=canonical_title,
            canonical_date=canonical_date,
            canonical_payload=canonical_payload,
        )
    )
    source_url = (
        "https://docs.static.szse.cn/www/market/periodical/month/"
        "W020240607344713255806.html"
    )
    authorization_payload = build_exchange_monthly_statistics_authorization(
        code_root=prepared_attempt.code_root,
        workspace=workspace,
        queue=queue,
        blocked_admission_path=blocked.manifest_path,
        topology_manifest_path=topology.manifest_path,
        candidate_id=str(candidates.candidates.item(0, "candidate_id")),
        source_url=source_url,
        cache_root=cache_root,
    )
    authorization_path = cache_root / "statistics_authorization.json"
    _write_canonical_json(authorization_path, authorization_payload)
    authorization = load_exchange_monthly_statistics_authorization(
        authorization_path,
        workspace=workspace,
        queue=queue,
    )
    request = authorization.request
    request.document_path.parent.mkdir(parents=True)
    request.document_path.write_bytes(html)
    _write_canonical_json(
        request.receipt_path,
        {
            "schema": "stage9_exchange_monthly_statistics_get_receipt/v1",
            "role": "exchange_monthly_statistics_http_get_receipt",
            "network_authorization_sha256": authorization.sha256,
            "request": {
                "purpose": "exchange_monthly_statistics",
                "method": "GET",
                "source_url": source_url,
                "request_sha256": request.request_sha256,
                "attempt_count": 1,
                "http_status": 200,
                "final_url": source_url if final_url is None else final_url,
                "redirect_followed_count": 0,
            },
            "document": {
                "sha256": (
                    hashlib.sha256(html).hexdigest()
                    if document_sha256 is None
                    else document_sha256
                ),
                "size_bytes": len(html),
                "media_type": media_type,
            },
            "final_test_strategy_outputs_read": False,
        },
    )
    statistics = publish_exchange_monthly_statistics(
        workspace=workspace,
        queue=queue,
        network_authorization_path=authorization_path,
        statistics=ExchangeMonthlyStatisticsInput(
            source_url=source_url,
            document_path=request.document_path,
            receipt_path=request.receipt_path,
        ),
    )
    return workspace, queue, candidates, statistics


def _pdf_bytes(text: str = "") -> bytes:
    stream = BytesIO()
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    if text:
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        font_reference = writer._add_object(font)
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {NameObject("/F1"): font_reference}
                )
            }
        )
        contents = DecodedStreamObject()
        contents.set_data(
            f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
        )
        page[NameObject("/Contents")] = writer._add_object(contents)
    writer.write(stream)
    return stream.getvalue()


def _unicode_pdf_bytes(lines: list[str]) -> bytes:
    stream = BytesIO()
    figure = Figure(figsize=(8.5, 11))
    FigureCanvasPdf(figure)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        figure.text(0.1, 0.9, "\n".join(lines))
        figure.savefig(stream, format="pdf")
    return stream.getvalue()


class _AdmissionAnnouncementTransport:
    def __init__(
        self,
        *,
        titles: dict[str, str],
        announcement_dates: dict[str, date],
    ) -> None:
        self.titles = titles
        self.announcement_dates = announcement_dates

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
        announcement_date = self.announcement_dates[symbol]
        timestamp = int(
            datetime.combine(
                announcement_date,
                time.min,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ).timestamp()
            * 1000
        )
        return json.dumps(
            {
                "totalpages": 1,
                "totalAnnouncement": 1,
                "announcements": [
                    {
                        "announcementId": f"{symbol}-announcement",
                        "announcementTitle": self.titles[symbol],
                        "announcementTime": timestamp,
                        "adjunctUrl": (
                            f"finalpage/{announcement_date.isoformat()}/{symbol}.PDF"
                        ),
                    }
                ],
            },
            separators=(",", ":"),
        ).encode()


class _AdmissionDocumentTransport:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads

    def fetch(self, url: str, *, timeout_seconds: float) -> bytes:
        del timeout_seconds
        symbol = Path(urlparse(url).path).stem
        return self.payloads[symbol]


class _SplitRequirementAnnouncementTransport(_AdmissionAnnouncementTransport):
    def fetch(
        self,
        endpoint: str,
        form: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> bytes:
        if endpoint.endswith("/information/topSearch/query"):
            return super().fetch(
                endpoint,
                form,
                timeout_seconds=timeout_seconds,
            )
        del timeout_seconds
        symbol = form["stock"].split(",", maxsplit=1)[0]
        if symbol != "000001":
            return super().fetch(endpoint, form, timeout_seconds=0)
        announcements = [
            {
                "announcementId": "000001-prior",
                "announcementTitle": "2024年度权益分派实施公告",
                "announcementTime": int(
                    datetime.combine(
                        date(2024, 5, 28),
                        time.min,
                        tzinfo=ZoneInfo("Asia/Shanghai"),
                    ).timestamp()
                    * 1000
                ),
                "adjunctUrl": "finalpage/2024-05-28/000001-prior.PDF",
            },
            {
                "announcementId": "000001-post",
                "announcementTitle": "2024年度权益分派实施公告",
                "announcementTime": int(
                    datetime.combine(
                        date(2024, 6, 2),
                        time.min,
                        tzinfo=ZoneInfo("Asia/Shanghai"),
                    ).timestamp()
                    * 1000
                ),
                "adjunctUrl": "finalpage/2024-06-02/000001-post.PDF",
            },
        ]
        return json.dumps(
            {
                "totalpages": 1,
                "totalAnnouncement": len(announcements),
                "announcements": announcements,
            },
            separators=(",", ":"),
        ).encode()


def _admission_inputs(
    attempt: PreparedAttempt,
    *,
    titles: dict[str, str],
    announcement_dates: dict[str, date],
    document_payloads: dict[str, bytes],
    candidate_symbols: set[str],
    announcement_transport: object | None = None,
    candidate_ex_dates: dict[str, date] | None = None,
):
    inputs = _complete_query_coverage(
        attempt,
        transport=announcement_transport
        or _AdmissionAnnouncementTransport(
            titles=titles,
            announcement_dates=announcement_dates,
        ),
    )
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
        transport=_AdmissionDocumentTransport(document_payloads),
    )

    def query(
        code: str,
        year: int,
        year_type: str,
    ) -> tuple[list[str], list[list[str]]]:
        del year_type
        symbol = code.split(".", maxsplit=1)[1]
        candidate_ex_date = (
            candidate_ex_dates.get(symbol)
            if candidate_ex_dates is not None
            else None
        )
        if candidate_ex_date is None:
            candidate_ex_date = (
                date(2024, 6, 1) if symbol == "000001" else date(2024, 7, 1)
            )
        if year != candidate_ex_date.year or symbol not in candidate_symbols:
            return _FIELDS, []
        ex_date = candidate_ex_date.isoformat()
        return _FIELDS, [
            [code, ex_date, ex_date, "", "0.10", "0", "0"]
        ]

    collection = collect_corporate_action_candidates(
        preparation=inputs.preparation,
        authorization=inputs.authorization,
        contract=inputs.contract,
        output_root=inputs.output_root,
        query=query,
    )
    queue = load_verified_review_queue(workspace)
    candidates = load_verified_candidate_snapshot(
        collection.manifest_path,
        workspace=workspace,
    )
    return workspace, queue, candidates


def _scenario(
    name: str,
) -> tuple[
    dict[str, str],
    dict[str, date],
    dict[str, bytes],
    str,
]:
    quarterly = "2024年第三季度报告"
    strong = "2024年度权益分派实施公告"
    titles = {"000001": strong, "600000": quarterly}
    dates = {
        "000001": date(2024, 5, 28),
        "600000": date(2024, 6, 18),
    }
    payloads = {
        "000001": _pdf_bytes("2024-06-01"),
        "600000": _pdf_bytes("2024-07-01"),
    }
    if name == "weak_title":
        titles["000001"] = "关于2024年度权益分派的公告"
        return titles, dates, payloads, "no_strong_implementation_route"
    if name == "cross_symbol_pdf":
        titles = {"000001": quarterly, "600000": strong}
        payloads["600000"] = _pdf_bytes("000001 2024-06-01")
        return titles, dates, payloads, "no_same_symbol_announcement"
    if name == "invalid_or_missing_pdf":
        payloads["000001"] = b"<html>not an official PDF</html>"
        return titles, dates, payloads, "no_valid_cached_official_pdf"
    if name == "candidate_date_not_in_pdf":
        payloads["000001"] = _pdf_bytes("implementation announcement")
        return titles, dates, payloads, "candidate_date_not_found_in_pdf"
    raise AssertionError(f"unknown admission scenario: {name}")


def _write_canonical_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
    )


def _write_rendition_receipt(
    path: Path,
    *,
    source_url: str,
    payload: bytes,
    authorization_sha256: str,
    request: AuthorizedRenditionRequest,
    attempt_count: int = 1,
    final_url: str | None = None,
) -> None:
    from ashare_multifactor.final_test.official_document_validation import (
        validate_official_document,
    )

    document = validate_official_document(payload, source_url=source_url)
    _write_canonical_json(
        path,
        {
            "schema": "stage9_candidate_evidence_get_receipt/v2",
            "role": "candidate_evidence_http_get_receipt",
            "network_authorization_sha256": authorization_sha256,
            "request": {
                "purpose": request.purpose,
                "method": "GET",
                "source_url": source_url,
                "request_sha256": request.request_sha256,
                "attempt_count": attempt_count,
                "http_status": 200,
                "final_url": source_url if final_url is None else final_url,
                "redirect_followed_count": 0,
            },
            "document": {
                "sha256": document.sha256,
                "size_bytes": document.size_bytes,
                "page_count": document.page_count,
                "media_type": document.media_type,
            },
            "final_test_strategy_outputs_read": False,
        },
    )


def test_exchange_monthly_statistics_unique_row_closes_admission(
    prepared_attempt: PreparedAttempt,
    tmp_path: Path,
) -> None:
    from ashare_multifactor.final_test.official_candidate_review_admission import (
        require_candidate_review_admission,
    )

    workspace, queue, candidates = _admission_inputs(
        prepared_attempt,
        titles={
            "000001": "2023年度权益分派实施公告",
            "600000": "2024年第三季度报告",
        },
        announcement_dates={
            "000001": date(2024, 5, 17),
            "600000": date(2024, 6, 18),
        },
        document_payloads={
            "000001": _unicode_pdf_bytes(
                [
                    "证券代码：000001",
                    "2023年度权益分派实施公告",
                    "每10股派发现金红利1元（含税），不送红股，不转增股本。",
                    "三、股权登记日与除权除息日",
                    "股权登记日：2024年5月23日；除权除息日：2023年5月24日。",
                    "四、权益分派方法",
                ]
            ),
            "600000": _pdf_bytes("2024-07-01"),
        },
        candidate_symbols={"000001"},
        candidate_ex_dates={"000001": date(2024, 5, 24)},
    )
    candidate = candidates.candidates.row(0, named=True)
    source_url = (
        "https://docs.static.szse.cn/www/market/periodical/month/"
        "W020240607344713255806.html"
    )
    html = _statistics_html(
        (
            (
                "000001",
                "平安银行",
                "0",
                "0.000",
                "1000000.00",
                "0.100",
                "0",
                "0.000",
                "0.000",
                "",
                "2024/05/24",
                "2024/05/23",
                "10.20",
                "10.30",
            ),
        )
    )
    from ashare_multifactor.final_test.official_candidate_review_admission import (
        CandidateReviewAdmissionError,
    )

    with pytest.raises(CandidateReviewAdmissionError) as blocked:
        require_candidate_review_admission(
            workspace=workspace,
            queue=queue,
            candidates=candidates,
            date_rule_path=_DATE_RULE_PATH,
        )
    topology = publish_candidate_query_topology(
        destination=workspace.root.parent,
        candidate_manifest_path=candidates.manifest_path,
        blocked_admission_path=blocked.value.manifest_path,
        contract=load_action_source_contract(_CONTRACT_PATH),
    )
    cache_root = workspace.root.parent / STATISTICS_CACHE_DIRECTORY
    cache_root.mkdir()
    authorization_payload = build_exchange_monthly_statistics_authorization(
        code_root=prepared_attempt.code_root,
        workspace=workspace,
        queue=queue,
        blocked_admission_path=blocked.value.manifest_path,
        topology_manifest_path=topology.manifest_path,
        candidate_id=str(candidate["candidate_id"]),
        source_url=source_url,
        cache_root=cache_root,
    )
    authorization_path = tmp_path / "statistics_authorization.json"
    _write_canonical_json(authorization_path, authorization_payload)
    authorization = load_exchange_monthly_statistics_authorization(
        authorization_path,
        workspace=workspace,
        queue=queue,
    )
    request = authorization.request
    request.document_path.parent.mkdir(parents=True)
    request.document_path.write_bytes(html)
    _write_canonical_json(
        request.receipt_path,
        {
            "schema": "stage9_exchange_monthly_statistics_get_receipt/v1",
            "role": "exchange_monthly_statistics_http_get_receipt",
            "network_authorization_sha256": authorization.sha256,
            "request": {
                "purpose": "exchange_monthly_statistics",
                "method": "GET",
                "source_url": source_url,
                "request_sha256": request.request_sha256,
                "attempt_count": 1,
                "http_status": 200,
                "final_url": source_url,
                "redirect_followed_count": 0,
            },
            "document": {
                "sha256": hashlib.sha256(html).hexdigest(),
                "size_bytes": len(html),
                "media_type": "text/html; charset=GBK",
            },
            "final_test_strategy_outputs_read": False,
        },
    )
    statistics = publish_exchange_monthly_statistics(
        workspace=workspace,
        queue=queue,
        network_authorization_path=authorization_path,
        statistics=ExchangeMonthlyStatisticsInput(
            source_url=source_url,
            document_path=request.document_path,
            receipt_path=request.receipt_path,
        ),
    )
    bound_workspace = bind_exchange_monthly_statistics(
        workspace=workspace,
        queue=queue,
        statistics=statistics,
    )

    admission = require_candidate_review_admission(
        workspace=bound_workspace,
        queue=load_verified_review_queue(bound_workspace),
        candidates=load_verified_candidate_snapshot(
            candidates.manifest_path,
            workspace=bound_workspace,
        ),
        date_rule_path=_DATE_RULE_PATH,
    )

    assert admission.ready is True
    assert admission.decisions_path is not None
    decision = pl.read_parquet(admission.decisions_path).row(0, named=True)
    assert decision["evidence_kind"] == "exchange_monthly_statistics"
    statistics_binding = admission.manifest["exchange_monthly_statistics"]
    assert statistics_binding["post_event"] is True
    assert statistics_binding["source_period_end"] == "2024-05-31"
    assert statistics_binding["asset_path_date"] == "2024-06-07"
    assert statistics_binding["canonical_announcement"]["catalog_id"] == (
        decision["catalog_id"]
    )
    assert statistics_binding["canonical_announcement"]["document_sha256"]
    assert decision["source_url"] == source_url
    assert decision["announcement_publication_date"] == date(2024, 5, 17)
    assert decision["date_match"] == "exact_symbol_row_in_exchange_statistics"


def test_exchange_monthly_statistics_parser_uses_official_14_column_layout() -> None:
    values = (
        "300917",
        "特发服务",
        "0",
        "0.000",
        "37,180,000",
        "0.220",
        "0",
        "0.000",
        "0.000",
        "0",
        "2024/05/24",
        "2024/05/23",
        "37.20",
        "37.42",
    )
    payload = _statistics_html((values,))

    row = parse_unique_statistics_row(payload, symbol="300917")

    assert row.symbol == "300917"
    assert row.ex_date == date(2024, 5, 24)
    assert row.record_date == date(2024, 5, 23)
    assert str(row.cash_per_share) == "0.220"
    assert str(row.share_ratio) == "0.000"
    assert row.source_period_end == date(2024, 5, 31)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"\xff", "not GBK"),
        (
            _statistics_html(
                (("600000", "浦发银行", *("0",) * 12),)
            ),
            "target row is not unique",
        ),
        (
            _statistics_html(
                (
                    (
                        "300917", "特发服务", "0", "0.000", "1", "0.220",
                        "0", "0", "0", "0", "2024/05/24", "2024/05/23",
                        "1", "1",
                    ),
                    (
                        "300917", "特发服务", "0", "0.000", "1", "0.220",
                        "0", "0", "0", "0", "2024/05/24", "2024/05/23",
                        "1", "1",
                    ),
                )
            ),
            "target row is not unique",
        ),
        (
            _statistics_html(
                (("300917", "特发服务", *("0",) * 11),),
                headers=_STATISTICS_HEADERS[:-1],
            ),
            "table schema differs",
        ),
        (
            _statistics_html(
                (
                    (
                        "300917", "特发服务", "0", "0", "1", "0.220",
                        "0", "0", "0", "0", "2024/05/24", "2024/05/23",
                        "1", "1",
                    ),
                ),
                caption="MONTHLY MARKET STATISTICS （2024.05）",
            ),
            "table title differs",
        ),
        (
            "<style></style><table><caption>DIVIDEND,BONUS AND RIGHTS "
            "ISSUES - （2024.05）</caption>".encode("gbk"),
            "table title differs",
        ),
    ],
)
def test_exchange_monthly_statistics_parser_fails_closed(
    payload: bytes,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_unique_statistics_row(payload, symbol="300917")


def test_exchange_monthly_statistics_parser_does_not_borrow_adjacent_row() -> None:
    target = (
        "300917", "特发服务", "0", "0.000", "1", "0.220", "0", "0", "0",
        "0", "2023/05/24", "2023/05/23", "1", "1",
    )
    adjacent = (
        "300918", "南山智尚", "0", "0.000", "1", "0.220", "0", "0", "0",
        "0", "2024/05/24", "2024/05/23", "1", "1",
    )

    row = parse_unique_statistics_row(
        _statistics_html((target, adjacent)),
        symbol="300917",
    )

    assert row.ex_date == date(2023, 5, 24)
    assert row.record_date == date(2023, 5, 23)


def test_exchange_monthly_statistics_authorization_rejects_asset_date_window(
    prepared_attempt: PreparedAttempt,
) -> None:
    workspace, queue, candidates, blocked, topology, cache_root = (
        _exchange_statistics_authorization_inputs(prepared_attempt)
    )

    with pytest.raises(ValueError, match="not post-event"):
        build_exchange_monthly_statistics_authorization(
            code_root=prepared_attempt.code_root,
            workspace=workspace,
            queue=queue,
            blocked_admission_path=blocked.manifest_path,
            topology_manifest_path=topology.manifest_path,
            candidate_id=str(candidates.candidates.item(0, "candidate_id")),
            source_url=(
                "https://docs.static.szse.cn/www/market/periodical/month/"
                "W020240801344713255806.html"
            ),
            cache_root=cache_root,
        )


def test_exchange_monthly_statistics_authorization_rejects_non_policy_url(
    prepared_attempt: PreparedAttempt,
) -> None:
    workspace, queue, candidates, blocked, topology, cache_root = (
        _exchange_statistics_authorization_inputs(prepared_attempt)
    )

    with pytest.raises(ValueError, match="source URL"):
        build_exchange_monthly_statistics_authorization(
            code_root=prepared_attempt.code_root,
            workspace=workspace,
            queue=queue,
            blocked_admission_path=blocked.manifest_path,
            topology_manifest_path=topology.manifest_path,
            candidate_id=str(candidates.candidates.item(0, "candidate_id")),
            source_url=(
                "https://docs.static.szse.cn/www/market/periodical/year/"
                "W020240607344713255806.html"
            ),
            cache_root=cache_root,
        )


def test_exchange_monthly_statistics_uses_bounded_canonical_date_section(
    prepared_attempt: PreparedAttempt,
) -> None:
    lines = [
        "证券代码：000001",
        "2023年度权益分派实施公告",
        "每10股派发现金红利1元（含税），不送红股，不转增股本。",
        *[f"前置说明{index}{'内容' * 30}" for index in range(17)],
        "三、股权登记日与除权除息日",
        "本次权益分派股权登记日为：2024年5月23日，除权除息日为：2023年5月24日。",
        "四、权益分派方法",
    ]

    _, _, _, statistics = _publish_exchange_statistics_fixture(
        prepared_attempt,
        html=_statistics_html((_VALID_STATISTICS_ROW,)),
        canonical_lines=lines,
    )

    canonical = statistics.record["canonical_announcement"]
    assert canonical["record_date"] == "2024-05-23"


@pytest.mark.parametrize(
    "section_lines",
    [
        (
            "股权登记日为：2024年5月23日。",
            "四、权益分派方法",
        ),
        (
            "三、股权登记日与除权除息日",
            "股权登记日为：2024年5月23日。",
            "三、股权登记日与除权除息日",
            "四、权益分派方法",
        ),
        (
            "三、股权登记日与除权除息日",
            "本节未列出登记日。",
            "四、权益分派方法",
        ),
        (
            "三、股权登记日与除权除息日",
            "股权登记日为：2024年5月23日。",
            "股权登记日为：2024年5月23日。",
            "四、权益分派方法",
        ),
    ],
)
def test_exchange_monthly_statistics_rejects_unbounded_canonical_date(
    prepared_attempt: PreparedAttempt,
    section_lines: tuple[str, ...],
) -> None:
    canonical_lines = [
        "证券代码：000001",
        "2023年度权益分派实施公告",
        "每10股派发现金红利1元（含税），不送红股，不转增股本。",
        *section_lines,
    ]

    with pytest.raises(ValueError, match="canonical announcement is not unique"):
        _publish_exchange_statistics_fixture(
            prepared_attempt,
            html=_statistics_html((_VALID_STATISTICS_ROW,)),
            canonical_lines=canonical_lines,
        )


@pytest.mark.parametrize(
    ("title", "payload"),
    [
        (
            "2023年度权益分派公告",
            _unicode_pdf_bytes(
                [
                    "证券代码：000001",
                    "关于2023年度权益分派的公告",
                    "三、股权登记日与除权除息日",
                    "股权登记日为：2024年5月23日。",
                    "四、其他事项",
                ]
            ),
        ),
        ("2023年度权益分派实施公告", b"<html>invalid PDF</html>"),
    ],
)
def test_exchange_monthly_statistics_authorization_rejects_noncanonical_pdf(
    prepared_attempt: PreparedAttempt,
    title: str,
    payload: bytes | None,
) -> None:
    workspace, queue, candidates, blocked, topology, cache_root = (
        _exchange_statistics_authorization_inputs(
            prepared_attempt,
            canonical_title=title,
            canonical_payload=payload,
        )
    )

    with pytest.raises(ValueError, match="topology differs"):
        build_exchange_monthly_statistics_authorization(
            code_root=prepared_attempt.code_root,
            workspace=workspace,
            queue=queue,
            blocked_admission_path=blocked.manifest_path,
            topology_manifest_path=topology.manifest_path,
            candidate_id=str(candidates.candidates.item(0, "candidate_id")),
            source_url=(
                "https://docs.static.szse.cn/www/market/periodical/month/"
                "W020240607344713255806.html"
            ),
            cache_root=cache_root,
        )


def test_exchange_monthly_statistics_keeps_pdf_semantic_route_upgrade(
    prepared_attempt: PreparedAttempt,
) -> None:
    workspace, queue, candidates, blocked, topology, cache_root = (
        _exchange_statistics_authorization_inputs(
            prepared_attempt,
            canonical_title="2023年度权益分派公告",
        )
    )

    authorization = build_exchange_monthly_statistics_authorization(
        code_root=prepared_attempt.code_root,
        workspace=workspace,
        queue=queue,
        blocked_admission_path=blocked.manifest_path,
        topology_manifest_path=topology.manifest_path,
        candidate_id=str(candidates.candidates.item(0, "candidate_id")),
        source_url=(
            "https://docs.static.szse.cn/www/market/periodical/month/"
            "W020240607344713255806.html"
        ),
        cache_root=cache_root,
    )

    assert authorization["candidate"]["candidate_id"] == (
        candidates.candidates.item(0, "candidate_id")
    )


def test_exchange_monthly_statistics_rejects_post_candidate_canonical_pdf(
    prepared_attempt: PreparedAttempt,
) -> None:
    with pytest.raises(ValueError, match="canonical announcement is not unique"):
        _publish_exchange_statistics_fixture(
            prepared_attempt,
            html=_statistics_html((_VALID_STATISTICS_ROW,)),
            canonical_date=date(2024, 5, 25),
        )


@pytest.mark.parametrize(
    ("index", "value", "message"),
    [
        (0, "300917", "target row is not unique"),
        (10, "2024/05/25", "row differs from candidate"),
        (11, "2024/05/22", "canonical announcement is not unique"),
        (5, "0.200", "row differs from candidate"),
        (3, "0.100", "row differs from candidate"),
    ],
)
def test_exchange_monthly_statistics_rejects_field_mismatch(
    prepared_attempt: PreparedAttempt,
    index: int,
    value: str,
    message: str,
) -> None:
    row = list(_VALID_STATISTICS_ROW)
    row[index] = value

    with pytest.raises(ValueError, match=message):
        _publish_exchange_statistics_fixture(
            prepared_attempt,
            html=_statistics_html((tuple(row),)),
        )


def test_exchange_monthly_statistics_rejects_report_month_mismatch(
    prepared_attempt: PreparedAttempt,
) -> None:
    html = _statistics_html(
        (_VALID_STATISTICS_ROW,),
        caption="DIVIDEND,BONUS AND RIGHTS ISSUES - （2024.04）",
    )

    with pytest.raises(ValueError, match="row differs from candidate"):
        _publish_exchange_statistics_fixture(prepared_attempt, html=html)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"media_type": "text/html; charset=UTF-8"},
            "receipt differs",
        ),
        (
            {
                "final_url": (
                    "https://docs.static.szse.cn/www/market/periodical/month/"
                    "redirected.html"
                )
            },
            "receipt differs",
        ),
        ({"document_sha256": "0" * 64}, "receipt differs"),
    ],
)
def test_exchange_monthly_statistics_rejects_receipt_drift(
    prepared_attempt: PreparedAttempt,
    overrides: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _publish_exchange_statistics_fixture(
            prepared_attempt,
            html=_statistics_html((_VALID_STATISTICS_ROW,)),
            **overrides,
        )


def test_exchange_monthly_statistics_rejects_canonical_pdf_symlink(
    prepared_attempt: PreparedAttempt,
) -> None:
    workspace, queue, _, statistics = _publish_exchange_statistics_fixture(
        prepared_attempt,
        html=_statistics_html((_VALID_STATISTICS_ROW,)),
    )
    authorization = load_exchange_monthly_statistics_authorization(
        Path(str(statistics.manifest["network_authorization"]["path"])),
        workspace=workspace,
        queue=queue,
    )
    html_bytes = (statistics.root / "statistics.html").read_bytes()
    receipt_bytes = (statistics.root / "statistics.receipt.json").read_bytes()
    relative = str(
        queue.frame.filter(queue.frame["symbol"] == "000001").item(
            0,
            "document_cache_path",
        )
    )
    document_path = workspace.root / relative
    backup_path = document_path.with_name("document.backup")
    document_path.rename(backup_path)
    document_path.symlink_to(backup_path)

    with pytest.raises(ValueError, match="symlink|canonical path is invalid"):
        build_verified_statistics_record(
            workspace=workspace,
            queue=queue,
            authorization_payload=authorization.payload,
            authorization_sha256=authorization.sha256,
            request=authorization.request,
            html_bytes=html_bytes,
            receipt_bytes=receipt_bytes,
        )


def test_exchange_monthly_statistics_rejects_cross_review_session_binding(
    prepared_attempt: PreparedAttempt,
) -> None:
    workspace, queue, _, statistics = _publish_exchange_statistics_fixture(
        prepared_attempt,
        html=_statistics_html((_VALID_STATISTICS_ROW,)),
    )
    bound_workspace = bind_exchange_monthly_statistics(
        workspace=workspace,
        queue=queue,
        statistics=statistics,
    )
    bound_queue = load_verified_review_queue(bound_workspace)

    with pytest.raises(ValueError, match="binding inputs differ"):
        bind_exchange_monthly_statistics(
            workspace=bound_workspace,
            queue=bound_queue,
            statistics=statistics,
        )


def _rendition_authorization(
    prepared_attempt: PreparedAttempt,
    *,
    workspace,
    queue,
    candidates,
    tmp_path: Path,
    rendition_source_url: str,
    authority_source_url: str,
    candidate_id: str | None = None,
):
    from ashare_multifactor.final_test.official_candidate_review_admission import (
        CandidateReviewAdmissionError,
        require_candidate_review_admission,
    )

    with pytest.raises(CandidateReviewAdmissionError) as blocked:
        require_candidate_review_admission(
            workspace=workspace,
            queue=queue,
            candidates=candidates,
            date_rule_path=_DATE_RULE_PATH,
        )
    topology = publish_candidate_query_topology(
        destination=workspace.root.parent,
        candidate_manifest_path=candidates.manifest_path,
        blocked_admission_path=blocked.value.manifest_path,
        contract=load_action_source_contract(_CONTRACT_PATH),
    )
    cache_root = workspace.root.parent / RENDITION_CACHE_DIRECTORY
    cache_root.mkdir()
    payload = build_candidate_review_rendition_authorization(
        code_root=prepared_attempt.code_root,
        workspace=workspace,
        queue=queue,
        blocked_admission_path=blocked.value.manifest_path,
        topology_manifest_path=topology.manifest_path,
        candidate_id=(
            candidate_id
            if candidate_id is not None
            else str(candidates.candidates.item(0, "candidate_id"))
        ),
        rendition_source_url=rendition_source_url,
        authority_source_url=authority_source_url,
        cache_root=cache_root,
    )
    path = tmp_path / "rendition_authorization.json"
    _write_canonical_json(path, payload)
    verified = load_candidate_review_rendition_authorization(
        path,
        workspace=workspace,
        queue=queue,
    )
    return path, verified


def _historical_admission_inputs(
    tmp_path: Path,
    *,
    lags: tuple[int, int] = (4, 13),
) -> dict[str, Path]:
    code_root = tmp_path / "code"
    cache_root = tmp_path / "pdf-cache"
    cache_root.mkdir(parents=True)
    new_ex_date = date(2021, 6, 1)
    existing_ex_date = date(2021, 7, 1)
    pairs = [
        {
            "pair_id": "pair-new",
            "historical_candidate_id": "candidate-new",
            "symbol": "000001",
            "market": "sz",
            "ex_date": new_ex_date,
            "announcement_date": new_ex_date - timedelta(days=lags[0]),
            "catalog_id": "catalog-new",
            "announcement_id": "1001",
            "announcement_title": "2020年度权益分派实施公告",
            "source_url": (
                "https://static.cninfo.com.cn/finalpage/"
                f"{(new_ex_date - timedelta(days=lags[0])).isoformat()}/1001.PDF"
            ),
            "pdf_name": "000001_1001.pdf",
            "text": "2021-06-01",
            "cached": False,
        },
        {
            "pair_id": "pair-existing",
            "historical_candidate_id": "candidate-existing",
            "symbol": "600000",
            "market": "sh",
            "ex_date": existing_ex_date,
            "announcement_date": (
                existing_ex_date - timedelta(days=lags[1])
            ),
            "catalog_id": "catalog-existing",
            "announcement_id": "1002",
            "announcement_title": "2020年度利润分派实施公告",
            "source_url": (
                "https://static.cninfo.com.cn/finalpage/"
                f"{(existing_ex_date - timedelta(days=lags[1])).isoformat()}/1002.PDF"
            ),
            "pdf_name": "600000_1002.pdf",
            "text": "2021/7/1",
            "cached": True,
        },
    ]
    pdf_records: dict[str, dict[str, object]] = {}
    for pair in pairs:
        payload = _pdf_bytes(str(pair["text"]))
        path = cache_root / str(pair["pdf_name"])
        path.write_bytes(payload)
        pdf_records[str(pair["pair_id"])] = {
            "path": path,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
            "page_count": 1,
        }

    receipt_path = cache_root / "000001_1001.session00b.receipt.json"
    receipt_payload = {
        "schema": "stage9_historical_pdf_receipt/v1",
        "role": "immutable_session00b_historical_official_pdf_receipt",
        "catalog_id": "catalog-new",
        "symbol": "000001",
        "source_url": pairs[0]["source_url"],
        "pdf_relative_path": "cache/000001_1001.pdf",
        "pdf_sha256": pdf_records["pair-new"]["sha256"],
        "pdf_size_bytes": pdf_records["pair-new"]["size_bytes"],
        "pdf_page_count": 1,
        "media_type": "application/pdf",
        "final_test_strategy_outputs_read": False,
    }
    _write_canonical_json(receipt_path, receipt_payload)
    receipt_index = {
        "schema": "stage9_historical_pdf_receipt_index/v1",
        "role": "session00b_historical_pdf_receipt_index",
        "receipt_count": 1,
        "receipts": [
            {
                "catalog_id": "catalog-new",
                "announcement_id": "1001",
                "symbol": "000001",
                "market": "sz",
                "announcement_date": pairs[0][
                    "announcement_date"
                ].isoformat(),
                "source_url": pairs[0]["source_url"],
                "pdf_path": "cache/000001_1001.pdf",
                "pdf_sha256": pdf_records["pair-new"]["sha256"],
                "pdf_size_bytes": pdf_records["pair-new"]["size_bytes"],
                "receipt_path": "cache/000001_1001.session00b.receipt.json",
                "receipt_sha256": hashlib.sha256(
                    receipt_path.read_bytes()
                ).hexdigest(),
            }
        ],
    }
    receipt_index_path = tmp_path / "receipt-index.json"
    _write_canonical_json(receipt_index_path, receipt_index)
    existing_inventory = {
        "schema": "stage9_existing_historical_pdf_inventory/v1",
        "role": "offline_existing_cache_inventory",
        "file_count": 1,
        "valid_pdf_count": 1,
        "files": [
            {
                "catalog_id": "catalog-existing",
                "announcement_id": "1002",
                "symbol": "600000",
                "market": "sh",
                "announcement_date": pairs[1][
                    "announcement_date"
                ].isoformat(),
                "source_url": pairs[1]["source_url"],
                "path": "cache/600000_1002.pdf",
                "sha256": pdf_records["pair-existing"]["sha256"],
                "size_bytes": pdf_records["pair-existing"]["size_bytes"],
                "page_count": 1,
            }
        ],
    }
    existing_inventory_path = tmp_path / "existing-inventory.json"
    _write_canonical_json(existing_inventory_path, existing_inventory)
    discovery = pl.DataFrame(
        {
            "catalog_id": [str(pair["catalog_id"]) for pair in pairs],
            "announcement_id": [
                str(pair["announcement_id"]) for pair in pairs
            ],
            "symbol": [str(pair["symbol"]) for pair in pairs],
            "market": [str(pair["market"]) for pair in pairs],
            "announcement_title": [
                str(pair["announcement_title"]) for pair in pairs
            ],
            "announcement_date": [
                pair["announcement_date"].isoformat() for pair in pairs
            ],
            "source_url": [str(pair["source_url"]) for pair in pairs],
            "already_cached": [bool(pair["cached"]) for pair in pairs],
        }
    )
    discovery_path = tmp_path / "discovery.parquet"
    discovery.write_parquet(discovery_path)
    derivation = pl.DataFrame(
        {
            "pair_id": [str(pair["pair_id"]) for pair in pairs],
            "historical_candidate_id": [
                str(pair["historical_candidate_id"]) for pair in pairs
            ],
            "symbol": [str(pair["symbol"]) for pair in pairs],
            "market": [str(pair["market"]) for pair in pairs],
            "ex_date": [pair["ex_date"] for pair in pairs],
            "announcement_date": [
                pair["announcement_date"] for pair in pairs
            ],
            "lag_calendar_days": [
                (pair["ex_date"] - pair["announcement_date"]).days
                for pair in pairs
            ],
            "candidate_source_row_count": [1, 1],
            "catalog_id": [str(pair["catalog_id"]) for pair in pairs],
            "announcement_id": [
                str(pair["announcement_id"]) for pair in pairs
            ],
            "announcement_title": [
                str(pair["announcement_title"]) for pair in pairs
            ],
            "source_url": [str(pair["source_url"]) for pair in pairs],
            "pdf_path": [
                f"cache/{pair['pdf_name']}" for pair in pairs
            ],
            "pdf_sha256": [
                pdf_records[str(pair["pair_id"])]["sha256"] for pair in pairs
            ],
            "pdf_size_bytes": [
                pdf_records[str(pair["pair_id"])]["size_bytes"]
                for pair in pairs
            ],
            "pdf_page_count": [1, 1],
            "title_positive_literal": ["权益分派", "利润分派"],
            "date_match_label": ["除权除息日", "除权除息日"],
            "date_match_token": ["2021-06-01", "2021年7月1日"],
            "date_match_distance_characters": [0, 0],
        }
    )
    derivation_path = tmp_path / "derivation.parquet"
    derivation.write_parquet(derivation_path)
    derivation_manifest = {
        "schema": "stage9_candidate_review_admission_date_rule_derivation/v1",
        "role": "candidate_review_admission_date_rule_derivation",
        "period": ["2017-01-01", "2021-12-31"],
        "final_test_candidate_gaps_read": False,
        "final_test_strategy_outputs_read": False,
        "derivation": {
            "path": "derivation.parquet",
            "sha256": hashlib.sha256(
                derivation_path.read_bytes()
            ).hexdigest(),
            "size_bytes": derivation_path.stat().st_size,
            "pair_count": derivation.height,
        },
    }
    derivation_manifest_path = tmp_path / "derivation-manifest.json"
    _write_canonical_json(derivation_manifest_path, derivation_manifest)
    date_rule = {
        "schema": "stage9_candidate_review_admission_date_rule/v2",
        "role": "candidate_review_admission_date_rule",
        "period": ["2017-01-01", "2021-12-31"],
        "final_test_candidate_gaps_read": False,
        "final_test_strategy_outputs_read": False,
        "derivation": {
            "manifest_path": "derivation-manifest.json",
            "manifest_sha256": hashlib.sha256(
                derivation_manifest_path.read_bytes()
            ).hexdigest(),
            "parquet_path": "derivation.parquet",
            "parquet_sha256": hashlib.sha256(
                derivation_path.read_bytes()
            ).hexdigest(),
        },
        "historical_input_sha256": {
            "historical_pdf_discovery_parquet": hashlib.sha256(
                discovery_path.read_bytes()
            ).hexdigest(),
            "historical_pdf_receipt_index": hashlib.sha256(
                receipt_index_path.read_bytes()
            ).hexdigest(),
            "historical_pdf_existing_inventory": hashlib.sha256(
                existing_inventory_path.read_bytes()
            ).hexdigest(),
        },
        "lag": {
            "definition": (
                "ex_date_minus_announcement_date_in_calendar_days"
            ),
            "interval_closed": True,
            "minimum_calendar_days": 4,
            "maximum_calendar_days": 13,
        },
        "temporal_policy": {
            "predicate": "publication_date_lte_candidate_ex_date",
            "publication_date_source": "cninfo_finalpage_path_date",
            "same_day_allowed": True,
            "historical_lag_interval_usage": "diagnostic_only",
            "final_test_gap_distribution_used_to_select_predicate": False,
        },
    }
    date_rule_path = (
        code_root
        / "configs/evidence/stage9_candidate_review_admission_date_rule.json"
    )
    _write_canonical_json(date_rule_path, date_rule)
    return {
        "code_root": code_root,
        "derivation_path": derivation_path,
        "derivation_manifest_path": derivation_manifest_path,
        "discovery_path": discovery_path,
        "receipt_index_path": receipt_index_path,
        "existing_inventory_path": existing_inventory_path,
        "date_rule_path": date_rule_path,
        "pdf_cache_root": cache_root,
        "destination": tmp_path / "output",
    }


def test_historical_candidate_admission_recomputes_frozen_pairs(
    tmp_path: Path,
) -> None:
    from ashare_multifactor.final_test.official_candidate_review_admission import (
        require_historical_candidate_review_admission,
    )

    inputs = _historical_admission_inputs(tmp_path)
    inputs["destination"].mkdir()

    admission = require_historical_candidate_review_admission(**inputs)

    assert admission.ready is True
    assert admission.manifest["candidate_count"] == 2
    assert admission.manifest["admitted_count"] == 2
    assert admission.manifest["unresolved_count"] == 0
    assert admission.decisions_path is not None
    decisions = pl.read_parquet(admission.decisions_path)
    assert decisions.get_column("pair_id").to_list() == [
        "pair-existing",
        "pair-new",
    ]
    assert decisions.get_column("status").to_list() == [
        "admitted",
        "admitted",
    ]
    assert {
        record["role"] for record in admission.manifest["inputs"]
    } == {
        "historical_candidate_derivation",
        "historical_candidate_derivation_manifest",
        "historical_pdf_discovery",
        "historical_pdf_receipt_index",
        "historical_pdf_existing_inventory",
        "historical_pdf_cache",
        "candidate_review_admission_date_rule",
    }


def test_historical_candidate_admission_treats_lag_window_as_diagnostic(
    tmp_path: Path,
) -> None:
    from ashare_multifactor.final_test.official_candidate_review_admission import (
        require_historical_candidate_review_admission,
    )

    inputs = _historical_admission_inputs(tmp_path, lags=(14, 13))
    inputs["destination"].mkdir()

    admission = require_historical_candidate_review_admission(**inputs)

    assert admission.ready is True
    assert admission.decisions_path is not None
    decision = (
        pl.read_parquet(admission.decisions_path)
        .filter(pl.col("pair_id") == "pair-new")
        .row(0, named=True)
    )
    assert decision["status"] == "admitted"
    assert decision["historical_lag_interval_hit"] is False


@pytest.mark.parametrize("mutation", ["discovery_bytes", "missing_pdf"])
def test_historical_candidate_admission_rejects_frozen_input_tamper(
    tmp_path: Path,
    mutation: str,
) -> None:
    from ashare_multifactor.final_test.official_candidate_review_admission import (
        require_historical_candidate_review_admission,
    )

    inputs = _historical_admission_inputs(tmp_path)
    inputs["destination"].mkdir()
    if mutation == "discovery_bytes":
        inputs["discovery_path"].write_bytes(
            inputs["discovery_path"].read_bytes() + b"drift"
        )
    else:
        (inputs["pdf_cache_root"] / "000001_1001.pdf").unlink()

    with pytest.raises(ValueError):
        require_historical_candidate_review_admission(**inputs)


def test_historical_candidate_admission_publishes_nonzero_unresolved_on_route_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ashare_multifactor.final_test import (
        official_announcement_routing as routing_module,
        official_candidate_review_admission as admission_module,
    )

    inputs = _historical_admission_inputs(tmp_path)
    inputs["destination"].mkdir()
    monkeypatch.setattr(
        routing_module,
        "route_announcement_title",
        lambda _title: {
            "route": "excluded",
            "candidate_type": "",
            "reason": "simulated_rule_drift",
        },
    )

    with pytest.raises(
        admission_module.CandidateReviewAdmissionError
    ) as blocked_error:
        admission_module.require_historical_candidate_review_admission(
            **inputs
        )

    manifest = json.loads(blocked_error.value.manifest_path.read_bytes())
    assert manifest["status"] == "blocked"
    assert manifest["unresolved_count"] == 2
    unresolved = pl.read_parquet(
        blocked_error.value.manifest_path.parent
        / "unresolved_candidates.parquet"
    )
    assert unresolved.height == 2
    assert all(
        "no_strong_implementation_route" in codes.to_list()
        for codes in unresolved.get_column("failure_codes")
    )


def test_candidate_review_admission_accepts_exact_date_at_historical_diagnostic_boundaries(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_candidate_review_admission import (
        require_candidate_review_admission,
    )

    workspace, queue, candidates = _admission_inputs(
        prepared_attempt,
        titles={
            "000001": "2024年度权益分派实施公告",
            "600000": "2024年度权益分派实施公告",
        },
        announcement_dates={
            "000001": date(2024, 5, 28),
            "600000": date(2024, 6, 18),
        },
        document_payloads={
            "000001": _pdf_bytes("2024-06-01"),
            "600000": _pdf_bytes("2024-07-01"),
        },
        candidate_symbols={"000001", "600000"},
    )

    admission = require_candidate_review_admission(
        workspace=workspace,
        queue=queue,
        candidates=candidates,
        date_rule_path=_DATE_RULE_PATH,
    )

    assert admission.ready is True
    manifest = json.loads(admission.manifest_path.read_bytes())
    assert manifest["status"] == "ready"
    assert manifest["candidate_count"] == 2
    assert manifest["admitted_count"] == 2
    assert manifest["unresolved_count"] == 0
    assert manifest["date_rule"]["temporal_policy"] == {
        "predicate": "publication_date_lte_candidate_ex_date",
        "publication_date_source": "cninfo_finalpage_path_date",
        "same_day_allowed": True,
        "historical_lag_interval_usage": "diagnostic_only",
        "final_test_gap_distribution_used_to_select_predicate": False,
    }
    assert pl.read_parquet(admission.unresolved_path).is_empty()
    manifest_bytes = admission.manifest_path.read_bytes()
    unresolved_bytes = admission.unresolved_path.read_bytes()

    replay = require_candidate_review_admission(
        workspace=workspace,
        queue=queue,
        candidates=candidates,
        date_rule_path=_DATE_RULE_PATH,
    )

    assert replay.manifest_sha256 == admission.manifest_sha256
    assert replay.manifest_path.read_bytes() == manifest_bytes
    assert replay.unresolved_path.read_bytes() == unresolved_bytes


@pytest.mark.parametrize(
    "announcement_date",
    [
        pytest.param(date(2024, 6, 1), id="lag-0"),
        pytest.param(date(2024, 5, 18), id="lag-14"),
        pytest.param(date(2024, 5, 14), id="lag-18"),
    ],
)
def test_candidate_review_admission_accepts_causally_valid_exact_date(
    prepared_attempt: PreparedAttempt,
    announcement_date: date,
) -> None:
    from ashare_multifactor.final_test.official_candidate_review_admission import (
        require_candidate_review_admission,
    )

    workspace, queue, candidates = _admission_inputs(
        prepared_attempt,
        titles={
            "000001": "2024年度权益分派实施公告",
            "600000": "2024年第三季度报告",
        },
        announcement_dates={
            "000001": announcement_date,
            "600000": date(2024, 6, 18),
        },
        document_payloads={
            "000001": _pdf_bytes("2024-06-01"),
            "600000": _pdf_bytes("2024-07-01"),
        },
        candidate_symbols={"000001"},
    )

    admission = require_candidate_review_admission(
        workspace=workspace,
        queue=queue,
        candidates=candidates,
        date_rule_path=_DATE_RULE_PATH,
    )

    assert admission.ready is True
    assert admission.manifest["admitted_count"] == 1
    assert admission.manifest["unresolved_count"] == 0


@pytest.mark.parametrize(
    "leading_lines",
    [
        pytest.param(
            [
                "董事会公告",
                "1",
                "证券代码：000001",
                "证券简称：测试股份",
                "公告编号：2024-001",
                "测试股份有限公司2024年度权益分派",
                "实施公告",
                "除权除息日：2024-06-01",
            ],
            id="weak-generic-heading-before-strong-heading",
        ),
        pytest.param(
            [
                "证券代码：000001200001",
                "证券简称：测试A 测试B",
                "公告编号：2024-001",
                "2024年度权益分派实施公告",
                "除权除息日：2024-06-01",
            ],
            id="complete-six-digit-code-groups",
        ),
        pytest.param(
            [
                (
                    "证券代码：000001证券简称：测试股份"
                    "公告编号：2024-001测试股份有限公司"
                    "2024年度权益分派实施公告"
                ),
                "除权除息日：2024-06-01",
            ],
            id="boilerplate-fields-with-trailing-strong-heading",
        ),
        pytest.param(
            [
                "证券代码：000001",
                "本公司及董事会全体成员保证信息披露真实、准确、完整。",
                "2024年度权益分派实施公告",
                "除权除息日：2024-06-01",
            ],
            id="assurance-statement-before-strong-heading",
        ),
    ],
)
def test_candidate_review_admission_upgrades_a_strong_pdf_leading_heading(
    prepared_attempt: PreparedAttempt,
    leading_lines: list[str],
) -> None:
    from ashare_multifactor.final_test.official_candidate_review_admission import (
        require_candidate_review_admission,
    )

    workspace, queue, candidates = _admission_inputs(
        prepared_attempt,
        titles={
            "000001": "关于2024年度权益分派的公告",
            "600000": "2024年第三季度报告",
        },
        announcement_dates={
            "000001": date(2024, 5, 28),
            "600000": date(2024, 6, 18),
        },
        document_payloads={
            "000001": _unicode_pdf_bytes(leading_lines),
            "600000": _pdf_bytes("2024-07-01"),
        },
        candidate_symbols={"000001"},
    )

    admission = require_candidate_review_admission(
        workspace=workspace,
        queue=queue,
        candidates=candidates,
        date_rule_path=_DATE_RULE_PATH,
    )

    assert admission.ready is True
    assert admission.manifest["unresolved_count"] == 0


@pytest.mark.parametrize(
    "date_token",
    [
        pytest.param(
            f"2024{separator}{month}{separator}{day}",
            id=f"{separator}-{month}-{day}",
        )
        for separator in ("-", "/", ".")
        for month in ("6", "06")
        for day in ("1", "01")
    ]
    + [
        pytest.param("2024 - 6 - 1", id="whitespace-normalized"),
        pytest.param(
            "2024/5/31-2024/6/12024/6/1",
            id="adjacent-table-dates",
        ),
        pytest.param(
            "2024/5/312024/6/12024/6/2",
            id="date-cell-between-adjacent-table-dates",
        ),
        pytest.param(
            "2024年5月31日2024年6月1日2024年6月2日",
            id="date-cell-between-adjacent-chinese-table-dates",
        ),
        pytest.param(
            "2024/6/12024/6/22024/6/3",
            id="date-cell-first-in-three-numeric-table-dates",
        ),
        pytest.param(
            "2024/5/302024/5/312024/6/1",
            id="date-cell-last-in-three-numeric-table-dates",
        ),
        pytest.param(
            "2024年6月1日2024年6月2日2024年6月3日",
            id="date-cell-first-in-three-chinese-table-dates",
        ),
        pytest.param(
            "2024年5月30日2024年5月31日2024年6月1日",
            id="date-cell-last-in-three-chinese-table-dates",
        ),
        pytest.param(
            "2024/5/31-报告2024/6/1",
            id="unrelated-date-prefix-before-natural-text-boundary",
        ),
        pytest.param(
            "2024年度报告2024/6/1",
            id="yearly-report-before-natural-text-boundary",
        ),
        pytest.param(
            "2024/报告2024/6/1",
            id="slash-prefixed-narrative-before-natural-text-boundary",
        ),
        pytest.param(
            "2024/abc2024/6/1",
            id="ascii-narrative-before-natural-text-boundary",
        ),
        pytest.param(
            "2024年abc2024年6月1日",
            id="chinese-date-marker-narrative-before-natural-text-boundary",
        ),
    ],
)
def test_candidate_review_admission_accepts_exact_numeric_date_variants(
    prepared_attempt: PreparedAttempt,
    date_token: str,
) -> None:
    from ashare_multifactor.final_test.official_candidate_review_admission import (
        require_candidate_review_admission,
    )

    workspace, queue, candidates = _admission_inputs(
        prepared_attempt,
        titles={
            "000001": "2024年度权益分派实施公告",
            "600000": "2024年第三季度报告",
        },
        announcement_dates={
            "000001": date(2024, 5, 28),
            "600000": date(2024, 6, 18),
        },
        document_payloads={
            "000001": (
                _pdf_bytes(date_token)
                if date_token.isascii()
                else _unicode_pdf_bytes([date_token])
            ),
            "600000": _pdf_bytes("2024-07-01"),
        },
        candidate_symbols={"000001"},
    )

    admission = require_candidate_review_admission(
        workspace=workspace,
        queue=queue,
        candidates=candidates,
        date_rule_path=_DATE_RULE_PATH,
    )

    assert admission.ready is True


@pytest.mark.parametrize(
    "candidate_ex_date,date_token",
    [
        pytest.param(
            date(2025, 6, 3),
            "A股2025/5/30/2025/6/32025/6/3",
            id="sse-empty-last-trading-date-before-single-digit-day",
        ),
        pytest.param(
            date(2025, 9, 10),
            "A股2025/9/9/2025/9/102025/9/10",
            id="sse-empty-last-trading-date-before-double-digit-day",
        ),
        pytest.param(
            date(2025, 6, 3),
            "A股2025年05月30日/2025/6/32025/6/3",
            id="complete-chinese-date-before-slash-delimiter",
        ),
        pytest.param(
            date(2025, 6, 3),
            "A股2025年05月30日-2025/6/32025/6/3",
            id="complete-chinese-date-before-hyphen-delimiter",
        ),
    ],
)
def test_candidate_review_admission_accepts_sse_empty_last_trading_date_cell(
    prepared_attempt: PreparedAttempt,
    candidate_ex_date: date,
    date_token: str,
) -> None:
    from ashare_multifactor.final_test.official_candidate_review_admission import (
        require_candidate_review_admission,
    )

    workspace, queue, candidates = _admission_inputs(
        prepared_attempt,
        titles={
            "000001": "2025年度权益分派实施公告",
            "600000": "2025年第三季度报告",
        },
        announcement_dates={
            "000001": candidate_ex_date,
            "600000": date(2025, 10, 18),
        },
        document_payloads={
            "000001": _unicode_pdf_bytes([date_token]),
            "600000": _pdf_bytes("2025-10-20"),
        },
        candidate_symbols={"000001"},
        candidate_ex_dates={"000001": candidate_ex_date},
    )

    admission = require_candidate_review_admission(
        workspace=workspace,
        queue=queue,
        candidates=candidates,
        date_rule_path=_DATE_RULE_PATH,
    )

    assert admission.ready is True


@pytest.mark.parametrize(
    "date_token",
    [
        pytest.param(
            "12025年12月31日/2025/6/32025/6/3",
            id="five-digit-chinese-year-max-length-before-slash",
        ),
        pytest.param(
            "12025年06月03日/2025/6/32025/6/3",
            id="five-digit-chinese-target-date-before-slash",
        ),
        pytest.param(
            "12025年12月31日-2025/6/32025/6/3",
            id="five-digit-chinese-year-max-length-before-hyphen",
        ),
        pytest.param(
            "12025年06月03日-2025/6/32025/6/3",
            id="five-digit-chinese-target-date-before-hyphen",
        ),
    ],
)
def test_candidate_review_admission_rejects_five_digit_chinese_date_cell_chain(
    prepared_attempt: PreparedAttempt,
    date_token: str,
) -> None:
    from ashare_multifactor.final_test.official_candidate_review_admission import (
        CandidateReviewAdmissionError,
        require_candidate_review_admission,
    )

    workspace, queue, candidates = _admission_inputs(
        prepared_attempt,
        titles={
            "000001": "2025年度权益分派实施公告",
            "600000": "2025年第三季度报告",
        },
        announcement_dates={
            "000001": date(2025, 6, 3),
            "600000": date(2025, 10, 18),
        },
        document_payloads={
            "000001": _unicode_pdf_bytes([date_token]),
            "600000": _pdf_bytes("2025-10-20"),
        },
        candidate_symbols={"000001"},
        candidate_ex_dates={"000001": date(2025, 6, 3)},
    )

    with pytest.raises(CandidateReviewAdmissionError):
        require_candidate_review_admission(
            workspace=workspace,
            queue=queue,
            candidates=candidates,
            date_rule_path=_DATE_RULE_PATH,
        )


@pytest.mark.parametrize(
    "date_token",
    [
        "12024-6-1",
        "2024-6-10",
        "12024-6-11",
        "12024/5/312024/6/1",
        "9999/99/992024/6/1",
        "2023/2/292024/6/1",
        "9999年99月99日2024年6月1日",
        "2023年2月29日2024年6月1日",
        "2024/6/12024/abc",
        "2024/6/12024/999",
        "2024/6/12024年abc",
        "9999/99/992024/5/312024/6/1",
        "2024/6/12024/6/22024/abc",
        "9999年99月99日2024年5月31日2024年6月1日",
        "2024年6月1日2024年6月2日2024年abc",
        "2024/6/2024/6/1",
        "2024/6-2024/6/1",
        "2024-6/2024/6/1",
        "2024//2024/6/1",
        "2024年6月1年2024年6月1日",
        "12024/6/2024/6/1",
        "12024年6月1年2024年6月1日",
    ],
)
def test_candidate_review_admission_rejects_numeric_date_substrings(
    prepared_attempt: PreparedAttempt,
    date_token: str,
) -> None:
    from ashare_multifactor.final_test.official_candidate_review_admission import (
        CandidateReviewAdmissionError,
        require_candidate_review_admission,
    )

    workspace, queue, candidates = _admission_inputs(
        prepared_attempt,
        titles={
            "000001": "2024年度权益分派实施公告",
            "600000": "2024年第三季度报告",
        },
        announcement_dates={
            "000001": date(2024, 5, 28),
            "600000": date(2024, 6, 18),
        },
        document_payloads={
            "000001": (
                _pdf_bytes(date_token)
                if date_token.isascii()
                else _unicode_pdf_bytes([date_token])
            ),
            "600000": _pdf_bytes("2024-07-01"),
        },
        candidate_symbols={"000001"},
    )

    with pytest.raises(CandidateReviewAdmissionError):
        require_candidate_review_admission(
            workspace=workspace,
            queue=queue,
            candidates=candidates,
            date_rule_path=_DATE_RULE_PATH,
        )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("除权除息日：2024/6/12024/6/2", True),
        ("除权除息日：2024年6月1日2024年6月2日", True),
        ("除权除息日：2024/6/12024/6/22024/6/3", True),
        (
            "除权除息日：2024年6月1日2024年6月2日2024年6月3日",
            True,
        ),
        ("除权除息日：2024/6/12024/abc", False),
        ("除权除息日：2024年6月1日2024年abc", False),
        ("除权除息日：2024/6/12024/6/22024/abc", False),
        (
            "除权除息日：2024年6月1日2024年6月2日2024年abc",
            False,
        ),
    ],
)
def test_candidate_pdf_labelled_date_requires_a_complete_adjacent_date_cell(
    text: str,
    expected: bool,
) -> None:
    from ashare_multifactor.final_test.official_candidate_pdf_evidence import (
        inspect_candidate_pdf,
    )

    payload = _pdf_bytes(text) if text.isascii() else _unicode_pdf_bytes([text])
    evidence = inspect_candidate_pdf(payload)

    assert evidence.has_labelled_leading_date(date(2024, 6, 1)) is expected


@pytest.mark.parametrize(
    "leading_lines",
    [
        pytest.param(
            [
                "证券代码：000001",
                "2022年度分红派息公告",
                "自分配方案披露至实施期间，公司股本总额未发生变化。",
                "本次实施的分配方案与股东大会审议通过的方案一致。",
                "除权除息日：2024-06-01",
            ],
            id="explicit-current-implementation-preamble",
        ),
        pytest.param(
            [
                "证券代码：000001",
                "2024年半年度利润分配方案公告",
                "本公司及董事会全体成员保证信息披露真实。",
                "特别提示：",
                "1、公司本次利润分配方案以实",
                "施权益分派方案时股权登记日为基数。",
                "除权除息日：2024-06-01",
            ],
            id="numbered-special-notice-implementation-preamble",
        ),
        pytest.param(
            [
                "证券代码：000001",
                "2022年度分红派息公告",
                "本公司及董事会全体成员保证信息披露真实。",
                "一、股东大会审议通过利润分配方案情况",
                "1、方案已经股东大会审议通过。",
                "本次实施的分配方案与审议通过的方案一致。",
                "除权除息日：2024-06-01",
            ],
            id="implementation-preamble-in-first-formal-section",
        ),
    ],
)
def test_candidate_review_admission_upgrades_an_explicit_bounded_preamble(
    prepared_attempt: PreparedAttempt,
    leading_lines: list[str],
) -> None:
    from ashare_multifactor.final_test.official_candidate_review_admission import (
        require_candidate_review_admission,
    )

    workspace, queue, candidates = _admission_inputs(
        prepared_attempt,
        titles={
            "000001": "关于2024年度权益分派的公告",
            "600000": "2024年第三季度报告",
        },
        announcement_dates={
            "000001": date(2024, 5, 28),
            "600000": date(2024, 6, 18),
        },
        document_payloads={
            "000001": _unicode_pdf_bytes(leading_lines),
            "600000": _pdf_bytes("2024-07-01"),
        },
        candidate_symbols={"000001"},
    )

    admission = require_candidate_review_admission(
        workspace=workspace,
        queue=queue,
        candidates=candidates,
        date_rule_path=_DATE_RULE_PATH,
    )

    assert admission.ready is True


@pytest.mark.parametrize(
    "title,leading_lines",
    [
        pytest.param(
            "关于2024年度权益分派的公告",
            [
                "证券代码：000001",
                "关于2024年度权益分派的公告",
                "本次工作将实施严格的内部控制。",
                "除权除息日：2024-06-01",
            ],
            id="isolated-implementation-word",
        ),
        pytest.param(
            "关于2024年度权益分派的公告",
            [
                "证券代码：000001",
                "关于2024年度权益分派的公告",
                "若未来实施权益分派方案，公司将另行公告。",
                "除权除息日：2024-06-01",
            ],
            id="conditional-implementation-preamble",
        ),
        pytest.param(
            "关于2024年度权益分派的公告",
            [
                "证券代码：000001",
                "关于2024年度权益分派的公告",
                "公司拟于股东大会审议通过后实施权益分派方案。",
                "除权除息日：2024-06-01",
            ],
            id="future-implementation-preamble",
        ),
        pytest.param(
            "关于2024年度权益分派的公告",
            [
                "证券代码：000001",
                "关于2024年度权益分派的公告",
                *[f"首部说明{i}" for i in range(1, 19)],
                "本次实施的分配方案与审议通过的方案一致。",
                "除权除息日：2024-06-01",
            ],
            id="implementation-after-twenty-line-boundary",
        ),
        pytest.param(
            "吸收合并实施公告",
            [
                "证券代码：000001",
                "2024年度权益分派实施公告",
                "除权除息日：2024-06-01",
            ],
            id="other-candidate-type",
        ),
        pytest.param(
            "关于2024年度权益分派的公告",
            [
                "证券代码：1000001",
                "2024年度权益分派实施公告",
                "除权除息日：2024-06-01",
            ],
            id="arbitrary-security-digit-substring",
        ),
        pytest.param(
            "关于2024年度权益分派的公告",
            [
                (
                    "证券代码：000001证券简称：测试股份"
                    "公告编号：2024-001实施"
                ),
                "除权除息日：2024-06-01",
            ],
            id="boilerplate-with-isolated-implementation-tail",
        ),
    ],
)
def test_candidate_review_admission_does_not_overpromote_pdf_text(
    prepared_attempt: PreparedAttempt,
    title: str,
    leading_lines: list[str],
) -> None:
    from ashare_multifactor.final_test.official_candidate_review_admission import (
        CandidateReviewAdmissionError,
        require_candidate_review_admission,
    )

    workspace, queue, candidates = _admission_inputs(
        prepared_attempt,
        titles={"000001": title, "600000": "2024年第三季度报告"},
        announcement_dates={
            "000001": date(2024, 5, 28),
            "600000": date(2024, 6, 18),
        },
        document_payloads={
            "000001": _unicode_pdf_bytes(leading_lines),
            "600000": _pdf_bytes("2024-07-01"),
        },
        candidate_symbols={"000001"},
    )

    with pytest.raises(CandidateReviewAdmissionError):
        require_candidate_review_admission(
            workspace=workspace,
            queue=queue,
            candidates=candidates,
            date_rule_path=_DATE_RULE_PATH,
        )


def test_candidate_review_admission_rejects_post_event_announcement(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_candidate_review_admission import (
        CandidateReviewAdmissionError,
        require_candidate_review_admission,
    )

    workspace, queue, candidates = _admission_inputs(
        prepared_attempt,
        titles={
            "000001": "2024年度权益分派实施公告",
            "600000": "2024年第三季度报告",
        },
        announcement_dates={
            "000001": date(2024, 6, 2),
            "600000": date(2024, 6, 18),
        },
        document_payloads={
            "000001": _pdf_bytes("2024-06-01"),
            "600000": _pdf_bytes("2024-07-01"),
        },
        candidate_symbols={"000001"},
    )

    with pytest.raises(CandidateReviewAdmissionError) as blocked:
        require_candidate_review_admission(
            workspace=workspace,
            queue=queue,
            candidates=candidates,
            date_rule_path=_DATE_RULE_PATH,
        )

    unresolved = pl.read_parquet(
        blocked.value.manifest_path.with_name("unresolved_candidates.parquet")
    )
    assert unresolved.item(0, "failure_codes").to_list() == [
        "announcement_after_candidate_ex_date",
        "no_single_announcement_satisfies_all_requirements",
    ]


def test_candidate_review_admission_blocks_when_cached_pdf_bytes_drift(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_candidate_review_admission import (
        CandidateReviewAdmissionError,
        require_candidate_review_admission,
    )

    workspace, queue, candidates = _admission_inputs(
        prepared_attempt,
        titles={"000001": "2024年度权益分派实施公告", "600000": "2024年第三季度报告"},
        announcement_dates={
            "000001": date(2024, 5, 28),
            "600000": date(2024, 6, 18),
        },
        document_payloads={
            "000001": _pdf_bytes("2024-06-01"),
            "600000": _pdf_bytes("2024-07-01"),
        },
        candidate_symbols={"000001"},
    )
    cached = queue.frame.filter(pl.col("symbol") == "000001").row(0, named=True)
    (workspace.root / cached["document_cache_path"]).write_bytes(b"changed")

    with pytest.raises(CandidateReviewAdmissionError):
        require_candidate_review_admission(
            workspace=workspace,
            queue=queue,
            candidates=candidates,
            date_rule_path=_DATE_RULE_PATH,
        )

    unresolved = pl.read_parquet(
        next(workspace.root.parent.rglob("unresolved_candidates.parquet"))
    )
    assert unresolved.item(0, "failure_codes").to_list() == [
        "no_valid_cached_official_pdf",
        "no_single_announcement_satisfies_all_requirements",
    ]


def test_candidate_review_admission_keeps_blocked_identity_after_targeted_recovery(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_candidate_review_admission import (
        CandidateReviewAdmissionError,
        require_candidate_review_admission,
    )

    workspace, queue, candidates = _admission_inputs(
        prepared_attempt,
        titles={
            "000001": "2024年度权益分派实施公告",
            "600000": "2024年第三季度报告",
        },
        announcement_dates={
            "000001": date(2024, 5, 28),
            "600000": date(2024, 6, 18),
        },
        document_payloads={
            "000001": b"<html>temporary upstream failure</html>",
            "600000": _pdf_bytes("2024-07-01"),
        },
        candidate_symbols={"000001"},
    )
    with pytest.raises(CandidateReviewAdmissionError) as blocked_error:
        require_candidate_review_admission(
            workspace=workspace,
            queue=queue,
            candidates=candidates,
            date_rule_path=_DATE_RULE_PATH,
        )
    blocked_manifest = blocked_error.value.manifest_path
    blocked_bytes = blocked_manifest.read_bytes()

    recovered_workspace = fetch_official_documents(
        workspace.catalog_path,
        destination=workspace.root.parent,
        transport=_AdmissionDocumentTransport(
            {
                "000001": _pdf_bytes("2024-06-01"),
                "600000": _pdf_bytes("2024-07-01"),
            }
        ),
        retry_quarantined=True,
    )
    ready = require_candidate_review_admission(
        workspace=recovered_workspace,
        queue=load_verified_review_queue(recovered_workspace),
        candidates=candidates,
        date_rule_path=_DATE_RULE_PATH,
    )

    assert ready.ready is True
    assert ready.manifest_path != blocked_manifest
    assert blocked_manifest.read_bytes() == blocked_bytes
    assert json.loads(blocked_bytes)["status"] == "blocked"
    assert json.loads(ready.manifest_path.read_bytes())["status"] == "ready"


def test_candidate_review_admission_does_not_join_partial_requirements_across_announcements(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_candidate_review_admission import (
        CandidateReviewAdmissionError,
        require_candidate_review_admission,
    )

    titles = {
        "000001": "unused",
        "600000": "2024年第三季度报告",
    }
    dates = {
        "000001": date(2024, 5, 28),
        "600000": date(2024, 6, 18),
    }
    workspace, queue, candidates = _admission_inputs(
        prepared_attempt,
        titles=titles,
        announcement_dates=dates,
        document_payloads={
            "000001-prior": _pdf_bytes("implementation announcement"),
            "000001-post": _pdf_bytes("2024-06-01"),
            "600000": _pdf_bytes("2024-07-01"),
        },
        candidate_symbols={"000001"},
        announcement_transport=_SplitRequirementAnnouncementTransport(
            titles=titles,
            announcement_dates=dates,
        ),
    )

    with pytest.raises(CandidateReviewAdmissionError):
        require_candidate_review_admission(
            workspace=workspace,
            queue=queue,
            candidates=candidates,
            date_rule_path=_DATE_RULE_PATH,
        )

    unresolved = pl.read_parquet(
        next(workspace.root.parent.rglob("unresolved_candidates.parquet"))
    )
    assert unresolved.item(0, "failure_codes").to_list() == [
        "announcement_after_candidate_ex_date",
        "no_single_announcement_satisfies_all_requirements",
    ]
    assert unresolved.item(0, "same_symbol_announcement_count") == 2
    assert unresolved.item(0, "strong_implementation_announcement_count") == 2


def test_candidate_review_admission_reports_only_the_unresolved_candidate(
    prepared_attempt: PreparedAttempt,
) -> None:
    from ashare_multifactor.final_test.official_candidate_review_admission import (
        CandidateReviewAdmissionError,
        require_candidate_review_admission,
    )

    workspace, queue, candidates = _admission_inputs(
        prepared_attempt,
        titles={
            "000001": "2024年度权益分派实施公告",
            "600000": "2024年度权益分派实施公告",
        },
        announcement_dates={
            "000001": date(2024, 5, 28),
            "600000": date(2024, 6, 18),
        },
        document_payloads={
            "000001": _pdf_bytes("2024-06-01"),
            "600000": _pdf_bytes("implementation announcement"),
        },
        candidate_symbols={"000001", "600000"},
    )

    with pytest.raises(CandidateReviewAdmissionError):
        require_candidate_review_admission(
            workspace=workspace,
            queue=queue,
            candidates=candidates,
            date_rule_path=_DATE_RULE_PATH,
        )

    manifest_path = next(
        workspace.root.parent.rglob("candidate_review_admission.json")
    )
    manifest = json.loads(manifest_path.read_bytes())
    unresolved = pl.read_parquet(manifest_path.with_name("unresolved_candidates.parquet"))
    assert manifest["candidate_count"] == 2
    assert manifest["admitted_count"] == 1
    assert manifest["unresolved_count"] == 1
    assert unresolved.get_column("symbol").to_list() == ["600000"]


@pytest.mark.parametrize(
    "scenario_name",
    [
        "weak_title",
        "cross_symbol_pdf",
        "invalid_or_missing_pdf",
        "candidate_date_not_in_pdf",
    ],
)
def test_candidate_review_admission_publishes_exact_blocked_reason_before_failing(
    prepared_attempt: PreparedAttempt,
    scenario_name: str,
) -> None:
    from ashare_multifactor.final_test.official_candidate_review_admission import (
        CandidateReviewAdmissionError,
        require_candidate_review_admission,
    )

    titles, dates, payloads, primary_failure = _scenario(scenario_name)
    workspace, queue, candidates = _admission_inputs(
        prepared_attempt,
        titles=titles,
        announcement_dates=dates,
        document_payloads=payloads,
        candidate_symbols={"000001"},
    )
    plans_root = workspace.root.parent / "official_review_batch_plans"
    plans_before = set(plans_root.iterdir()) if plans_root.exists() else set()

    with pytest.raises(CandidateReviewAdmissionError):
        require_candidate_review_admission(
            workspace=workspace,
            queue=queue,
            candidates=candidates,
            date_rule_path=_DATE_RULE_PATH,
        )

    manifests = list(
        workspace.root.parent.rglob("candidate_review_admission.json")
    )
    assert len(manifests) == 1
    manifest_bytes = manifests[0].read_bytes()
    manifest = json.loads(manifest_bytes)
    unresolved_path = manifests[0].with_name("unresolved_candidates.parquet")
    unresolved_bytes = unresolved_path.read_bytes()
    unresolved = pl.read_parquet(unresolved_path)
    assert manifest["status"] == "blocked"
    assert manifest["candidate_count"] == 1
    assert manifest["admitted_count"] == 0
    assert manifest["unresolved_count"] == 1
    assert unresolved.height == 1
    assert unresolved.item(0, "failure_codes").to_list() == [
        primary_failure,
        "no_single_announcement_satisfies_all_requirements",
    ]
    assert (
        set(plans_root.iterdir()) if plans_root.exists() else set()
    ) == plans_before

    if scenario_name == "candidate_date_not_in_pdf":
        with pytest.raises(CandidateReviewAdmissionError):
            require_candidate_review_admission(
                workspace=workspace,
                queue=queue,
                candidates=candidates,
                date_rule_path=_DATE_RULE_PATH,
            )
        assert manifests[0].read_bytes() == manifest_bytes
        assert unresolved_path.read_bytes() == unresolved_bytes
