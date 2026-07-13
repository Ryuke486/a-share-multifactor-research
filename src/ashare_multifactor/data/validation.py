from dataclasses import asdict, dataclass
from datetime import date

import polars as pl


@dataclass(frozen=True)
class QualityIssue:
    severity: str
    code: str
    count: int
    message: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


_PRICE_COLUMNS = (
    "open_raw",
    "high_raw",
    "low_raw",
    "close_raw",
    "prev_close_raw",
    "open_adj",
    "high_adj",
    "low_adj",
    "close_adj",
    "prev_close_adj",
)


def _row_count(frame: pl.DataFrame, condition: pl.Expr) -> int:
    return frame.select(condition.fill_null(False).sum()).item()


def validate_daily_panel(frame: pl.DataFrame, expected_date: date) -> list[QualityIssue]:
    issues: list[QualityIssue] = []

    duplicate_count = frame.select(pl.struct("date", "symbol").is_duplicated().sum()).item()
    if duplicate_count:
        issues.append(QualityIssue("error", "duplicate_key", duplicate_count, "duplicate (date, symbol) keys"))

    date_count = _row_count(frame, pl.col("date") != expected_date)
    if date_count:
        issues.append(QualityIssue("error", "date_mismatch", date_count, "date differs from expected file date"))

    missing_price_count = _row_count(frame, pl.any_horizontal(pl.col(column).is_null() for column in _PRICE_COLUMNS))
    if missing_price_count:
        issues.append(QualityIssue("error", "missing_price", missing_price_count, "raw or adjusted key price is missing"))

    invalid_ohlc = []
    for suffix in ("raw", "adj"):
        open_ = pl.col(f"open_{suffix}")
        high = pl.col(f"high_{suffix}")
        low = pl.col(f"low_{suffix}")
        close = pl.col(f"close_{suffix}")
        invalid_ohlc.extend(
            [
                pl.any_horizontal(open_ <= 0, high <= 0, low <= 0, close <= 0),
                high < pl.max_horizontal(open_, close),
                low > pl.min_horizontal(open_, close),
            ]
        )
    invalid_ohlc_count = _row_count(frame, pl.any_horizontal(invalid_ohlc))
    if invalid_ohlc_count:
        issues.append(QualityIssue("error", "invalid_ohlc", invalid_ohlc_count, "OHLC values are non-positive or inconsistent"))

    negative_count = _row_count(frame, pl.any_horizontal(pl.col("volume") < 0, pl.col("amount") < 0))
    if negative_count:
        issues.append(QualityIssue("error", "negative_volume_amount", negative_count, "volume or amount is negative"))

    warning_checks = (
        ("missing_industry", pl.col("industry").is_null(), "industry is missing"),
        ("missing_margin", pl.col("is_margin").is_null(), "margin eligibility is missing"),
        (
            "missing_valuation",
            pl.any_horizontal(pl.col("pe_ttm").is_null(), pl.col("pb").is_null(), pl.col("ps_ttm").is_null()),
            "one or more valuation fields are missing",
        ),
        (
            "zero_volume_amount",
            pl.any_horizontal(pl.col("volume") == 0, pl.col("amount") == 0),
            "volume or amount is zero",
        ),
    )
    for code, condition, message in warning_checks:
        count = _row_count(frame, condition)
        if count:
            issues.append(QualityIssue("warning", code, count, message))

    severity_order = {"error": 0, "warning": 1}
    return sorted(issues, key=lambda issue: (severity_order[issue.severity], issue.code))


def raise_on_errors(issues: list[QualityIssue]) -> None:
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        detail = "; ".join(f"{issue.code}:{issue.count}" for issue in errors)
        raise ValueError(f"data quality errors: {detail}")
