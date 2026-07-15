from datetime import date
from pathlib import Path

import pytest

from ashare_multifactor.validation.corporate_action_source import (
    collect_baostock_rows,
    load_official_payment_date_overrides,
    load_official_security_events,
    merge_baostock_caches,
    write_baostock_cache,
)
from ashare_multifactor.audit.records import sha256_file


FIELDS = [
    "code",
    "dividPreNoticeDate",
    "dividAgmPumDate",
    "dividPlanAnnounceDate",
    "dividPlanDate",
    "dividRegistDate",
    "dividOperateDate",
    "dividPayDate",
    "dividStockMarketDate",
    "dividCashPsBeforeTax",
    "dividCashPsAfterTax",
    "dividStocksPs",
    "dividCashStock",
    "dividReserveToStockPs",
]


def test_collect_baostock_rows_queries_only_validation_operate_years() -> None:
    calls: list[tuple[str, int, str]] = []

    def query(code: str, year: int, year_type: str):
        calls.append((code, year, year_type))
        row = [""] * len(FIELDS)
        row[FIELDS.index("code")] = code
        row[FIELDS.index("dividOperateDate")] = f"{year}-06-01"
        return FIELDS, [row]

    result = collect_baostock_rows(["000001"], query)

    assert calls == [("sz.000001", year, "operate") for year in range(2017, 2022)]
    assert result.get_column("query_year").to_list() == list(range(2017, 2022))
    assert result.get_column("query_year_type").unique().to_list() == ["operate"]


def test_collect_baostock_rows_rejects_post_2021_response() -> None:
    def query(code: str, year: int, year_type: str):
        row = [""] * len(FIELDS)
        row[FIELDS.index("code")] = code
        row[FIELDS.index("dividOperateDate")] = f"{year}-12-31"
        row[FIELDS.index("dividPayDate")] = "2022-01-04"
        return FIELDS, [row]

    with pytest.raises(ValueError, match="sealed final test"):
        collect_baostock_rows(["000001"], query)


def test_write_baostock_cache_records_complete_query_coverage(tmp_path: Path) -> None:
    def query(code: str, year: int, year_type: str):
        return FIELDS, []

    output = tmp_path / "baostock_dividends.parquet"
    write_baostock_cache(["000001", "600000"], output, query=query)

    assert output.is_file()
    metadata = output.with_suffix(".metadata.json").read_text(encoding="utf-8")
    assert '"symbol_count": 2' in metadata
    assert '"query_years": [\n    2017,' in metadata
    assert '"query_year_type": "operate"' in metadata


def test_merge_baostock_caches_requires_exact_symbol_coverage(tmp_path: Path) -> None:
    def query(code: str, year: int, year_type: str):
        return FIELDS, []

    first = tmp_path / "first.parquet"
    second = tmp_path / "second.parquet"
    write_baostock_cache(["000001"], first, query=query)
    write_baostock_cache(["600000"], second, query=query)

    output = tmp_path / "combined.parquet"
    merge_baostock_caches([first, second], output, symbols=["000001", "600000"])
    assert output.is_file()

    with pytest.raises(ValueError, match="coverage"):
        merge_baostock_caches([first], output, symbols=["000001", "600000"])


def test_official_payment_date_override_requires_hashed_bounded_evidence(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    pdf = cache / "000819_notice.pdf"
    pdf.write_bytes(b"official bounded notice")
    evidence = tmp_path / "evidence.csv"
    evidence.write_text(
        "symbol,ex_date,payment_date,source_url,cache_file,sha256\n"
        f"000819,2021-06-25,2021-06-25,"
        "https://static.cninfo.com.cn/finalpage/2021-06-18/notice.PDF,"
        f"{pdf.name},{sha256_file(pdf)}\n",
        encoding="utf-8",
    )

    result = load_official_payment_date_overrides(evidence, cache)

    assert result.row(0, named=True) == {
        "symbol": "000819",
        "ex_date": date(2021, 6, 25),
        "payment_date": date(2021, 6, 25),
        "source_url": (
            "https://static.cninfo.com.cn/finalpage/2021-06-18/notice.PDF"
        ),
    }


def test_official_payment_date_override_rejects_final_test_date(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    pdf = cache / "notice.pdf"
    pdf.write_bytes(b"notice")
    evidence = tmp_path / "evidence.csv"
    evidence.write_text(
        "symbol,ex_date,payment_date,source_url,cache_file,sha256\n"
        "000819,2021-06-25,2022-01-04,https://static.cninfo.com.cn/a.pdf,"
        f"{pdf.name},{sha256_file(pdf)}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="sealed final test"):
        load_official_payment_date_overrides(evidence, cache)


def test_official_security_events_verify_source_and_event_contract(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    pdf = cache / "merger.pdf"
    pdf.write_bytes(b"official merger")
    evidence = tmp_path / "events.csv"
    evidence.write_text(
        "effective_date,source_symbol,event_type,target_symbol,ratio,"
        "cash_per_share,source_url,cache_file,sha256\n"
        "2017-12-25,000916,stock_merger,001965,0.6956,0.0,"
        "https://static.cninfo.com.cn/finalpage/2017-12-21/notice.PDF,"
        f"{pdf.name},{sha256_file(pdf)}\n",
        encoding="utf-8",
    )

    result = load_official_security_events(evidence, cache)

    assert result.item(0, "source_symbol") == "000916"
    assert result.item(0, "target_symbol") == "001965"
    assert result.item(0, "ratio") == pytest.approx(0.6956)
