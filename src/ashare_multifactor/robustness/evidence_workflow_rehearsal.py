"""Compatibility facade for the final-test evidence-workflow rehearsal.

The rehearsal rehearses the deferred final-test evidence workflow, so it now
lives with that workflow:

    ashare_multifactor.final_test.evidence_workflow_rehearsal

This module keeps the historical import path resolvable without making the
v1.0 stage packages depend on the final-test orchestration layer: the target
module is resolved lazily, so importing ``ashare_multifactor.robustness``
never loads ``final_test``. Callers that explicitly ask for the rehearsal
still receive it.
"""

from __future__ import annotations

import importlib
from typing import Any


_REHEARSAL_MODULE = "ashare_multifactor.final_test.evidence_workflow_rehearsal"
_EXPORTED = ("build_evidence_workflow_rehearsal",)


def __getattr__(name: str) -> Any:
    if name in _EXPORTED:
        return getattr(importlib.import_module(_REHEARSAL_MODULE), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*globals(), *_EXPORTED})


__all__ = _EXPORTED
