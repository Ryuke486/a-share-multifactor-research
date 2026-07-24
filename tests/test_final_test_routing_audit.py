from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest


def _catalog_and_routing() -> tuple[pl.DataFrame, pl.DataFrame]:
    catalog = pl.DataFrame(
        {
            "catalog_id": ["a", "b", "c", "d"],
            "announcement_title": [
                "2020年度报告",
                "2020年第三季度报告",
                "2020年度权益分派实施公告",
                "关于董事辞职的公告",
            ],
            "source_url": [
                "https://static.cninfo.com.cn/finalpage/2020-01-01/a.PDF",
                "https://static.cninfo.com.cn/finalpage/2020-02-01/b.PDF",
                "https://static.cninfo.com.cn/finalpage/2020-03-01/c.PDF",
                "https://static.cninfo.com.cn/finalpage/2020-04-01/d.PDF",
            ],
            "symbol": ["600000", "000001", "600000", "000001"],
            "market": ["sh", "sz", "sh", "sz"],
        }
    )
    routing = pl.DataFrame(
        {
            "catalog_id": ["a", "b", "c", "d"],
            "route": ["excluded", "excluded", "candidate", "uncertain"],
            "candidate_type": ["", "", "corporate_action", ""],
            "reason": [
                "routine_periodic_report",
                "routine_periodic_report",
                "corporate_action_keyword:权益分派",
                "no_frozen_rule_match",
            ],
        }
    )
    return catalog, routing


def test_routing_audit_captures_known_titles_and_builds_human_exclusion_queue() -> None:
    from ashare_multifactor.final_test.routing_audit import audit_routing_frames

    catalog, routing = _catalog_and_routing()
    known = pl.DataFrame(
        {
            "case_id": ["dividend", "merger", "termination"],
            "title": [
                "2020年度权益分派实施公告",
                "换股吸收合并实施公告",
                "关于股票终止上市的公告",
            ],
            "expected_event_type": [
                "corporate_action",
                "stock_merger",
                "security_event",
            ],
        }
    )

    result = audit_routing_frames(
        catalog,
        routing,
        known_cases=known,
        exclusion_sample_size=2,
    )

    assert result.report["known_capture_rate"] == 1.0
    assert result.report["known_miss_count"] == 0
    assert result.report["exclusion_sample_count"] == 2
    assert result.report["exclusion_review_complete"] is False
    assert result.exclusion_queue.get_column("review_status").unique().to_list() == [
        "needs_human_review"
    ]


def test_routing_audit_rejects_a_known_event_excluded_by_the_frozen_rule() -> None:
    from ashare_multifactor.final_test.routing_audit import audit_routing_frames

    catalog, routing = _catalog_and_routing()
    known = pl.DataFrame(
        {
            "case_id": ["bad"],
            "title": ["2020年度报告"],
            "expected_event_type": ["corporate_action"],
        }
    )

    with pytest.raises(ValueError, match="known.*excluded|miss"):
        audit_routing_frames(
            catalog,
            routing,
            known_cases=known,
            exclusion_sample_size=2,
        )


def test_all_registered_pre_2022_known_titles_are_captured() -> None:
    from ashare_multifactor.final_test.official_announcement_routing import (
        route_announcement_title,
    )

    cases = pl.read_csv(
        Path(__file__).parents[1]
        / "configs/evidence/stage9_known_routing_cases.csv"
    )

    assert cases.height == 7
    assert all(
        route_announcement_title(title)["route"] != "excluded"
        for title in cases.get_column("title")
    )
