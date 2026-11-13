"""Frozen sample-split boundaries of the research protocol.

These constants are deliberate code-level guards: leakage checks compare against
them directly, so editing a YAML file cannot relocate the research, validation or
sealed final-test windows. ``configs/research_protocol.yaml`` must agree with
them; ``tests/test_protocol_dates.py`` enforces that.
"""

from __future__ import annotations

from datetime import date
from typing import Final

FACTOR_WARMUP_START: Final = date(2003, 1, 1)
RESEARCH_START: Final = date(2005, 1, 1)
RESEARCH_END: Final = date(2016, 12, 31)
VALIDATION_START: Final = date(2017, 1, 1)
VALIDATION_END: Final = date(2021, 12, 31)
FINAL_TEST_START: Final = date(2022, 1, 1)
FINAL_TEST_END: Final = date(2025, 12, 31)
