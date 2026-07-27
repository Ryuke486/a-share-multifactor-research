from __future__ import annotations

from io import BytesIO

import pytest


def test_html_error_page_is_rejected_before_document_publication() -> None:
    from ashare_multifactor.final_test.official_document_validation import (
        OfficialDocumentValidationError,
        validate_official_document,
    )

    with pytest.raises(OfficialDocumentValidationError, match="PDF") as failure:
        validate_official_document(
            b"<html><body>document temporarily unavailable</body></html>",
            source_url="https://static.cninfo.com.cn/finalpage/2024-01-01/notice.PDF",
        )

    assert failure.value.reason == "not_pdf"


def test_readable_pdf_returns_structural_identity() -> None:
    from pypdf import PdfWriter

    from ashare_multifactor.final_test.official_document_validation import (
        validate_official_document,
    )

    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(stream)

    validated = validate_official_document(
        stream.getvalue(),
        source_url="https://static.cninfo.com.cn/finalpage/2024-01-01/notice.PDF",
    )

    assert validated.page_count == 1
    assert validated.size_bytes == len(stream.getvalue())
    assert validated.media_type == "application/pdf"


def test_pdf_signature_without_readable_structure_is_rejected() -> None:
    from ashare_multifactor.final_test.official_document_validation import (
        OfficialDocumentValidationError,
        validate_official_document,
    )

    with pytest.raises(OfficialDocumentValidationError) as failure:
        validate_official_document(
            b"%PDF-1.7\nnot a real document\n%%EOF",
            source_url="https://static.cninfo.com.cn/finalpage/2024-01-01/notice.PDF",
        )

    assert failure.value.reason == "unreadable_pdf"


def test_encrypted_pdf_is_rejected() -> None:
    from pypdf import PdfWriter

    from ashare_multifactor.final_test.official_document_validation import (
        OfficialDocumentValidationError,
        validate_official_document,
    )

    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.encrypt("secret")
    writer.write(stream)

    with pytest.raises(OfficialDocumentValidationError) as failure:
        validate_official_document(
            stream.getvalue(),
            source_url="https://static.cninfo.com.cn/finalpage/2024-01-01/notice.PDF",
        )

    assert failure.value.reason == "encrypted_pdf"


def test_zero_page_pdf_is_rejected() -> None:
    from pypdf import PdfWriter

    from ashare_multifactor.final_test.official_document_validation import (
        OfficialDocumentValidationError,
        validate_official_document,
    )

    stream = BytesIO()
    PdfWriter().write(stream)

    with pytest.raises(OfficialDocumentValidationError) as failure:
        validate_official_document(
            stream.getvalue(),
            source_url="https://static.cninfo.com.cn/finalpage/2024-01-01/notice.PDF",
        )

    assert failure.value.reason == "empty_pdf"
