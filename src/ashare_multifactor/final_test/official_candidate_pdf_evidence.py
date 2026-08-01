"""Bounded PDF evidence inspection shared by native and rendition adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO
import re
import unicodedata

from pypdf import PdfReader

from ashare_multifactor.final_test.official_announcement_routing import (
    route_announcement_title,
)


_STRONG_REASON_PREFIX = "corporate_action_strong_implementation:"
_CODE_BOILERPLATE = re.compile(
    r"(?:证券|股票)代码[:：]?\d+(?=(?:证券|股票)简称|公告编号|$)"
)
_SHORT_NAME_BOILERPLATE = re.compile(
    r"(?:证券|股票)简称[:：]?.*?(?=公告编号[:：]|$)"
)
_ANNOUNCEMENT_NUMBER_BOILERPLATE = re.compile(
    r"公告编号[:：]?(?:临)?\d{4}[-—]\d+(?:号)?"
)
_EXPLICIT_PREAMBLE = re.compile(
    r"(?:本次实施的?(?:利润)?分配方案|实施权益分派方案)"
)
_NON_CURRENT_IMPLEMENTATION = re.compile(
    r"(?:若|如|倘若|未来|后续|可能|预计|计划|拟(?:于|在)?|将(?:于|在)?)"
    r"[^。；;]{0,48}实施"
)
_SECURITY_CODE_FIELD = re.compile(r"(?:证券|股票)代码[:：]?(\d+)")
_DATE_LABEL = re.compile(r"(?:除权除息日|除息日|除权日)[:：]?\s*")
_NUMERIC_LABELLED_DATE = re.compile(
    rf"{_DATE_LABEL.pattern}(\d{{4}})([-/.])(\d{{1,2}})\2(\d{{1,2}})"
)
_CHINESE_LABELLED_DATE = re.compile(
    rf"{_DATE_LABEL.pattern}(\d{{4}})年(\d{{1,2}})月(\d{{1,2}})日"
)
_ADJACENT_DATE_CELL = r"\d{4}(?:[-/.]|年)"
_REPORT_PERIOD = re.compile(
    r"(?<!\d)(20\d{2})年?(年度|半年度|第一季度|第三季度)"
)
_DISTRIBUTION_TITLE = re.compile(
    r"(?:权益分派|利润分配|利润分派|分红派息|现金分红)"
)
_CASH_PER_TEN = re.compile(
    r"每10股(?:派发?|分配)(?:现金红利|现金股利)?"
    r"(?:人民币)?(\d+(?:\.\d+)?)元"
)
_BONUS_PER_TEN = re.compile(
    r"每10股(?:送红股|送股)(\d+(?:\.\d+)?)股"
)
_TRANSFER_PER_TEN = re.compile(
    r"每10股(?:以资本公积金(?:向全体股东)?)?"
    r"转增(\d+(?:\.\d+)?)股"
)


@dataclass(frozen=True)
class CandidatePdfEvidence:
    """Facts extracted from one PDF without joining another document."""

    text: str
    leading_block: str
    routing_text: str
    strong_reason: str | None
    labelled_security_codes: tuple[str, ...]
    labelled_dates: tuple[date, ...]
    report_periods: tuple[str, ...]
    distribution_terms: tuple[str, ...]

    def contains_symbol(self, symbol: str) -> bool:
        escaped = re.escape(symbol)
        return (
            re.search(rf"(?<!\d){escaped}(?!\d)", self.text) is not None
            or symbol in self.labelled_security_codes
        )

    def has_unique_leading_symbol(self, symbol: str) -> bool:
        return set(self.labelled_security_codes) == {symbol}

    def has_date(self, value: date) -> bool:
        return date_in_text(value, self.text)

    def has_labelled_leading_date(self, value: date) -> bool:
        return labelled_date_in_text(value, self.leading_block)


def inspect_candidate_pdf(payload: bytes) -> CandidatePdfEvidence:
    """Inspect at most one bounded first-page block for routing semantics."""
    try:
        pages = PdfReader(BytesIO(payload)).pages
        text = "\n".join(page.extract_text() or "" for page in pages)
        first_page = pages[0].extract_text() or ""
    except (EOFError, IndexError, OSError, TypeError, ValueError):
        return CandidatePdfEvidence("", "", "", None, (), (), (), ())
    normalized_text = unicodedata.normalize("NFKC", text)
    block_lines = _bounded_lines(first_page)
    leading_block = "\n".join(block_lines)
    routing_text = "\n".join(_title_lines(block_lines))
    return CandidatePdfEvidence(
        text=_compact_text(normalized_text),
        leading_block=leading_block,
        routing_text=routing_text,
        strong_reason=_strong_reason(
            routing_text,
            leading_block,
        ),
        labelled_security_codes=_labelled_security_codes(leading_block),
        labelled_dates=_labelled_dates(leading_block),
        report_periods=_report_periods(routing_text),
        distribution_terms=_distribution_terms(leading_block),
    )


def date_in_text(value: date, text: str) -> bool:
    """Match exact numeric date variants with digit boundaries."""
    return any(
        re.search(_bounded_date_pattern(token), text) is not None
        for token in _date_tokens(value)
    )


def labelled_date_in_text(value: date, text: str) -> bool:
    """Require one exact date immediately after an ex-date label."""
    return any(
        re.search(
            rf"{_DATE_LABEL.pattern}{_bounded_date_pattern(token)}",
            text,
        )
        is not None
        for token in _date_tokens(value)
    )


def _bounded_lines(first_page: str) -> list[str]:
    lines: list[str] = []
    consumed = 0
    for raw_line in unicodedata.normalize("NFKC", first_page).splitlines():
        line = _compact_text(raw_line.strip())
        if not line:
            continue
        remaining = 800 - consumed
        if remaining <= 0:
            break
        lines.append(line[:remaining])
        consumed += len(lines[-1])
        if len(lines) == 20:
            break
    return lines


def _compact_text(text: str) -> str:
    return "".join(character for character in text if not character.isspace())


def _title_lines(lines: list[str]) -> list[str]:
    title_lines: list[str] = []
    for raw_line in lines:
        line = _strip_boilerplate_fields(raw_line)
        if not line:
            continue
        if not line.isdigit():
            title_lines.append(line)
    return title_lines


def _strong_reason(title_text: str, leading_block: str) -> str | None:
    title_lines = title_text.splitlines()
    for start in range(len(title_lines)):
        for width in range(1, min(3, len(title_lines) - start) + 1):
            title_candidate = "".join(title_lines[start : start + width])
            if _NON_CURRENT_IMPLEMENTATION.search(title_candidate):
                continue
            title_route = route_announcement_title(title_candidate)
            if _is_strong(title_route):
                return title_route["reason"]
            if "公告" in title_lines[start + width - 1]:
                break
    compact_leading = leading_block.replace("\n", "")
    if (
        _EXPLICIT_PREAMBLE.search(compact_leading) is None
        or _NON_CURRENT_IMPLEMENTATION.search(compact_leading) is not None
    ):
        return None
    preamble_route = route_announcement_title(compact_leading)
    return preamble_route["reason"] if _is_strong(preamble_route) else None


def _is_strong(route: dict[str, str]) -> bool:
    return (
        route["route"] == "candidate"
        and route["candidate_type"] == "corporate_action"
        and route["reason"].startswith(_STRONG_REASON_PREFIX)
    )


def _date_tokens(value: date) -> set[str]:
    year, month, day = value.year, value.month, value.day
    months = {str(month), f"{month:02d}"}
    days = {str(day), f"{day:02d}"}
    return {
        f"{year}{separator}{month_token}{separator}{day_token}"
        for separator in ("-", "/", ".")
        for month_token in months
        for day_token in days
    } | {
        f"{year}年{month_token}月{day_token}日"
        for month_token in months
        for day_token in days
    }


def _labelled_security_codes(text: str) -> tuple[str, ...]:
    codes: set[str] = set()
    for digits in _SECURITY_CODE_FIELD.findall(text):
        if len(digits) % 6 != 0:
            continue
        codes.update(
            digits[index : index + 6]
            for index in range(0, len(digits), 6)
        )
    return tuple(sorted(codes))


def _strip_boilerplate_fields(line: str) -> str:
    value = _CODE_BOILERPLATE.sub("", line)
    value = _SHORT_NAME_BOILERPLATE.sub("", value)
    return _ANNOUNCEMENT_NUMBER_BOILERPLATE.sub("", value)


def _bounded_date_pattern(token: str) -> str:
    return (
        rf"(?<!\d){re.escape(token)}"
        rf"(?:(?!\d)|(?={_ADJACENT_DATE_CELL}))"
    )


def _labelled_dates(text: str) -> tuple[date, ...]:
    values: set[date] = set()
    for match in _NUMERIC_LABELLED_DATE.finditer(text):
        try:
            values.add(
                date(
                    int(match.group(1)),
                    int(match.group(3)),
                    int(match.group(4)),
                )
            )
        except ValueError:
            continue
    for match in _CHINESE_LABELLED_DATE.finditer(text):
        try:
            values.add(
                date(
                    int(match.group(1)),
                    int(match.group(2)),
                    int(match.group(3)),
                )
            )
        except ValueError:
            continue
    return tuple(sorted(values))


def _report_periods(text: str) -> tuple[str, ...]:
    if _DISTRIBUTION_TITLE.search(text) is None:
        return ()
    return tuple(
        sorted(
            {
                f"{match.group(1)}{match.group(2)}"
                for match in _REPORT_PERIOD.finditer(text)
            }
        )
    )


def _distribution_terms(text: str) -> tuple[str, ...]:
    compact = _compact_text(text)
    values: dict[str, str] = {}
    for name, pattern in (
        ("cash_per_10", _CASH_PER_TEN),
        ("bonus_per_10", _BONUS_PER_TEN),
        ("transfer_per_10", _TRANSFER_PER_TEN),
    ):
        matches = {_decimal_token(value) for value in pattern.findall(compact)}
        if len(matches) > 1:
            return ()
        if matches:
            values[name] = matches.pop()
    if re.search(r"(?:不送红股|不送股)", compact):
        values.setdefault("bonus_per_10", "0")
    if re.search(r"(?:不转增|不以[^,，。；;]{0,20}转增)", compact):
        values.setdefault("transfer_per_10", "0")
    return tuple(f"{name}={values[name]}" for name in sorted(values))


def _decimal_token(value: str) -> str:
    try:
        normalized = Decimal(value).normalize()
    except InvalidOperation:
        return ""
    rendered = format(normalized, "f")
    return "0" if Decimal(rendered) == 0 else rendered
