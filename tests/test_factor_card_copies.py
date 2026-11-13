from __future__ import annotations

import hashlib
from pathlib import Path

from ashare_multifactor.factors.definitions import FACTOR_DEFINITIONS

CARDS = Path(__file__).resolve().parents[1] / "docs" / "factor_cards"


def test_tracked_factor_cards_are_the_unedited_copies_of_the_stage_four_artifacts() -> None:
    recorded = dict(
        reversed(line.split("  ", 1))
        for line in (CARDS / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    )

    assert sorted(recorded) == sorted(f"{item.name}.md" for item in FACTOR_DEFINITIONS)
    for name, digest in recorded.items():
        assert hashlib.sha256((CARDS / name).read_bytes()).hexdigest() == digest, name
