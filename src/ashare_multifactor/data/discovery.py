import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_金玥数据\.csv$")


@dataclass(frozen=True)
class DailyFilePair:
    trading_date: date
    unadjusted: Path
    backward_adjusted: Path


def _index_files(root: Path, start: date, end: date) -> dict[date, Path]:
    indexed = {}
    for path in root.rglob("*.csv"):
        match = DATE_RE.match(path.name)
        if match is None:
            continue
        trading_date = date.fromisoformat(match.group(1))
        if start <= trading_date <= end:
            if trading_date in indexed:
                raise ValueError(f"duplicate file for {trading_date}")
            indexed[trading_date] = path
    return indexed


def discover_daily_pairs(
    unadjusted_root: Path,
    backward_adjusted_root: Path,
    start: date,
    end: date,
) -> list[DailyFilePair]:
    raw = _index_files(unadjusted_root, start, end)
    adj = _index_files(backward_adjusted_root, start, end)
    missing = sorted(set(raw) ^ set(adj))
    if missing:
        raise ValueError(f"unpaired trading dates: {missing[:10]}")
    return [DailyFilePair(day, raw[day], adj[day]) for day in sorted(raw)]
