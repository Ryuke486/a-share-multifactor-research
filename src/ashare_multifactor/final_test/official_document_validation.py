"""Content validation for bytes claimed to be official PDF evidence."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError


@dataclass(frozen=True)
class ValidatedOfficialDocument:
    """Stable identity and structural facts for one readable PDF."""

    sha256: str
    size_bytes: int
    page_count: int
    media_type: str = "application/pdf"


class OfficialDocumentValidationError(ValueError):
    """Bytes that cannot be admitted to the official-document cache."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


def validate_official_document(
    payload: bytes,
    *,
    source_url: str,
) -> ValidatedOfficialDocument:
    """Reject non-PDF bytes before any immutable evidence publication."""
    if not isinstance(source_url, str) or not source_url:
        raise ValueError("official document source URL is invalid")
    if not isinstance(payload, bytes) or not payload.startswith(b"%PDF-"):
        raise OfficialDocumentValidationError(
            "not_pdf",
            "official document response is not a PDF",
        )
    try:
        reader = PdfReader(BytesIO(payload), strict=False)
        if reader.is_encrypted:
            raise OfficialDocumentValidationError(
                "encrypted_pdf",
                "official document PDF is encrypted",
            )
        page_count = len(reader.pages)
    except OfficialDocumentValidationError:
        raise
    except (EOFError, OSError, PdfReadError, TypeError, ValueError) as error:
        raise OfficialDocumentValidationError(
            "unreadable_pdf",
            "official document PDF is unreadable",
        ) from error
    if page_count <= 0:
        raise OfficialDocumentValidationError(
            "empty_pdf",
            "official document PDF has no pages",
        )
    return ValidatedOfficialDocument(
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        page_count=page_count,
    )
