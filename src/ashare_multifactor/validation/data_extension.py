from __future__ import annotations

from datetime import date

from ashare_multifactor.config import ResearchConfig
from ashare_multifactor.data.build import BuildManifest, build_parquet_dataset
from ashare_multifactor.validation.protocol import assert_validation_read_allowed


def build_validation_daily_panel(
    config: ResearchConfig,
    start: date,
    end: date,
) -> BuildManifest:
    """Build the isolated validation panel through the stage-two reader and gates."""
    assert_validation_read_allowed(start, end)
    if start < config.validation.start or end > config.validation.end:
        raise ValueError("validation daily panel must stay inside validation period")
    output = config.paths.processed / "validation_evaluation/daily_panel"
    return build_parquet_dataset(config, start, end, output_root=output)
