from __future__ import annotations

import polars as pl
import pytest


@pytest.mark.parametrize(
    ("title", "route", "candidate_type"),
    [
        ("2020年度权益分派实施公告", "candidate", "corporate_action"),
        ("2019年年度分红派息实施公告", "candidate", "corporate_action"),
        ("利润分配及资本公积金转增股本实施公告", "candidate", "corporate_action"),
        ("关于本次合并换股实施结果暨新增股份上市公告", "candidate", "stock_merger"),
        ("招商公路换股吸收合并华北高速事项公告", "uncertain", "stock_merger"),
        ("关于公司股票终止上市的公告", "candidate", "security_event"),
        ("关于公司股票可能终止上市的风险提示公告", "excluded", ""),
        ("关于全资子公司之间吸收合并完成的公告", "excluded", ""),
        ("关于注销募集资金专户的公告", "excluded", ""),
        (
            "关于实施2022年度权益分派时浦发转债停止转股的提示性公告",
            "excluded",
            "",
        ),
        (
            "关于2023年度权益分派调整可转债转股价格的公告",
            "excluded",
            "",
        ),
        ("2021年第三季度报告", "excluded", ""),
        ("关于董事辞职的公告", "excluded", ""),
    ],
)
def test_frozen_routing_is_recall_first_for_known_event_title_families(
    title: str,
    route: str,
    candidate_type: str,
) -> None:
    from ashare_multifactor.final_test.official_announcement_routing import (
        route_announcement_title,
    )

    actual = route_announcement_title(title)

    assert actual["route"] == route
    assert actual["candidate_type"] == candidate_type


def test_frozen_routing_keeps_ambiguous_supported_security_terms_for_review() -> None:
    from ashare_multifactor.final_test.official_announcement_routing import (
        route_announcement_title,
    )

    actual = route_announcement_title("关于主动终止上市方案的公告")

    assert actual == {
        "route": "uncertain",
        "candidate_type": "security_event",
        "reason": "ambiguous_security_event_keyword:终止上市",
    }


def test_catalog_routing_does_not_download_ambiguous_duplicates_of_a_stronger_candidate() -> None:
    from ashare_multifactor.final_test.official_announcement_routing import (
        announcement_routing_frame,
    )

    catalog = pl.DataFrame(
        {
            "catalog_id": ["a", "b", "c"],
            "symbol": ["000916", "000916", "000001"],
            "announcement_title": [
                "关于本次换股吸收合并实施结果暨新增股份上市公告",
                "换股吸收合并报告书草案",
                "换股吸收合并报告书草案",
            ],
        }
    )

    routing = announcement_routing_frame(catalog).sort("catalog_id")

    assert routing.get_column("route").to_list() == [
        "candidate",
        "excluded",
        "uncertain",
    ]
    assert routing.row(1, named=True)["reason"] == (
        "stronger_candidate_available:stock_merger"
    )
