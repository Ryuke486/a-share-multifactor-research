"""Isolated pre-2022 rehearsal of the production official-query core."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import time

import polars as pl

from ashare_multifactor.data.security import market_for_symbol
from ashare_multifactor.final_test.action_source_contract import (
    FinalActionSourceContract,
)
from ashare_multifactor.final_test.official_announcement_catalog import (
    announcement_catalog_frame,
)
from ashare_multifactor.final_test.official_announcement_pages import (
    catalog_rows_from_packages,
)
from ashare_multifactor.final_test.official_announcement_routing import (
    announcement_routing_frame,
)
from ashare_multifactor.final_test.official_collection_progress import (
    OfficialCollectionProgressObserver,
)
from ashare_multifactor.final_test.official_document_fetcher import (
    DocumentFetchPolicy,
)
from ashare_multifactor.final_test.official_query_client import (
    OFFICIAL_SECURITY_IDENTITY_ENDPOINT,
    OfficialQueryTransientError,
    OfficialQueryTransport,
    RetryPolicy,
)
from ashare_multifactor.final_test.official_query_coverage import (
    OfficialQueryScope,
    canonical_json_bytes,
)
from ashare_multifactor.final_test.official_query_index import (
    validate_official_query_coverage_index,
)
from ashare_multifactor.final_test.official_query_packages import (
    collect_coverage,
)
from ashare_multifactor.final_test.official_rehearsal_sample import (
    select_rehearsal_sample,
)
from ashare_multifactor.final_test.official_security_identity_package import (
    identity_form,
    load_identity_package,
    publish_identity_package,
)


_MAX_VALIDATION_DATE = date(2021, 12, 31)
_REPORT_NAME = "rehearsal_report.json"
_PROGRESS_NAME = "rehearsal_progress.json"
_SAMPLE_NAME = "sample.parquet"
_CATALOG_NAME = "catalog.parquet"
_ROUTING_NAME = "routing.parquet"
_IDENTITY_INDEX_NAME = "identity_index.json"
_COVERAGE_DIRECTORY = "official_query_coverage"
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class OfficialRehearsalResult:
    """Machine-readable outputs from one isolated validation rehearsal."""

    root: Path
    sample_path: Path
    index_path: Path
    catalog_path: Path
    routing_path: Path
    report_path: Path


class _PersistentMetrics(OfficialCollectionProgressObserver):
    def __init__(
        self,
        path: Path,
        *,
        binding: dict[str, object],
        minimum_interval_seconds: float,
        bootstrap_requests: int,
        bootstrap_pages: int,
        bootstrap_splits: int,
    ) -> None:
        self._path = path
        self._binding = binding
        self._started = time.monotonic()
        existing = self._load()
        if existing is None:
            self.request_count = bootstrap_requests
            self.page_count = bootstrap_pages
            self.split_count = bootstrap_splits
            self.completed = 0
            self._base_elapsed = bootstrap_requests * minimum_interval_seconds
            self.restart_count = 1 if bootstrap_requests else 0
            self.reconstructed_from_cache = bootstrap_requests > 0
        else:
            self.request_count = int(existing["request_count"])
            self.page_count = int(existing["verified_page_count"])
            self.split_count = int(existing["split_count"])
            self.completed = int(existing["completed_securities"])
            self._base_elapsed = float(existing["active_elapsed_seconds"])
            self.restart_count = int(existing["restart_count"]) + 1
            self.reconstructed_from_cache = bool(
                existing["reconstructed_from_cache"]
            )
        self._persist()

    def identity_request(self, *, symbol: str, completed: int, total: int) -> None:
        del symbol, completed, total
        self.request_count += 1
        self._persist()

    def identity_completed(self, *, symbol: str, completed: int, total: int) -> None:
        del symbol, total
        self.completed = completed
        self._persist()

    def query_request(self, *, scope: OfficialQueryScope, page: int) -> None:
        del scope, page
        self.request_count += 1
        self._persist()

    def query_page_verified(
        self,
        *,
        scope: OfficialQueryScope,
        page: int,
        total_pages: int,
    ) -> None:
        del scope, page, total_pages
        self.page_count += 1
        self._persist()

    def split_recorded(self, *, scope: OfficialQueryScope) -> None:
        del scope
        self.split_count += 1
        self._persist()

    def security_completed(
        self,
        *,
        scope: OfficialQueryScope,
        completed: int,
        total: int,
    ) -> None:
        del scope, total
        self.completed = completed
        self._persist()

    def snapshot(self) -> dict[str, object]:
        self._persist()
        return {
            "request_count": self.request_count,
            "verified_page_count": self.page_count,
            "split_count": self.split_count,
            "active_elapsed_seconds": self._elapsed(),
            "restart_count": self.restart_count,
            "reconstructed_from_cache": self.reconstructed_from_cache,
        }

    def _elapsed(self) -> float:
        return self._base_elapsed + max(0.0, time.monotonic() - self._started)

    def _load(self) -> dict[str, object] | None:
        if not self._path.exists():
            return None
        if self._path.is_symlink() or not self._path.is_file():
            raise ValueError("official rehearsal progress is unsafe")
        try:
            payload = json.loads(self._path.read_bytes())
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("official rehearsal progress is invalid") from error
        if (
            not isinstance(payload, dict)
            or self._path.read_bytes() != canonical_json_bytes(payload)
            or payload.get("schema_version") != "1"
            or payload.get("role") != "official_rehearsal_progress"
            or payload.get("binding") != self._binding
        ):
            raise ValueError("official rehearsal progress identity differs")
        return payload

    def _persist(self) -> None:
        payload = canonical_json_bytes(
            {
                "schema_version": "1",
                "role": "official_rehearsal_progress",
                "binding": self._binding,
                "request_count": self.request_count,
                "verified_page_count": self.page_count,
                "split_count": self.split_count,
                "completed_securities": self.completed,
                "active_elapsed_seconds": round(self._elapsed(), 6),
                "restart_count": self.restart_count,
                "reconstructed_from_cache": self.reconstructed_from_cache,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        temporary = self._path.with_name(f".{_PROGRESS_NAME}.{os.getpid()}.tmp")
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self._path)


def run_official_collection_rehearsal(
    *,
    panel: pl.DataFrame,
    output_root: Path,
    contract: FinalActionSourceContract,
    transport: OfficialQueryTransport,
    policy: RetryPolicy,
    sample_size: int,
    required_symbols: tuple[str, ...],
    start: date,
    end: date,
    projected_universe_size: int,
    implementation_sha256: str,
    sleep: Callable[[float], None] = time.sleep,
) -> OfficialRehearsalResult:
    """Run the production slicer against an isolated validation-period sample."""
    _validate_inputs(
        output_root=output_root,
        contract=contract,
        transport=transport,
        policy=policy,
        start=start,
        end=end,
        projected_universe_size=projected_universe_size,
        implementation_sha256=implementation_sha256,
        sleep=sleep,
    )
    output_root.mkdir(parents=True, exist_ok=True)
    _assert_safe_root(output_root)
    sample = select_rehearsal_sample(
        panel,
        sample_size=sample_size,
        required_symbols=required_symbols,
    )
    sample_bytes = _parquet_bytes(sample)
    existing = _existing_result(
        output_root,
        expected_period=(start.isoformat(), end.isoformat()),
        sample_sha256=hashlib.sha256(sample_bytes).hexdigest(),
        projected_universe_size=projected_universe_size,
        implementation_sha256=implementation_sha256,
    )
    if existing is not None:
        return existing

    started = time.monotonic()
    created_at = datetime.now(timezone.utc).isoformat()
    sample_path = output_root / _SAMPLE_NAME
    _write_or_verify(sample_path, sample_bytes)

    identities_root = output_root / "official_security_identities"
    coverage_root = output_root / _COVERAGE_DIRECTORY
    identities_root.mkdir(mode=0o700, exist_ok=True)
    coverage_root.mkdir(mode=0o700, exist_ok=True)
    sample_sha256 = hashlib.sha256(sample_bytes).hexdigest()
    bootstrap_requests, bootstrap_pages, bootstrap_splits = _bootstrap_metrics(
        identities_root,
        coverage_root,
    )
    metrics = _PersistentMetrics(
        output_root / _PROGRESS_NAME,
        binding={
            "period": [start.isoformat(), end.isoformat()],
            "sample_sha256": sample_sha256,
            "implementation_sha256": implementation_sha256,
        },
        minimum_interval_seconds=policy.minimum_interval_seconds,
        bootstrap_requests=bootstrap_requests,
        bootstrap_pages=bootstrap_pages,
        bootstrap_splits=bootstrap_splits,
    )
    identities_fd = os.open(
        identities_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        identities, identity_records = _collect_identities(
            identities_fd,
            sample=sample,
            transport=transport,
            policy=policy,
            created_at=created_at,
            metrics=metrics,
            sleep=sleep,
        )
    finally:
        os.close(identities_fd)
    _write_or_verify(
        identities_root / _IDENTITY_INDEX_NAME,
        canonical_json_bytes(
            {
                "schema_version": "1",
                "role": "official_rehearsal_identity_index",
                "period": [start.isoformat(), end.isoformat()],
                "records": identity_records,
            }
        ),
    )

    scopes = tuple(
        sorted(
            (
                OfficialQueryScope(
                    symbol=symbol,
                    market=market_for_symbol(symbol),
                    category="announcements",
                    query_category="",
                    start=start,
                    end=end,
                    org_id=identities[symbol],
                )
                for symbol in sample.get_column("symbol").to_list()
            ),
            key=lambda scope: (
                scope.category,
                scope.market,
                scope.symbol,
                scope.query_category,
            ),
        )
    )
    coverage_fd = os.open(
        coverage_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        progress = collect_coverage(
            coverage_fd=coverage_fd,
            coverage_root=coverage_root,
            scopes=scopes,
            transport=transport,
            policy=policy,
            max_scopes=None,
            created_at=created_at,
            progress_observer=metrics,
        )
    finally:
        os.close(coverage_fd)
    if progress.index_path is None or progress.completed_scopes != sample_size:
        raise ValueError("official rehearsal coverage is incomplete")
    verified = validate_official_query_coverage_index(
        progress.index_path,
        expected_scopes=scopes,
    )
    catalog = announcement_catalog_frame(
        catalog_rows_from_packages(
            coverage_root,
            packages=verified.packages,
            contract=contract,
        )
    )
    catalog_path = output_root / _CATALOG_NAME
    _write_or_verify(catalog_path, _parquet_bytes(catalog))
    routing = _routing_frame(catalog)
    routing_path = output_root / _ROUTING_NAME
    _write_or_verify(routing_path, _parquet_bytes(routing))

    current_elapsed = max(0.0, time.monotonic() - started)
    metric_snapshot = metrics.snapshot()
    elapsed = float(metric_snapshot["active_elapsed_seconds"])
    query_projected = round(elapsed / sample_size * projected_universe_size)
    routing_counts = {
        route: routing.filter(pl.col("route") == route).height
        for route in ("candidate", "uncertain", "excluded")
    }
    review_queue_count = routing_counts["candidate"] + routing_counts["uncertain"]
    projected_document_count = round(
        review_queue_count / sample_size * projected_universe_size
    )
    document_interval = DocumentFetchPolicy().minimum_interval_seconds
    document_lower_bound = round(projected_document_count * document_interval)
    projected = query_projected + document_lower_bound
    report_path = output_root / _REPORT_NAME
    _write_or_verify(
        report_path,
        canonical_json_bytes(
            {
                "schema_version": "1",
                "role": "official_collection_validation_rehearsal",
                "period": [start.isoformat(), end.isoformat()],
                "sample_count": sample.height,
                "sample_sha256": hashlib.sha256(sample_path.read_bytes()).hexdigest(),
                "coverage_index_sha256": verified.index_sha256,
                "leaf_package_count": len(verified.packages),
                "announcement_count": catalog.height,
                "routing_counts": routing_counts,
                "review_queue_count": review_queue_count,
                "projected_document_count": projected_document_count,
                "document_request_interval_seconds": document_interval,
                "document_request_lower_bound_seconds": document_lower_bound,
                "request_count": metric_snapshot["request_count"],
                "verified_page_count": metric_snapshot["verified_page_count"],
                "split_count": metric_snapshot["split_count"],
                "elapsed_seconds": round(elapsed, 6),
                "current_run_elapsed_seconds": round(current_elapsed, 6),
                "restart_count": metric_snapshot["restart_count"],
                "reconstructed_from_cache": metric_snapshot[
                    "reconstructed_from_cache"
                ],
                "full_universe_size": projected_universe_size,
                "query_full_universe_eta_seconds": query_projected,
                "full_universe_eta_seconds": projected,
                "within_36_hour_budget": projected <= 36 * 60 * 60,
                "implementation_sha256": implementation_sha256,
                "created_at": created_at,
            }
        ),
    )
    return OfficialRehearsalResult(
        root=output_root,
        sample_path=sample_path,
        index_path=progress.index_path,
        catalog_path=catalog_path,
        routing_path=routing_path,
        report_path=report_path,
    )


def _collect_identities(
    identities_fd: int,
    *,
    sample: pl.DataFrame,
    transport: OfficialQueryTransport,
    policy: RetryPolicy,
    created_at: str,
    metrics: _PersistentMetrics,
    sleep: Callable[[float], None],
) -> tuple[dict[str, str], list[dict[str, object]]]:
    identities: dict[str, str] = {}
    records: list[dict[str, object]] = []
    symbols = sample.get_column("symbol").to_list()
    for completed, symbol in enumerate(symbols):
        market = market_for_symbol(symbol)
        try:
            identity, record = load_identity_package(
                identities_fd,
                symbol=symbol,
                market=market,
            )
        except FileNotFoundError:
            response = _fetch_identity(
                symbol,
                transport=transport,
                policy=policy,
                metrics=metrics,
                completed=completed,
                total=len(symbols),
                sleep=sleep,
            )
            identity, record = publish_identity_package(
                identities_fd,
                symbol=symbol,
                market=market,
                response=response,
                created_at=created_at,
            )
        metrics.identity_completed(
            symbol=symbol,
            completed=completed + 1,
            total=len(symbols),
        )
        identities[symbol] = identity.org_id
        records.append(record)
    return identities, sorted(records, key=lambda record: (record["market"], record["symbol"]))


def _fetch_identity(
    symbol: str,
    *,
    transport: OfficialQueryTransport,
    policy: RetryPolicy,
    metrics: _PersistentMetrics,
    completed: int,
    total: int,
    sleep: Callable[[float], None],
) -> bytes:
    form = identity_form(symbol)
    for attempt in range(policy.attempts):
        metrics.identity_request(
            symbol=symbol,
            completed=completed,
            total=total,
        )
        try:
            payload = transport.fetch(
                OFFICIAL_SECURITY_IDENTITY_ENDPOINT,
                form,
                timeout_seconds=policy.timeout_seconds,
            )
        except OfficialQueryTransientError:
            if attempt + 1 == policy.attempts:
                raise
            sleep(policy.minimum_interval_seconds * (2**attempt))
            continue
        if not payload:
            raise ValueError("official rehearsal identity response is empty")
        sleep(policy.minimum_interval_seconds)
        return payload
    raise RuntimeError("official rehearsal identity retry loop terminated unexpectedly")


def _routing_frame(catalog: pl.DataFrame) -> pl.DataFrame:
    return announcement_routing_frame(catalog).drop("rule_version")


def _bootstrap_metrics(
    identities_root: Path,
    coverage_root: Path,
) -> tuple[int, int, int]:
    identity_count = sum(
        1 for _ in identities_root.rglob("identity_manifest.json")
    )
    page_count = 0
    for manifest_path in coverage_root.rglob("query_manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_bytes())
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("official rehearsal cached query manifest is invalid") from error
        pages = manifest.get("page_count") if isinstance(manifest, dict) else None
        if not isinstance(pages, int) or isinstance(pages, bool) or pages <= 0:
            raise ValueError("official rehearsal cached query manifest is invalid")
        page_count += pages
    split_count = sum(1 for _ in coverage_root.rglob("split.json"))
    return identity_count + page_count, page_count, split_count


def _validate_inputs(
    *,
    output_root: Path,
    contract: FinalActionSourceContract,
    transport: OfficialQueryTransport,
    policy: RetryPolicy,
    start: date,
    end: date,
    projected_universe_size: int,
    implementation_sha256: str,
    sleep: Callable[[float], None],
) -> None:
    if (
        not isinstance(output_root, Path)
        or {"final_test", "final_test_evidence"}.intersection(output_root.parts)
        or not isinstance(contract, FinalActionSourceContract)
        or not hasattr(transport, "fetch")
        or not isinstance(policy, RetryPolicy)
        or not isinstance(start, date)
        or not isinstance(end, date)
        or start > end
        or end > _MAX_VALIDATION_DATE
        or not isinstance(projected_universe_size, int)
        or isinstance(projected_universe_size, bool)
        or projected_universe_size <= 0
        or not isinstance(implementation_sha256, str)
        or _SHA256.fullmatch(implementation_sha256) is None
        or not callable(sleep)
    ):
        raise ValueError("official rehearsal input is invalid or crosses 2021")


def _assert_safe_root(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("official rehearsal output root is unsafe")
    if any(ancestor.is_symlink() for ancestor in root.parents):
        raise ValueError("official rehearsal output root uses a symlink")


def _parquet_bytes(frame: pl.DataFrame) -> bytes:
    stream = BytesIO()
    frame.write_parquet(stream, compression="zstd")
    return stream.getvalue()


def _write_or_verify(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise ValueError(f"official rehearsal artifact differs: {path.name}")
        return
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _existing_result(
    root: Path,
    *,
    expected_period: tuple[str, str],
    sample_sha256: str,
    projected_universe_size: int,
    implementation_sha256: str,
) -> OfficialRehearsalResult | None:
    report = root / _REPORT_NAME
    if not report.exists():
        return None
    paths = {
        "sample_path": root / _SAMPLE_NAME,
        "index_path": root / _COVERAGE_DIRECTORY / "official_query_coverage.json",
        "catalog_path": root / _CATALOG_NAME,
        "routing_path": root / _ROUTING_NAME,
        "report_path": report,
    }
    if report.is_symlink() or any(
        path.is_symlink() or not path.is_file() for path in paths.values()
    ):
        raise ValueError("official rehearsal completed inventory is unsafe")
    try:
        payload = json.loads(report.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("official rehearsal report is invalid") from error
    if (
        not isinstance(payload, dict)
        or report.read_bytes() != canonical_json_bytes(payload)
        or payload.get("role") != "official_collection_validation_rehearsal"
        or payload.get("period") != list(expected_period)
        or payload.get("sample_sha256") != sample_sha256
        or payload.get("full_universe_size") != projected_universe_size
        or payload.get("implementation_sha256") != implementation_sha256
    ):
        raise ValueError("official rehearsal report is invalid")
    return OfficialRehearsalResult(root=root, **paths)
