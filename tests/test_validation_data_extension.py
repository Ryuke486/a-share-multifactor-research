from datetime import date
import json
from pathlib import Path

import polars as pl
import pytest

from test_build import _config, _write_pair

from ashare_multifactor.validation.data_extension import build_validation_daily_panel


def test_validation_builder_reuses_canonical_reader_and_audited_manifest(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _write_pair(config, date(2017, 1, 3))
    _write_pair(config, date(2017, 1, 4))

    manifest = build_validation_daily_panel(
        config, date(2017, 1, 3), date(2017, 1, 4)
    )

    root = config.paths.processed / "validation_evaluation/daily_panel"
    panel = pl.read_parquet(root / "year=2017/part-000.parquet")
    saved = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest.rows == 2
    assert panel.select("date", "symbol").rows() == [
        (date(2017, 1, 3), "000001"),
        (date(2017, 1, 4), "000001"),
    ]
    assert panel.is_duplicated().sum() == 0
    assert saved["years"] == [2017]
    assert saved["quality_issues"]["records"] == 0


def test_validation_builder_blocks_final_test_before_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    called = False

    def forbidden(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True
        raise AssertionError("discovery must not run")

    monkeypatch.setattr("ashare_multifactor.data.build.discover_daily_pairs", forbidden)

    with pytest.raises(ValueError, match="final test period is sealed"):
        build_validation_daily_panel(
            config, date(2021, 12, 31), date(2022, 1, 4)
        )

    assert called is False
