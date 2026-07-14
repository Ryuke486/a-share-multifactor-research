from datetime import date

import polars as pl
import pytest

from ashare_multifactor.research.corporate_action_sources import (
    normalize_cninfo_dividend,
    normalize_fhps_em,
)


def _provider_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "代码": ["000001", "600012", "000002"],
            "名称": ["平安银行", "皖通高速", "万科A"],
            "送转股份-送转总比例": [1.0, 0.0, 0.0],
            "送转股份-送转比例": [0.0, 0.0, 0.0],
            "送转股份-转股比例": [1.0, 0.0, 0.0],
            "现金分红-现金分红比例": [2.0, 1.0, 0.0],
            "股权登记日": [date(2005, 6, 1), date(2005, 7, 14), None],
            "除权除息日": [date(2005, 6, 2), date(2005, 7, 15), None],
            "方案进度": ["实施分配", "实施分配", "董事会决议通过"],
            "最新公告日期": [date(2005, 5, 25), date(2005, 7, 11), date(2005, 4, 1)],
        }
    )


def test_normalize_fhps_em_keeps_only_effective_actions() -> None:
    result = normalize_fhps_em(_provider_frame(), report_period="20041231")

    assert result.select("symbol").to_series().to_list() == ["000001", "600012"]
    assert result.select("effective_date").to_series().to_list() == [
        date(2005, 6, 2),
        date(2005, 7, 15),
    ]
    assert result.select("cash_per_share").to_series().to_list() == [0.2, 0.1]
    assert result.select("share_ratio").to_series().to_list() == [0.1, 0.0]
    assert result.select("source").unique().item() == "eastmoney_fhps_via_akshare"


def test_normalize_fhps_em_rejects_missing_required_columns() -> None:
    with pytest.raises(ValueError, match="missing provider columns"):
        normalize_fhps_em(_provider_frame().drop("除权除息日"), report_period="20041231")


def test_normalize_fhps_em_rejects_post_research_effective_date() -> None:
    frame = _provider_frame().with_columns(
        pl.when(pl.col("代码") == "000001")
        .then(pl.lit(date(2017, 1, 3)))
        .otherwise(pl.col("除权除息日"))
        .alias("除权除息日")
    )

    with pytest.raises(ValueError, match="outside research period"):
        normalize_fhps_em(frame, report_period="20161231")


def test_normalize_fhps_em_can_filter_provider_batch_to_research_period() -> None:
    frame = _provider_frame().with_columns(
        pl.when(pl.col("代码") == "000001")
        .then(pl.lit(date(2017, 1, 3)))
        .otherwise(pl.col("除权除息日"))
        .alias("除权除息日")
    )

    result = normalize_fhps_em(
        frame,
        report_period="20160630",
        filter_to_research_period=True,
    )

    assert result.select("symbol").to_series().to_list() == ["600012"]


def test_normalize_cninfo_uses_payment_and_share_credit_dates() -> None:
    frame = pl.DataFrame(
        {
            "实施方案公告日期": [date(2010, 5, 20)],
            "分红类型": ["年度分红"],
            "送股比例": [2.0],
            "转增比例": [1.0],
            "派息比例": [1.5],
            "股权登记日": [date(2010, 6, 1)],
            "除权日": [date(2010, 6, 2)],
            "派息日": [date(2010, 6, 8)],
            "股份到账日": ["2010-06-03"],
            "实施方案分红说明": ["10送2转1派1.5元"],
            "报告时间": ["2009年报"],
        }
    )

    result = normalize_cninfo_dividend(frame, symbol="000001")

    assert result.select("event_type").to_series().to_list() == [
        "share_distribution",
        "cash_dividend",
    ]
    assert result.select("effective_date").to_series().to_list() == [
        date(2010, 6, 3),
        date(2010, 6, 8),
    ]
    assert result.filter(pl.col("event_type") == "share_distribution").item(
        0, "share_ratio"
    ) == pytest.approx(0.3)
    assert result.filter(pl.col("event_type") == "cash_dividend").item(
        0, "cash_per_share"
    ) == 0.15


def test_normalize_cninfo_rejects_cash_without_payment_date() -> None:
    frame = pl.DataFrame(
        {
            "实施方案公告日期": [date(2010, 5, 20)],
            "分红类型": ["年度分红"],
            "送股比例": [0.0],
            "转增比例": [0.0],
            "派息比例": [1.5],
            "股权登记日": [date(2010, 6, 1)],
            "除权日": [date(2010, 6, 2)],
            "派息日": [None],
            "股份到账日": [None],
            "实施方案分红说明": ["10派1.5元"],
            "报告时间": ["2009年报"],
        }
    )

    with pytest.raises(ValueError, match="cash dividend missing payment date"):
        normalize_cninfo_dividend(frame, symbol="000001")
