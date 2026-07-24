from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path

import yaml

from ashare_multifactor.audit.records import file_record, verify_file_record
from ashare_multifactor.data.security import market_for_symbol
from ashare_multifactor.final_test.data_inventory import write_json
from ashare_multifactor.final_test.gate import (
    FINAL_TEST_END,
    FINAL_TEST_START,
    FinalTestAuthorization,
)
from ashare_multifactor.final_test.execution_identity import (
    assert_execution_identity_authorized,
)
from ashare_multifactor.final_test.official_query_coverage import (
    OFFICIAL_QUERY_ENDPOINT,
    OfficialQueryScope,
)


OFFICIAL_MARKET_SOURCES = (("sh", "sse"), ("sz", "szse"))
SHARED_ANNOUNCEMENT_CATEGORY = "announcements"
OFFICIAL_EVIDENCE_URL_PREFIXES = (
    "https://static.cninfo.com.cn/finalpage/",
    "https://disc.static.szse.cn/download/disc/",
    "https://www.sse.com.cn/disclosure/listedinfo/announcement/",
    "https://www.sse.com.cn/assortment/stock/list/info/profit/",
)
_SOURCE_EVIDENCE_PREFIXES = {
    "sse": (
        OFFICIAL_EVIDENCE_URL_PREFIXES[0],
        OFFICIAL_EVIDENCE_URL_PREFIXES[2],
        OFFICIAL_EVIDENCE_URL_PREFIXES[3],
    ),
    "szse": (
        OFFICIAL_EVIDENCE_URL_PREFIXES[0],
        OFFICIAL_EVIDENCE_URL_PREFIXES[1],
    ),
}


@dataclass(frozen=True)
class FinalActionSourceContract:
    start: date
    end: date
    provider: str
    query_year_type: str
    query_years: tuple[int, ...]
    official_query_endpoint: str
    official_query_method: str
    official_query_categories: tuple[tuple[str, str], ...]
    allowed_url_prefixes: tuple[str, ...]
    market_sources: tuple[tuple[str, str], ...]
    supported_markets: tuple[str, ...]
    required_files: tuple[str, ...]


def load_action_source_contract(path: Path) -> FinalActionSourceContract:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    start, end = (date.fromisoformat(str(value)) for value in raw["period"])
    structured = raw["structured_source"]
    query_coverage = raw["official_query_coverage"]
    contract = FinalActionSourceContract(
        start=start,
        end=end,
        provider=str(structured["provider"]),
        query_year_type=str(structured["query_year_type"]),
        query_years=tuple(int(value) for value in structured["query_years"]),
        official_query_endpoint=str(query_coverage["endpoint"]),
        official_query_method=str(query_coverage["method"]),
        official_query_categories=tuple(
            (str(category), str(query_category))
            for category, query_category in query_coverage["categories"].items()
        ),
        allowed_url_prefixes=tuple(raw["official_evidence_url_prefixes"]),
        market_sources=tuple(
            (str(market), str(source))
            for market, source in raw["official_market_sources"].items()
        ),
        supported_markets=tuple(raw.get("supported_markets", ())),
        required_files=tuple(raw["required_execution_files"]),
    )
    if (
        (contract.start, contract.end) != (FINAL_TEST_START, FINAL_TEST_END)
        or contract.provider != "baostock_query_dividend_data"
        or contract.query_year_type != "operate"
        or contract.query_years != (2022, 2023, 2024, 2025)
        or contract.official_query_endpoint != OFFICIAL_QUERY_ENDPOINT
        or contract.official_query_method != "POST"
        or contract.official_query_categories
        != (("corporate_actions", ""), ("security_events", ""))
        or set(contract.required_files)
        != {"corporate_actions.parquet", "security_events.parquet"}
        or contract.allowed_url_prefixes != OFFICIAL_EVIDENCE_URL_PREFIXES
        or contract.market_sources != OFFICIAL_MARKET_SOURCES
        or contract.supported_markets != ("sh", "sz")
    ):
        raise ValueError("invalid frozen final execution source contract")
    return contract


def official_query_scope(
    contract: FinalActionSourceContract,
    *,
    symbol: str,
    category: str,
    org_id: str | None = None,
) -> OfficialQueryScope:
    """Return the shared announcement query for one logical evidence category."""
    try:
        query_category = dict(contract.official_query_categories)[category]
        market = market_for_symbol(symbol)
    except (KeyError, ValueError) as error:
        raise ValueError("official query category or symbol is invalid") from error
    if market not in contract.supported_markets:
        raise ValueError("official query symbol escapes supported markets")
    return OfficialQueryScope(
        symbol=symbol,
        market=market,
        category=SHARED_ANNOUNCEMENT_CATEGORY,
        query_category=query_category,
        start=contract.start,
        end=contract.end,
        org_id=org_id,
    )


def assert_allowed_evidence_url(
    url: str,
    contract: FinalActionSourceContract,
) -> None:
    if not any(url.startswith(prefix) for prefix in contract.allowed_url_prefixes):
        raise ValueError(f"invalid official evidence URL: {url}")


