"""One-shot final-test authorization and execution interfaces."""

from ashare_multifactor.final_test.incident_archive import archive_failed_attempt
from ashare_multifactor.final_test.official_query_incident_archive import (
    archive_incomplete_query_collection,
)

__all__ = ["archive_failed_attempt", "archive_incomplete_query_collection"]
