from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import polars as pl
import pytest

from ashare_multifactor.final_test.action_source_contract import (
    load_action_source_contract,
)
from ashare_multifactor.final_test.official_query_client import RetryPolicy


class RehearsalTransport:
    def __init__(self) -> None:
        self.query_periods: list[str] = []

    def fetch(
        self,
        endpoint: str,
        form: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> bytes:
        del timeout_seconds
        if endpoint.endswith("/information/topSearch/query"):
            symbol = form["keyWord"]
            return json.dumps(
                [{"code": symbol, "orgId": f"fixture-{symbol}"}],
                separators=(",", ":"),
            ).encode()
        self.query_periods.append(form["seDate"])
        symbol = form["stock"].split(",", maxsplit=1)[0]
        return json.dumps(
            {
                "totalpages": 1,
                "totalAnnouncement": 1,
                "announcements": [
                    {
                        "announcementId": f"announcement-{symbol}",
                        "announcementTitle": "2020年度权益分派实施公告",
                        "announcementTime": 1593561600000,
                        "adjunctUrl": f"finalpage/2020-07-01/{symbol}.PDF",
                    }
                ],
            },
            separators=(",", ":"),
        ).encode()


class InterruptedRehearsalTransport(RehearsalTransport):
    def __init__(self, *, interrupt_on_query: int) -> None:
        super().__init__()
        self.interrupt_on_query = interrupt_on_query
        self.query_attempts = 0

    def fetch(
        self,
        endpoint: str,
        form: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> bytes:
        if not endpoint.endswith("/information/topSearch/query"):
            self.query_attempts += 1
            if self.query_attempts == self.interrupt_on_query:
                raise KeyboardInterrupt("injected rehearsal interruption")
        return super().fetch(
            endpoint,
            form,
            timeout_seconds=timeout_seconds,
        )


def _panel() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [date(2021, 12, 31)] * 16,
            "symbol": [
                *(f"6000{index:02d}" for index in range(8)),
                *(f"0000{index:02d}" for index in range(8)),
            ],
            "total_market_cap": [float(index + 1) for index in range(16)],
        }
    )


def test_validation_rehearsal_reuses_production_slicing_and_stays_pre_2022(
    tmp_path: Path,
) -> None:
    from ashare_multifactor.final_test.official_rehearsal import (
        run_official_collection_rehearsal,
    )

    transport = RehearsalTransport()
    result = run_official_collection_rehearsal(
        panel=_panel(),
        output_root=tmp_path / "rehearsal",
        contract=load_action_source_contract(
            Path(__file__).parents[1] / "configs/final_execution_sources.yaml"
        ),
        transport=transport,
        policy=RetryPolicy(
            attempts=1,
            timeout_seconds=0.1,
            minimum_interval_seconds=0,
        ),
        sample_size=8,
        required_symbols=("600000", "000001"),
        start=date(2017, 1, 1),
        end=date(2021, 12, 31),
        projected_universe_size=5351,
        implementation_sha256="a" * 64,
    )

    report = json.loads(result.report_path.read_bytes())
    assert result.sample_path.is_file()
    assert result.index_path.is_file()
    assert result.catalog_path.is_file()
    assert result.routing_path.is_file()
    assert report["sample_count"] == 8
    assert report["announcement_count"] == 8
    assert report["routing_counts"]["candidate"] == 8
    assert report["projected_document_count"] == 5351
    assert (
        report["full_universe_eta_seconds"]
        >= report["query_full_universe_eta_seconds"]
    )
    assert report["document_request_lower_bound_seconds"] == 5351
    assert report["implementation_sha256"] == "a" * 64
    assert set(transport.query_periods) == {"2017-01-01~2021-12-31"}


def test_validation_rehearsal_persists_cumulative_metrics_across_interruption(
    tmp_path: Path,
) -> None:
    from ashare_multifactor.final_test.official_rehearsal import (
        run_official_collection_rehearsal,
    )

    arguments = {
        "panel": _panel(),
        "output_root": tmp_path / "rehearsal",
        "contract": load_action_source_contract(
            Path(__file__).parents[1] / "configs/final_execution_sources.yaml"
        ),
        "policy": RetryPolicy(
            attempts=1,
            timeout_seconds=0.1,
            minimum_interval_seconds=0,
        ),
        "sample_size": 8,
        "required_symbols": ("600000", "000001"),
        "start": date(2017, 1, 1),
        "end": date(2021, 12, 31),
        "projected_universe_size": 5351,
        "implementation_sha256": "b" * 64,
    }
    with pytest.raises(KeyboardInterrupt, match="injected"):
        run_official_collection_rehearsal(
            **arguments,
            transport=InterruptedRehearsalTransport(interrupt_on_query=3),
        )

    result = run_official_collection_rehearsal(
        **arguments,
        transport=RehearsalTransport(),
    )
    report = json.loads(result.report_path.read_bytes())

    assert report["restart_count"] == 1
    assert report["request_count"] == 8 + 3 + 6
