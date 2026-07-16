from datetime import date
import json
from pathlib import Path

import polars as pl
import pytest

from ashare_multifactor.validation.corporate_action_source import (
    collect_baostock_rows,
    load_official_action_corrections,
    load_official_payment_date_overrides,
    load_official_security_events,
    migrate_legacy_query_coverage,
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
    coverage = pl.read_parquet(output.with_suffix(".coverage.parquet"))
    assert coverage.shape == (10, 4)
    assert coverage.select(pl.col("status").unique()).item() == "ok"
    assert coverage.get_column("row_count").sum() == 0


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


def test_migrate_legacy_query_coverage_records_zero_event_responses(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "legacy.parquet"
    pl.DataFrame(
        {
            "code": ["sz.000001"],
            "query_year": [2017],
            "query_year_type": ["operate"],
        }
    ).write_parquet(cache)
    cache.with_suffix(".metadata.json").write_text(
        json.dumps(
            {
                "source": "baostock_query_dividend_data",
                "query_year_type": "operate",
                "query_years": list(range(2017, 2022)),
                "symbol_count": 1,
                "symbols": ["000001"],
                "row_count": 1,
                "maximum_allowed_date": "2021-12-31",
                "sha256": sha256_file(cache),
            }
        ),
        encoding="utf-8",
    )

    coverage = migrate_legacy_query_coverage(cache)

    assert coverage.height == 5
    assert coverage.filter(pl.col("year") == 2017).item(0, "row_count") == 1
    assert coverage.filter(pl.col("year") == 2018).item(0, "row_count") == 0


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


def test_official_action_corrections_require_hashed_bounded_evidence(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    snapshot = cache / "000042_notice.json"
    snapshot.write_text('{"source": "official implementation notice"}', encoding="utf-8")
    evidence = tmp_path / "corrections.csv"
    evidence.write_text(
        "symbol,source_ex_date,corrected_ex_date,payment_date,source_url,"
        "cache_file,sha256\n"
        "000042,2018-06-28,2018-06-22,2018-06-22,"
        "https://static.cninfo.com.cn/finalpage/2018-06-14/notice.PDF,"
        f"{snapshot.name},{sha256_file(snapshot)}\n",
        encoding="utf-8",
    )

    result = load_official_action_corrections(evidence, cache)

    assert result.row(0, named=True) == {
        "symbol": "000042",
        "source_ex_date": date(2018, 6, 28),
        "corrected_ex_date": date(2018, 6, 22),
        "payment_date": date(2018, 6, 22),
        "source_url": "https://static.cninfo.com.cn/finalpage/2018-06-14/notice.PDF",
    }


def test_repository_action_corrections_cover_exact_audit_anomalies() -> None:
    root = Path(__file__).parents[1]

    result = load_official_action_corrections(
        root / "configs/validation_corporate_action_corrections.csv",
        root / "configs/evidence/validation_action_corrections",
    )

    assert result.select(
        "symbol", "source_ex_date", "corrected_ex_date", "payment_date"
    ).rows() == [
        ("000042", date(2018, 6, 28), date(2018, 6, 22), date(2018, 6, 22)),
        ("002227", date(2018, 6, 1), date(2018, 6, 1), date(2018, 6, 1)),
        ("002335", date(2018, 5, 16), date(2018, 5, 16), date(2018, 5, 16)),
        ("002358", date(2019, 7, 5), date(2019, 7, 5), date(2019, 7, 5)),
    ]


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
