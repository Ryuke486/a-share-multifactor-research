from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path

import yaml

from ashare_multifactor.audit.records import file_record, verify_file_record
from ashare_multifactor.final_test.data_inventory import write_json
from ashare_multifactor.final_test.gate import (
    FINAL_TEST_END,
    FINAL_TEST_START,
    FinalTestAuthorization,
)


OFFICIAL_MARKET_SOURCES = (("sh", "sse"), ("sz", "szse"))
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
    allowed_url_prefixes: tuple[str, ...]
    market_sources: tuple[tuple[str, str], ...]
    supported_markets: tuple[str, ...]
    required_files: tuple[str, ...]


def load_action_source_contract(path: Path) -> FinalActionSourceContract:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    start, end = (date.fromisoformat(str(value)) for value in raw["period"])
    structured = raw["structured_source"]
    contract = FinalActionSourceContract(
        start=start,
        end=end,
        provider=str(structured["provider"]),
        query_year_type=str(structured["query_year_type"]),
        query_years=tuple(int(value) for value in structured["query_years"]),
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
        or set(contract.required_files)
        != {"corporate_actions.parquet", "security_events.parquet"}
        or contract.allowed_url_prefixes != OFFICIAL_EVIDENCE_URL_PREFIXES
        or contract.market_sources != OFFICIAL_MARKET_SOURCES
        or contract.supported_markets != ("sh", "sz")
    ):
        raise ValueError("invalid frozen final execution source contract")
    return contract


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
    write_json(destination, payload)
    return payload


def verify_execution_input_manifest(
    path: Path,
    authorization: FinalTestAuthorization,
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
    return resolved


def _assert_authorization(authorization: FinalTestAuthorization) -> None:
    if not isinstance(authorization, FinalTestAuthorization):
        raise TypeError("authorization must be a FinalTestAuthorization")
    if authorization.test_period != (FINAL_TEST_START, FINAL_TEST_END):
        raise ValueError("authorization period differs from final execution contract")