def evidence_url_matches_source(source: str, url: str) -> bool:
    return any(url.startswith(prefix) for prefix in _SOURCE_EVIDENCE_PREFIXES.get(source, ()))


def build_execution_input_manifest(
    destination: Path,
    *,
    authorization: FinalTestAuthorization,
    files: dict[str, Path],
    execution_identity: Mapping[str, object] | None = None,
    write: bool = True,
) -> dict[str, object]:
    _assert_authorization(authorization)
    required = {"corporate_actions.parquet", "security_events.parquet"}
    if set(files) != required:
        raise ValueError("execution-input files differ from the frozen contract")
    root = destination.parent.resolve()
    records = []
    for name, path in sorted(files.items()):
        if path.name != name:
            raise ValueError("execution-input filename is not canonical")
        try:
            records.append(
                file_record(path, root=root, role=name.removesuffix(".parquet")).to_dict()
            )
        except ValueError as error:
            raise ValueError("execution input must remain inside execution-input root") from error
    payload: dict[str, object] = {
        "attempt_id": authorization.attempt_id,
        "approval_id": authorization.approval_id,
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "robustness_release": authorization.robustness_release,
        "period": [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()],
        "files": records,
    }
    if execution_identity is not None:
        payload.update(
            assert_execution_identity_authorized(execution_identity, authorization)
        )
    if write:
        write_json(destination, payload)
    return payload


def verify_execution_input_manifest(
    path: Path,
    authorization: FinalTestAuthorization,
    *,
    execution_identity: Mapping[str, object] | None = None,
) -> dict[str, Path]:
    _assert_authorization(authorization)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("execution-input manifest path uses a symlink")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError("invalid execution-input manifest") from error
    expected = {
        "attempt_id": authorization.attempt_id,
        "approval_id": authorization.approval_id,
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "robustness_release": authorization.robustness_release,
        "period": [FINAL_TEST_START.isoformat(), FINAL_TEST_END.isoformat()],
    }
    if not isinstance(payload, dict) or any(
        payload.get(key) != value for key, value in expected.items()
    ):
        raise ValueError("execution-input manifest differs from authorization")
    if execution_identity is not None:
        bound_identity = assert_execution_identity_authorized(
            execution_identity,
            authorization,
        )
        if any(payload.get(key) != value for key, value in bound_identity.items()):
            raise ValueError("execution-input manifest execution identity differs")
    records = payload.get("files")
    if not isinstance(records, list):
        raise ValueError("execution-input manifest files are missing")
    by_name = {str(record.get("path")): record for record in records if isinstance(record, dict)}
    required = {"corporate_actions.parquet", "security_events.parquet"}
    if set(by_name) != required or len(records) != len(required):
        raise ValueError("execution-input manifest file set is incomplete")
    resolved = {}
    for name, record in sorted(by_name.items()):
        try:
            resolved[name] = verify_file_record(record, root=path.parent)
        except (FileNotFoundError, TypeError, ValueError) as error:
            raise ValueError(f"execution-input digest mismatch: {name}") from error
    snapshot_records = payload.get("coverage_snapshot_files")
    if execution_identity is not None and snapshot_records is None:
        raise ValueError(
            "execution-input coverage snapshot inventory is missing"
        )
    if snapshot_records is not None:
        if not isinstance(snapshot_records, list) or not snapshot_records:
            raise ValueError("execution-input coverage snapshot inventory is invalid")
        snapshot_paths: set[str] = set()
        for record in snapshot_records:
            if not isinstance(record, dict) or not isinstance(record.get("path"), str):
                raise ValueError("execution-input coverage snapshot inventory is invalid")
            relative = Path(str(record["path"]))
            if (
                relative.is_absolute()
                or not relative.parts
                or relative.parts[0] != "coverage_snapshot"
                or ".." in relative.parts
                or relative.as_posix() in snapshot_paths
            ):
                raise ValueError("execution-input coverage snapshot path is unsafe")
            snapshot_paths.add(relative.as_posix())
            try:
                verify_file_record(record, root=path.parent)
            except (FileNotFoundError, TypeError, ValueError) as error:
                raise ValueError(
                    "execution-input coverage snapshot digest mismatch"
                ) from error
        snapshot_items = list((path.parent / "coverage_snapshot").rglob("*"))
        if any(item.is_symlink() for item in snapshot_items):
            raise ValueError("execution-input coverage snapshot uses a symlink")
        actual_snapshot_paths = {
            item.relative_to(path.parent).as_posix()
            for item in snapshot_items
            if item.is_file()
        }
        if actual_snapshot_paths != snapshot_paths:
            raise ValueError("execution-input coverage snapshot inventory differs")
    return resolved


def _assert_authorization(authorization: FinalTestAuthorization) -> None:
    if not isinstance(authorization, FinalTestAuthorization):
        raise TypeError("authorization must be a FinalTestAuthorization")
    if authorization.test_period != (FINAL_TEST_START, FINAL_TEST_END):
        raise ValueError("authorization period differs from final execution contract")
