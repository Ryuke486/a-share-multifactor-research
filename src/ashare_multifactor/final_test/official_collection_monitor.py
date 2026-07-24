"""Atomic collector heartbeats and independent read-only status evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
from typing import Callable
from uuid import uuid4

from ashare_multifactor.final_test.official_collection_progress import (
    OfficialCollectionProgressObserver,
)
from ashare_multifactor.final_test.official_query_coverage import canonical_json_bytes
from ashare_multifactor.final_test.official_query_coverage import OfficialQueryScope


_HEARTBEAT_NAME = "collection_heartbeat.json"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_OBJECT = re.compile(r"[0-9a-f]{40}")
_IDENTITY_FIELDS = {
    "attempt_id",
    "git_commit",
    "git_tree",
    "sealed_protocol_sha256",
    "prepare_manifest_sha256",
}


@dataclass(frozen=True)
class CollectionStatus:
    """One read-only status snapshot suitable for CLI or notification output."""

    level: str
    reason: str
    phase: str
    completed_securities: int
    total_securities: int
    current_symbol: str
    current_period: tuple[str, str]
    current_page: tuple[int, int]
    request_count: int
    split_count: int
    eta_seconds: int


class CollectionHeartbeatReporter(OfficialCollectionProgressObserver):
    """Translate collector progress events into throttled atomic heartbeats."""

    def __init__(
        self,
        path: Path,
        *,
        identity: dict[str, str],
        total_securities: int,
        now: Callable[[], datetime],
        interval: timedelta = timedelta(seconds=60),
    ) -> None:
        if total_securities <= 0 or interval <= timedelta(0):
            raise ValueError("official collection heartbeat reporter is invalid")
        self._path = path
        self._identity = _validate_identity(identity)
        self._total = total_securities
        self._now = now
        self._interval = interval
        observed_at = _aware(now(), label="reporter start")
        self._phase_started_at = observed_at
        self._last_progress_at = observed_at
        self._last_write_at: datetime | None = None
        self._phase = "starting"
        self._completed = 0
        self._symbol = "pending"
        self._period = ("pending", "pending")
        self._page = (0, 0)
        self._request_count = 0
        self._split_count = 0

    def identity_request(self, *, symbol: str, completed: int, total: int) -> None:
        self._assert_total(total)
        changed = self._set_phase("resolve_identities") or self._symbol != symbol
        self._completed = completed
        self._symbol = symbol
        self._period = ("identity", "identity")
        self._page = (0, 0)
        self._request_count += 1
        self._write(force=changed)

    def identity_completed(self, *, symbol: str, completed: int, total: int) -> None:
        self._assert_total(total)
        changed = self._set_phase("resolve_identities") or self._symbol != symbol
        self._completed = completed
        self._symbol = symbol
        self._period = ("identity", "identity")
        self._page = (0, 0)
        self._last_progress_at = _aware(self._now(), label="identity progress")
        self._write(force=changed or completed == total)

    def query_request(self, *, scope: OfficialQueryScope, page: int) -> None:
        period = (scope.start.isoformat(), scope.end.isoformat())
        changed = (
            self._set_phase("collect_queries")
            or self._symbol != scope.symbol
            or self._period != period
        )
        self._symbol = scope.symbol
        self._period = period
        self._page = (page, max(page, self._page[1]))
        self._request_count += 1
        self._write(force=changed)

    def query_page_verified(
        self,
        *,
        scope: OfficialQueryScope,
        page: int,
        total_pages: int,
    ) -> None:
        self._set_phase("collect_queries")
        self._symbol = scope.symbol
        self._period = (scope.start.isoformat(), scope.end.isoformat())
        self._page = (page, total_pages)
        self._last_progress_at = _aware(self._now(), label="query progress")
        self._write(force=False)

    def split_recorded(self, *, scope: OfficialQueryScope) -> None:
        self._set_phase("collect_queries")
        self._symbol = scope.symbol
        self._period = (scope.start.isoformat(), scope.end.isoformat())
        self._split_count += 1
        self._last_progress_at = _aware(self._now(), label="split progress")
        self._write(force=True)

    def security_completed(
        self,
        *,
        scope: OfficialQueryScope,
        completed: int,
        total: int,
    ) -> None:
        self._assert_total(total)
        self._set_phase("collect_queries")
        self._completed = completed
        self._symbol = scope.symbol
        self._period = (scope.start.isoformat(), scope.end.isoformat())
        self._last_progress_at = _aware(self._now(), label="security progress")
        self._write(force=True)

    def finish(self, *, completed: int) -> None:
        if completed < 0 or completed > self._total:
            raise ValueError("official collection heartbeat finish is invalid")
        self._set_phase(
            "collect_queries_complete"
            if completed == self._total
            else "collect_queries_paused"
        )
        self._completed = completed
        if completed == self._total:
            self._symbol = "complete"
            self._page = (0, 0)
        self._last_progress_at = _aware(self._now(), label="finish progress")
        self._write(force=True)

    def _assert_total(self, total: int) -> None:
        if total != self._total:
            raise ValueError("official collection heartbeat scope differs")

    def _set_phase(self, phase: str) -> bool:
        if self._phase == phase:
            return False
        self._phase = phase
        self._phase_started_at = _aware(self._now(), label="phase start")
        self._completed = 0
        return True

    def _write(self, *, force: bool) -> None:
        observed_at = _aware(self._now(), label="heartbeat")
        if (
            not force
            and self._last_write_at is not None
            and observed_at - self._last_write_at < self._interval
        ):
            return
        write_collection_heartbeat(
            self._path,
            identity=self._identity,
            phase=self._phase,
            completed_securities=self._completed,
            total_securities=self._total,
            current_symbol=self._symbol,
            current_period=self._period,
            current_page=self._page,
            request_count=self._request_count,
            split_count=self._split_count,
            last_progress_at=self._last_progress_at,
            heartbeat_at=observed_at,
            eta_seconds=self._eta_seconds(observed_at),
        )
        self._last_write_at = observed_at

    def _eta_seconds(self, observed_at: datetime) -> int:
        if self._completed <= 0:
            return 0
        elapsed = max(0.0, (observed_at - self._phase_started_at).total_seconds())
        remaining = self._total - self._completed
        return round(elapsed / self._completed * remaining)


def write_collection_heartbeat(
    path: Path,
    *,
    identity: dict[str, str],
    phase: str,
    completed_securities: int,
    total_securities: int,
    current_symbol: str,
    current_period: tuple[str, str],
    current_page: tuple[int, int],
    request_count: int,
    split_count: int,
    last_progress_at: datetime,
    heartbeat_at: datetime,
    eta_seconds: int,
) -> None:
    """Atomically replace one mutable runtime heartbeat outside immutable evidence."""
    _validate_path(path)
    payload = {
        "schema_version": "1",
        "role": "official_collection_heartbeat",
        "identity": _validate_identity(identity),
        "phase": _nonempty(phase, label="phase"),
        "completed_securities": _count(completed_securities, label="completed"),
        "total_securities": _count(total_securities, label="total"),
        "current_symbol": _nonempty(current_symbol, label="symbol"),
        "current_period": list(_period(current_period)),
        "current_page": list(_page(current_page)),
        "request_count": _count(request_count, label="request count"),
        "split_count": _count(split_count, label="split count"),
        "last_progress_at": _aware(last_progress_at, label="last progress").isoformat(),
        "heartbeat_at": _aware(heartbeat_at, label="heartbeat").isoformat(),
        "eta_seconds": _count(eta_seconds, label="ETA"),
    }
    if payload["completed_securities"] > payload["total_securities"]:
        raise ValueError("official collection heartbeat progress is invalid")
    encoded = canonical_json_bytes(payload)
    temporary = path.parent / f".{_HEARTBEAT_NAME}.{uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def read_collection_status(
    path: Path,
    *,
    expected_identity: dict[str, str],
    now: datetime,
) -> CollectionStatus:
    """Read and classify one heartbeat without writing to collector state."""
    _validate_path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("official collection heartbeat is missing or unsafe")
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("official collection heartbeat is invalid") from error
    if (
        not isinstance(payload, dict)
        or raw != canonical_json_bytes(payload)
        or payload.get("schema_version") != "1"
        or payload.get("role") != "official_collection_heartbeat"
        or payload.get("identity") != _validate_identity(expected_identity)
    ):
        raise ValueError("official collection heartbeat identity differs")
    heartbeat_at = _parse_time(payload.get("heartbeat_at"), label="heartbeat")
    progress_at = _parse_time(payload.get("last_progress_at"), label="last progress")
    observed_at = _aware(now, label="monitor time")
    if observed_at < heartbeat_at or heartbeat_at < progress_at:
        raise ValueError("official collection heartbeat time is invalid")
    heartbeat_age = observed_at - heartbeat_at
    progress_age = observed_at - progress_at
    if heartbeat_age >= timedelta(minutes=10):
        level = "red"
        reason = "heartbeat is older than 10 minutes"
    elif progress_age >= timedelta(minutes=5):
        level = "yellow"
        reason = "no network progress for at least 5 minutes"
    else:
        level = "green"
        reason = "collector heartbeat and network progress are current"
    return CollectionStatus(
        level=level,
        reason=reason,
        phase=_nonempty(payload.get("phase"), label="phase"),
        completed_securities=_count(
            payload.get("completed_securities"),
            label="completed",
        ),
        total_securities=_count(payload.get("total_securities"), label="total"),
        current_symbol=_nonempty(payload.get("current_symbol"), label="symbol"),
        current_period=_period(payload.get("current_period")),
        current_page=_page(payload.get("current_page")),
        request_count=_count(payload.get("request_count"), label="request count"),
        split_count=_count(payload.get("split_count"), label="split count"),
        eta_seconds=_count(payload.get("eta_seconds"), label="ETA"),
    )


def _validate_path(path: Path) -> None:
    if path.name != _HEARTBEAT_NAME or path.parent.is_symlink() or not path.parent.is_dir():
        raise ValueError("official collection heartbeat path is invalid")


def _validate_identity(value: dict[str, str]) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != _IDENTITY_FIELDS:
        raise ValueError("official collection heartbeat identity is invalid")
    if (
        not isinstance(value["attempt_id"], str)
        or not value["attempt_id"]
        or _GIT_OBJECT.fullmatch(value["git_commit"]) is None
        or _GIT_OBJECT.fullmatch(value["git_tree"]) is None
        or _SHA256.fullmatch(value["sealed_protocol_sha256"]) is None
        or _SHA256.fullmatch(value["prepare_manifest_sha256"]) is None
    ):
        raise ValueError("official collection heartbeat identity is invalid")
    return dict(value)


def _nonempty(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"official collection heartbeat {label} is invalid")
    return value


def _count(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"official collection heartbeat {label} is invalid")
    return value


def _period(value: object) -> tuple[str, str]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(not isinstance(item, str) or not item for item in value)
    ):
        raise ValueError("official collection heartbeat period is invalid")
    return value[0], value[1]


def _page(value: object) -> tuple[int, int]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in value)
        or value[0] > value[1]
    ):
        raise ValueError("official collection heartbeat page is invalid")
    return value[0], value[1]


def _aware(value: datetime, *, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"official collection heartbeat {label} is invalid")
    return value


def _parse_time(value: object, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"official collection heartbeat {label} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"official collection heartbeat {label} is invalid") from error
    return _aware(parsed, label=label)
