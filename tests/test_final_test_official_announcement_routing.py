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


@pytest.mark.parametrize(
    "title",
    [
        "安科瑞2021年度权益分配实施公告",
        "2021年年度权益派息实施公告",
        "2021年年度A股股息分派实施公告",
        "中国移动有限公司2021年末期利润分派A股实施公告",
        "中国神华2021年度末期A股红利分派实施公告",
        "泛微网络2022年年度权益实施分派公告",
        "2025年中期分红A股实施公告",
        "2025年中期现金分红的实施公告",
        "2021年度利润分配预案实施公告",
        "亚翔集成-2025年度中期权益分配实施公告",
        "关于2018年年度权益分派实施的更正公告",
        "2024年度送股实施公告",
        "资本公积金转增股本实施公告",
    ],
)
def test_strong_corporate_action_implementation_variants_route_for_review(
    title: str,
) -> None:
    from ashare_multifactor.final_test.official_announcement_routing import (
        route_announcement_title,
    )

    actual = route_announcement_title(title)

    assert actual["route"] == "candidate"
    assert actual["candidate_type"] == "corporate_action"
    assert actual["reason"].startswith(
        "corporate_action_strong_implementation:"
    )


@pytest.mark.parametrize(
    "title",
    [
        "关于2024年度优先股股息派发实施方案的公告",
        "关于2024年度可转债付息实施公告",
        "关于2024年度利润分配预案的公告",
        "2024年第三季度报告",
    ],
)
def test_strong_corporate_action_routing_rejects_context_false_positives(
    title: str,
) -> None:
    from ashare_multifactor.final_test.official_announcement_routing import (
        route_announcement_title,
    )

    actual = route_announcement_title(title)

    assert (actual["route"], actual["candidate_type"]) != (
        "candidate",
        "corporate_action",
    )


@pytest.mark.parametrize(
    "title",
    [
        "2024年度权益分派实施后调整股票期权行权价格及数量的公告",
        "2024年度权益分派实施后调整限制性股票回购价格的公告",
        "关于因2024年度权益分派实施调整股权激励授予价格的公告",
        "关于因2024年度权益分派实施调整员工持股计划购买价格的公告",
    ],
)
def test_distribution_related_incentive_parameter_adjustments_are_excluded(
    title: str,
) -> None:
    from ashare_multifactor.final_test.official_announcement_routing import (
        route_announcement_title,
    )

    assert route_announcement_title(title) == {
        "route": "excluded",
        "candidate_type": "",
        "reason": (
            "distribution_related_equity_incentive_parameter_adjustment"
        ),
    }
