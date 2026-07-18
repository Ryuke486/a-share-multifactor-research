"""Exact identities that bind one final-test execution to its evidence snapshot."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ashare_multifactor.final_test.gate import FinalTestAuthorization


EXECUTION_IDENTITY_FIELDS = frozenset(
    {
        "attempt_id",
        "execution_id",
        "sealed_protocol_sha256",
        "prepare_manifest_sha256",
        "security_event_coverage_sha256",
        "corporate_action_coverage_sha256",
        "coverage_snapshot_manifest_sha256",
    }
)
_SHA256 = re.compile(r"[0-9a-f]{64}")


def validate_execution_identity(
    identity: Mapping[str, object],
) -> dict[str, str]:
    """Return an exact, primitive execution identity or fail closed."""
    if set(identity) != EXECUTION_IDENTITY_FIELDS:
        raise ValueError("final-test execution identity schema differs")
    values = {field: identity.get(field) for field in EXECUTION_IDENTITY_FIELDS}
    if (
        not isinstance(values["attempt_id"], str)
        or not values["attempt_id"]
        or not isinstance(values["execution_id"], str)
        or not values["execution_id"]
        or any(
            not isinstance(values[field], str)
            or _SHA256.fullmatch(str(values[field])) is None
            for field in EXECUTION_IDENTITY_FIELDS
            - {"attempt_id", "execution_id"}
        )
    ):
        raise ValueError("final-test execution identity is invalid")
    return {field: str(values[field]) for field in EXECUTION_IDENTITY_FIELDS}


def assert_execution_identity_authorized(
    identity: Mapping[str, object],
    authorization: FinalTestAuthorization,
) -> dict[str, str]:
    """Bind a validated execution identity to the consumed authorization."""
    verified = validate_execution_identity(identity)
    if (
        verified["attempt_id"] != authorization.attempt_id
        or verified["sealed_protocol_sha256"]
        != authorization.sealed_protocol_sha256
    ):
        raise ValueError("final-test execution identity differs from authorization")
    return verified
