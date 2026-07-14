from datetime import date

import polars as pl

from ashare_multifactor.execution.stale_audit import audit_stale_positions


def _positions(symbol: str = "000001") -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [date(2010, 2, 10)],
            "symbol": [symbol],
            "market_value": [10_000.0],
            "is_stale": [True],
            "stale_days": [40],
        }
    )


def _nav() -> pl.DataFrame:
    return pl.DataFrame({"date": [date(2010, 2, 10)], "nav": [100_000.0]})


def _panel(with_resume: bool) -> pl.DataFrame:
    dates = [date(2010, 1, 1)]
    opens = [10.0]
    if with_resume:
        dates.append(date(2010, 2, 11))
        opens.append(11.0)
    return pl.DataFrame({"date": dates, "symbol": ["000001"] * len(dates), "open_raw": opens})


def _events() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "source_symbol": pl.String,
            "effective_date": pl.Date,
            "event_type": pl.String,
        }
    )


def _evidence() -> pl.DataFrame:
    return pl.DataFrame(schema={"symbol": pl.String, "source_url": pl.String})


def test_stale_interval_is_explained_when_security_resumes() -> None:
    summary, intervals = audit_stale_positions(
        _positions(), _nav(), _panel(True), _events(), _evidence(), review_days=30
    )
    assert summary["status"] == "ready"
    assert intervals.item(0, "classification") == "explained_resumption"


def test_stale_interval_without_resume_or_security_event_blocks_release() -> None:
    summary, intervals = audit_stale_positions(
        _positions(), _nav(), _panel(False), _events(), _evidence(), review_days=30
    )
    assert summary["status"] == "blocked"
    assert intervals.item(0, "classification") == "unexplained"


def test_stale_interval_is_explained_by_registered_security_event() -> None:
    events = pl.DataFrame(
        {
            "source_symbol": ["000001"],
            "effective_date": [date(2010, 2, 15)],
            "event_type": ["cash_exit"],
        }
    )
    summary, intervals = audit_stale_positions(
        _positions(), _nav(), _panel(False), events, _evidence(), review_days=30
    )
    assert summary["status"] == "ready"
    assert intervals.item(0, "classification") == "explained_security_event"


def test_period_end_stale_interval_accepts_bound_public_suspension_evidence() -> None:
    evidence = pl.DataFrame(
        {"symbol": ["000001"], "source_url": ["https://static.cninfo.com.cn/a.pdf"]}
    )
    summary, intervals = audit_stale_positions(
        _positions(), _nav(), _panel(False), _events(), evidence, review_days=30
    )
    assert summary["status"] == "ready"
    assert intervals.item(0, "classification") == "explained_public_evidence"
