"""Frozen, recall-first routing for the complete official announcement catalog."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path

import polars as pl

from ashare_multifactor.audit.secure_tree import (
    read_frozen_tree_at,
    write_frozen_tree_at,
)
from ashare_multifactor.final_test.official_announcement_catalog import (
    VerifiedAnnouncementCatalog,
    load_verified_announcement_catalog,
)
from ashare_multifactor.final_test.official_query_collection_root import (
    opened_safe_directory,
)
from ashare_multifactor.final_test.official_query_coverage import canonical_json_bytes


ROUTING_DIRECTORY = "official_announcement_routing"
ROUTING_NAME = "routing.parquet"
ROUTING_MANIFEST_NAME = "routing_manifest.json"

_SCHEMA_VERSION = "1"
_RULE_VERSION = "stage9-shared-routing-v4"
_CORPORATE_ACTION_CANDIDATE_RULES = (
    ("corporate_action", "权益分派"),
    ("corporate_action", "分红派息"),
    ("corporate_action", "除权除息"),
    ("corporate_action", "分红实施"),
    ("corporate_action", "分配方案实施"),
    ("corporate_action", "利润分配实施"),
    ("corporate_action", "现金红利"),
    ("corporate_action", "股息红利"),
    ("corporate_action", "转增股本实施"),
    ("corporate_action", "送股实施"),
)
_MERGER_KEYWORDS = ("吸收合并", "换股合并", "合并换股")
_MERGER_IMPLEMENTATION_KEYWORDS = ("实施", "结果", "完成", "新增股份上市")
_FINAL_TERMINATION_KEYWORDS = ("股票终止上市", "股票摘牌")
_PRE_EVENT_WARNING_KEYWORDS = (
    "可能",
    "风险",
    "申请",
    "监管工作函",
    "整理期",
    "后续",
    "主办券商",
    "去向安排",
)
_CONTEXT_EXCLUSIONS = (
    ("convertible_bond_conversion_pause", ("停止转股",)),
    ("convertible_bond_price_adjustment", ("可转债", "转股价格")),
    ("convertible_bond_price_adjustment", ("转债", "转股价格")),
    ("non_security_merger", ("合并报表",)),
    ("non_security_merger", ("合并财务",)),
    ("non_security_merger", ("合并资产负债表",)),
    ("non_security_merger", ("合并利润表",)),
    ("non_security_merger", ("同一控制下", "合并")),
    ("non_security_merger", ("子公司之间", "吸收合并")),
    ("non_security_merger", ("全资子公司", "吸收合并")),
    ("equity_incentive_cancellation", ("回购注销",)),
    ("equity_incentive_cancellation", ("限制性股票", "注销")),
    ("equity_incentive_cancellation", ("股票期权", "注销")),
    ("fund_account_cancellation", ("募集资金", "注销")),
    ("fund_account_cancellation", ("专户", "注销")),
    ("fund_account_cancellation", ("账户", "注销")),
    ("subsidiary_cancellation", ("子公司", "注销")),
    ("subsidiary_cancellation", ("孙公司", "注销")),
    ("debt_instrument_delisting", ("债券", "摘牌")),
    ("debt_instrument_delisting", ("转债", "摘牌")),
    ("asset_transaction_listing", ("公开摘牌",)),
    ("asset_transaction_listing", ("协议摘牌",)),
    ("pre_event_warning", ("退市风险警示",)),
    ("pre_event_warning", ("可能终止上市",)),
    ("pre_event_warning", ("可能将被终止上市",)),
    ("pre_event_warning", ("可能被终止上市",)),
    ("pre_event_warning", ("连续停牌直至终止上市",)),
    ("pre_event_warning", ("退市整理期",)),
    ("pre_event_warning", ("终止上市", "风险提示")),
    ("pre_event_warning", ("退市", "风险提示")),
)
_AMBIGUOUS_SECURITY_KEYWORDS = (
    "吸收合并",
    "换股合并",
    "合并换股",
    "终止上市",
    "股票摘牌",
)
_ROUTING_SCHEMA = {
    "catalog_id": pl.String,
    "route": pl.String,
    "candidate_type": pl.String,
    "reason": pl.String,
    "rule_version": pl.String,
}


@dataclass(frozen=True)
class VerifiedAnnouncementRouting:
    """Complete immutable routing rows bound to one announcement catalog."""

    path: Path
    frame: pl.DataFrame
    routing_sha256: str
    manifest: dict[str, object]


def build_announcement_routing(
    catalog_path: Path,
    *,
    destination: Path,
) -> VerifiedAnnouncementRouting:
    """Route every catalog row without discarding excluded announcement evidence."""
    catalog = load_verified_announcement_catalog(catalog_path)
    if catalog.root.parent.absolute() != destination.absolute():
        raise ValueError("official announcement routing destination differs from catalog")
    frame = announcement_routing_frame(catalog.frame)
    routing_bytes = _parquet_bytes(frame)
    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "role": "official_announcement_routing",
        "rule_version": _RULE_VERSION,
        "rules_sha256": _rules_sha256(),
        "catalog_sha256": catalog.catalog_sha256,
        "routing": {
            "path": ROUTING_NAME,
            "sha256": hashlib.sha256(routing_bytes).hexdigest(),
            "size_bytes": len(routing_bytes),
            "row_count": frame.height,
        },
        "counts": {
            route: frame.filter(pl.col("route") == route).height
            for route in ("candidate", "uncertain", "excluded")
        },
    }
    with opened_safe_directory(destination, label="official evidence destination") as root_fd:
        write_frozen_tree_at(
            root_fd,
            ROUTING_DIRECTORY,
            {
                ROUTING_NAME: routing_bytes,
                ROUTING_MANIFEST_NAME: canonical_json_bytes(manifest),
            },
            resumable=True,
            label="official announcement routing",
        )
    return load_verified_announcement_routing(
        destination / ROUTING_DIRECTORY / ROUTING_NAME,
        catalog=catalog,
    )


def load_verified_announcement_routing(
    path: Path,
    *,
    catalog: VerifiedAnnouncementCatalog,
) -> VerifiedAnnouncementRouting:
    root = path.parent.absolute()
    if (
        path.name != ROUTING_NAME
        or root.name != ROUTING_DIRECTORY
        or root.is_symlink()
        or not root.is_dir()
    ):
        raise ValueError("official announcement routing path is invalid")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        files = read_frozen_tree_at(descriptor, label="official announcement routing")
    finally:
        os.close(descriptor)
    if set(files) != {ROUTING_NAME, ROUTING_MANIFEST_NAME}:
        raise ValueError("official announcement routing inventory is invalid")
    manifest = _canonical_object(files[ROUTING_MANIFEST_NAME])
    routing_bytes = files[ROUTING_NAME]
    record = manifest.get("routing")
    if (
        manifest.get("schema_version") != _SCHEMA_VERSION
        or manifest.get("role") != "official_announcement_routing"
        or manifest.get("rule_version") != _RULE_VERSION
        or manifest.get("rules_sha256") != _rules_sha256()
        or manifest.get("catalog_sha256") != catalog.catalog_sha256
        or not isinstance(record, dict)
        or record.get("path") != ROUTING_NAME
        or record.get("sha256") != hashlib.sha256(routing_bytes).hexdigest()
        or record.get("size_bytes") != len(routing_bytes)
    ):
        raise ValueError("official announcement routing identity differs")
    try:
        frame = pl.read_parquet(BytesIO(routing_bytes))
    except pl.exceptions.PolarsError as error:
        raise ValueError("official announcement routing is invalid") from error
    if (
        frame.schema != _ROUTING_SCHEMA
        or frame.height != record.get("row_count")
        or frame.height != catalog.frame.height
        or frame.get_column("catalog_id").to_list()
        != catalog.frame.sort("catalog_id").get_column("catalog_id").to_list()
        or frame.select(pl.col("catalog_id").is_duplicated().any()).item()
        or frame.filter(
            ~pl.col("route").is_in(["candidate", "uncertain", "excluded"])
            | (pl.col("reason") == "")
            | (pl.col("rule_version") != _RULE_VERSION)
        ).height
    ):
        raise ValueError("official announcement routing rows differ from catalog")
    counts = manifest.get("counts")
    expected_counts = {
        route: frame.filter(pl.col("route") == route).height
        for route in ("candidate", "uncertain", "excluded")
    }
    if counts != expected_counts:
        raise ValueError("official announcement routing counts differ")
    return VerifiedAnnouncementRouting(
        path=root / ROUTING_NAME,
        frame=frame,
        routing_sha256=hashlib.sha256(routing_bytes).hexdigest(),
        manifest=manifest,
    )


def announcement_routing_frame(catalog: pl.DataFrame) -> pl.DataFrame:
    """Route a complete catalog and suppress duplicate ambiguous review documents."""
    required = {"catalog_id", "symbol", "announcement_title"}
    if (
        not isinstance(catalog, pl.DataFrame)
        or not required <= set(catalog.columns)
        or catalog.get_column("catalog_id").n_unique() != catalog.height
    ):
        raise ValueError("official announcement routing input is invalid")
    provisional = []
    for row in catalog.iter_rows(named=True):
        catalog_id = row.get("catalog_id")
        symbol = row.get("symbol")
        title = row.get("announcement_title")
        if not all(isinstance(value, str) and value for value in (catalog_id, symbol, title)):
            raise ValueError("official announcement routing input is invalid")
        provisional.append(
            {
                "catalog_id": catalog_id,
                "symbol": symbol,
                **route_announcement_title(title),
            }
        )
    stronger_groups = {
        (row["symbol"], row["candidate_type"])
        for row in provisional
        if row["route"] == "candidate" and row["candidate_type"]
    }
    rows = []
    for row in provisional:
        route = row["route"]
        candidate_type = row["candidate_type"]
        reason = row["reason"]
        if (
            route == "uncertain"
            and candidate_type
            and (row["symbol"], candidate_type) in stronger_groups
        ):
            route = "excluded"
            reason = f"stronger_candidate_available:{candidate_type}"
            candidate_type = ""
        rows.append(
            {
                "catalog_id": row["catalog_id"],
                "route": route,
                "candidate_type": candidate_type,
                "reason": reason,
                "rule_version": _RULE_VERSION,
            }
        )
    return pl.DataFrame(rows, schema=_ROUTING_SCHEMA).sort("catalog_id")


def route_announcement_title(title: str) -> dict[str, str]:
    """Apply the frozen recall-first title rule without creating event facts."""
    if not isinstance(title, str):
        raise TypeError("official announcement title is invalid")
    for reason, required_keywords in _CONTEXT_EXCLUSIONS:
        if all(keyword in title for keyword in required_keywords):
            return {
                "route": "excluded",
                "candidate_type": "",
                "reason": reason,
            }
    for candidate_type, keyword in _CORPORATE_ACTION_CANDIDATE_RULES:
        if keyword in title:
            return {
                "route": "candidate",
                "candidate_type": candidate_type,
                "reason": f"{candidate_type}_keyword:{keyword}",
            }
    merger_keyword = next(
        (keyword for keyword in _MERGER_KEYWORDS if keyword in title),
        None,
    )
    if merger_keyword is not None and any(
        keyword in title for keyword in _MERGER_IMPLEMENTATION_KEYWORDS
    ):
        return {
            "route": "candidate",
            "candidate_type": "stock_merger",
            "reason": f"stock_merger_implementation_keyword:{merger_keyword}",
        }
    termination_keyword = next(
        (keyword for keyword in _FINAL_TERMINATION_KEYWORDS if keyword in title),
        None,
    )
    if termination_keyword is not None and not any(
        keyword in title for keyword in _PRE_EVENT_WARNING_KEYWORDS
    ):
        return {
            "route": "candidate",
            "candidate_type": "security_event",
            "reason": f"final_security_event_keyword:{termination_keyword}",
        }
    ambiguous_keyword = next(
        (keyword for keyword in _AMBIGUOUS_SECURITY_KEYWORDS if keyword in title),
        None,
    )
    if ambiguous_keyword is not None:
        candidate_type = (
            "stock_merger"
            if merger_keyword is not None
            else "security_event"
        )
        return {
            "route": "uncertain",
            "candidate_type": candidate_type,
            "reason": f"ambiguous_security_event_keyword:{ambiguous_keyword}",
        }
    return {
        "route": "excluded",
        "candidate_type": "",
        "reason": "no_supported_event_term",
    }


def _rules_sha256() -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "rule_version": _RULE_VERSION,
                "corporate_action_candidate_rules": (
                    _CORPORATE_ACTION_CANDIDATE_RULES
                ),
                "merger_keywords": _MERGER_KEYWORDS,
                "merger_implementation_keywords": (
                    _MERGER_IMPLEMENTATION_KEYWORDS
                ),
                "final_termination_keywords": _FINAL_TERMINATION_KEYWORDS,
                "pre_event_warning_keywords": _PRE_EVENT_WARNING_KEYWORDS,
                "context_exclusions": _CONTEXT_EXCLUSIONS,
                "ambiguous_security_keywords": _AMBIGUOUS_SECURITY_KEYWORDS,
                "default": "excluded:no_supported_event_term",
            }
        )
    ).hexdigest()


def _parquet_bytes(frame: pl.DataFrame) -> bytes:
    stream = BytesIO()
    frame.write_parquet(stream, compression="zstd")
    return stream.getvalue()


def _canonical_object(raw: bytes) -> dict[str, object]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("official announcement routing manifest is invalid") from error
    if not isinstance(payload, dict) or raw != canonical_json_bytes(payload):
        raise ValueError("official announcement routing manifest is invalid")
    return payload
