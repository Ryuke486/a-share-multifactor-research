"""Shared colors and axis styling for the supplement figures.

Colors come from the validated default palette: categorical slots 1-3 pass the
all-pairs colour-vision checks, so at most three series share an axis; the
diverging pair is blue <-> red with a neutral gray midpoint.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
from matplotlib.axes import Axes  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402


SURFACE = "#fcfcfb"
PRIMARY_INK = "#0b0b0b"
SECONDARY_INK = "#52514e"
MUTED_INK = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
SERIES = ("#2a78d6", "#eb6834", "#1baf7a")
DIVERGING = ("#2a78d6", "#f0efec", "#e34948")


def style_axis(axis: Axes, *, grid_axis: str = "y") -> None:
    axis.set_facecolor(SURFACE)
    if grid_axis:
        axis.grid(True, axis=grid_axis, color=GRID, linewidth=0.6)
        axis.set_axisbelow(True)
    axis.tick_params(colors=MUTED_INK, labelsize=8)
    for spine in ("top", "right"):
        axis.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        axis.spines[spine].set_color(GRID)


def title(axis: Axes, text: str) -> None:
    axis.set_title(text, fontsize=10, color=PRIMARY_INK, loc="left")


def save(figure: Figure, path) -> None:
    figure.patch.set_facecolor(SURFACE)
    figure.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path, dpi=140, facecolor=SURFACE, metadata={"Software": None})
