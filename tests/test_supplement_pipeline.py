from datetime import date

import polars as pl
import pytest

from ashare_multifactor.supplements.figure import _separated_labels
from ashare_multifactor.supplements.pipeline import _calendar_years, _period_rows

DAYS = [date(2017, 1, 3), date(2017, 1, 4), date(2017, 1, 5)]


def _nav() -> pl.DataFrame:
    return pl.DataFrame(
        {"date": DAYS, "nav": [101.0, 102.0, 104.0], "zero_cost_nav": [101.0, 103.0, 105.0]}
    )


def _index() -> pl.DataFrame:
    return pl.DataFrame(
        {"date": DAYS, "equal_weight": [1.0, 1.01, 1.02], "cap_weight": [1.0, 0.99, 1.0]}
    )


def test_period_rows_refuse_a_strategy_path_that_misses_its_published_return() -> None:
    kwargs = dict(start_levels=(100.0, 100.0, 1.0, 1.0), years=1.0)

    rows = _period_rows("validation", "c", _nav(), _index(), published_net=0.04, **kwargs)
    assert len(rows) == 4

    with pytest.raises(ValueError, match="does not reproduce its published return"):
        _period_rows("validation", "c", _nav(), _index(), published_net=0.05, **kwargs)


def test_period_rows_refuse_misaligned_benchmark_dates() -> None:
    shifted = _index().with_columns(pl.col("date").dt.offset_by("1d"))
    with pytest.raises(ValueError, match="misaligned"):
        _period_rows(
            "validation",
            "c",
            _nav(),
            shifted,
            start_levels=(100.0, 100.0, 1.0, 1.0),
            years=1.0,
            published_net=0.04,
        )


def test_calendar_years_chain_from_the_first_observation() -> None:
    nav = pl.DataFrame(
        {
            "date": [date(2016, 12, 29), date(2016, 12, 30), date(2017, 12, 29)],
            "nav": [100.0, 110.0, 99.0],
            "zero_cost_nav": [100.0, 110.0, 110.0],
        }
    )
    index = pl.DataFrame(
        {
            "date": nav["date"],
            "equal_weight": [1.0, 1.0, 1.2],
            "cap_weight": [1.0, 1.0, 1.0],
            "member_count": [0, 0, 1],
        }
    )

    years = _calendar_years(nav, index)

    assert years["strategy_net"].to_list() == pytest.approx([0.1, -0.1])
    assert years["equal_weight"].to_list() == pytest.approx([0.0, 0.2])
    assert years["net_minus_equal_weight"].to_list() == pytest.approx([0.1, -0.3])


def test_end_labels_are_pushed_apart_in_ratio_space() -> None:
    placed = _separated_labels([(5.0, "a"), (4.9, "b"), (14.0, "c")], minimum_ratio=1.1)

    assert [name for _, _, name in placed] == ["b", "a", "c"]
    assert placed[1][1] == pytest.approx(4.9 * 1.1)
    assert placed[2][1] == pytest.approx(14.0)
