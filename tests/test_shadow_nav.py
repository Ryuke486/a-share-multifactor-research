from datetime import date

import polars as pl

from ashare_multifactor.execution.corporate_actions import normalize_corporate_actions
from ashare_multifactor.execution.shadow_nav import shadow_nav_audit


def _panel(adjusted_second: float) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [date(2010, 1, 4), date(2010, 1, 5)],
            "symbol": ["000001", "000001"],
            "close_raw": [10.0, 5.0],
            "close_adj": [10.0, adjusted_second],
        }
    )


def _positions() -> pl.DataFrame:
    return pl.DataFrame(
        {"date": [date(2010, 1, 5)], "symbol": ["000001"], "market_value": [10_000.0]}
    )


def _actions(share_ratio: float) -> pl.DataFrame:
    return normalize_corporate_actions(
        pl.DataFrame(
            {
                "symbol": ["000001"],
                "ex_date": [date(2010, 1, 5)],
                "effective_date": [date(2010, 1, 5)],
                "cash_per_share": [0.0],
                "share_ratio": [share_ratio],
                "source": ["fixture"],
            }
        )
    )


def test_shadow_nav_accepts_complete_share_event() -> None:
    summary, _ = shadow_nav_audit(_panel(10.0), _actions(1.0), _positions(), threshold=0.005)
    assert summary["status"] == "ready"


def test_shadow_nav_rejects_missing_share_event() -> None:
    summary, _ = shadow_nav_audit(_panel(10.0), _actions(0.0), _positions(), threshold=0.005)
    assert summary["status"] == "blocked"


def test_shadow_nav_applies_action_on_first_price_observation_after_suspension() -> None:
    panel = pl.DataFrame(
        {
            "date": [date(2010, 1, 4), date(2010, 1, 6)],
            "symbol": ["000001", "000001"],
            "close_raw": [10.0, 5.0],
            "close_adj": [10.0, 10.0],
        }
    )
    actions = normalize_corporate_actions(
        pl.DataFrame(
            {
                "symbol": ["000001"],
                "ex_date": [date(2010, 1, 5)],
                "effective_date": [date(2010, 1, 5)],
                "cash_per_share": [0.0],
                "share_ratio": [1.0],
                "source": ["fixture"],
            }
        )
    )
    positions = pl.DataFrame(
        {"date": [date(2010, 1, 6)], "symbol": ["000001"], "market_value": [10_000.0]}
    )

    summary, _ = shadow_nav_audit(panel, actions, positions, threshold=0.005)

    assert summary["status"] == "ready"
