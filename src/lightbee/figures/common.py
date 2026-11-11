"""The shared look of every figure, and the markings every export must carry.

Every figure carries its classification marking top and bottom, the test and
evaluation banner, and a footer naming the object, window, data source and
generation time, all inside the figure itself, so the PNG an analyst downloads
for Mattermost stands on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import numpy as np
import numpy.typing as npt

from lightbee import __version__

NAVY: Final = "#162646"
PLOT: Final = "#000000"
TEXT: Final = "#c8d3e0"
MUTED: Final = "#8a9bb3"
GRID: Final = "#24365a"
BLUE_2: Final = "#739BCF"
GREEN: Final = "#27AE60"
AMBER: Final = "#E0A040"
FONT: Final = "Segoe UI, Arial, sans-serif"
BANNER: Final = "UNDER TEST AND EVALUATION: NOT FOR OPERATIONAL DECISIONS"
# Points drawn per panel; beyond this a panel is thinned evenly and says so.
MAX_POINTS: Final = 20_000


@dataclass(frozen=True, slots=True)
class Caption:
    """What every export must say about itself."""

    marking: str
    sat_no: int
    window: str
    source: str
    generated: datetime

    def footer(self) -> tuple[str, str]:
        """Two lines, so the footer fits a figure drawn as narrow as 1,000 px."""
        made = self.generated.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")
        return (
            f"NORAD {self.sat_no} | {self.window} | {self.source}",
            f"generated {made} | Light Bee {__version__} | Bluestaq Limited",
        )


def rounded(values: npt.ArrayLike, digits: int = 3) -> list[Any]:
    """Numbers for JSON: rounded, with NaN as null."""
    array = np.round(np.asarray(values, dtype=np.float64), digits)
    return [None if not np.isfinite(v) else float(v) for v in array]


def thin(count: int, limit: int = MAX_POINTS) -> npt.NDArray[np.int64]:
    """Indices that keep at most `limit` of `count` points, evenly spread."""
    if count <= limit:
        return np.arange(count, dtype=np.int64)
    return np.unique(np.linspace(0, count - 1, limit).round().astype(np.int64))


def axis(title: str, **extra: Any) -> dict[str, Any]:
    return {
        "title": {"text": title, "font": {"color": MUTED, "size": 12}},
        "gridcolor": GRID,
        "zeroline": False,
        "linecolor": GRID,
        "tickfont": {"color": MUTED, "size": 11},
        **extra,
    }


def layout(
    caption: Caption,
    title: str,
    subtitle: str,
    height: int,
    *,
    evidence: str = "",
    **extra: Any,
) -> dict[str, Any]:
    """The navy frame, with markings, banner and evidence as annotations so they export.

    Sized to stay legible when Mattermost shows the PNG as a 600 px preview:
    the export is 1,600 px wide at twice the pixel density, so 18 px text in
    the figure still reads at about 7 px in the preview, and the marking sits
    on its own solid bar at the top and bottom edges.
    """
    return {
        "height": height,
        "paper_bgcolor": NAVY,
        "plot_bgcolor": PLOT,
        "font": {"family": FONT, "color": TEXT, "size": 13},
        "margin": {"l": 90, "r": 130, "t": HEADER_PX, "b": FOOTER_PX},
        "dragmode": "select",
        "hovermode": "closest",
        "legend": {
            "bgcolor": "rgba(22,38,70,0.9)",
            "bordercolor": GRID,
            "borderwidth": 1,
            "itemsizing": "constant",
            "font": {"size": 13},
        },
        "shapes": [_bar(1.0, "bottom"), _bar(0.0, "top")],
        # The header is a stack of annotations above the plotting area, each a
        # fixed number of pixels up, so nothing overlaps at any figure size.
        "annotations": [
            _note(caption.marking, 1.0, "bottom", 18, bold=True, shift=HEADER_PX - 34),
            _note(BANNER, 1.0, "bottom", 17, colour=AMBER, shift=HEADER_PX - 64),
            _note(title, 1.0, "bottom", 24, bold=True, colour=BLUE_2, shift=HEADER_PX - 104),
            _note(subtitle, 1.0, "bottom", 15, shift=HEADER_PX - 132),
            _note(evidence, 1.0, "bottom", 14, colour=MUTED, shift=HEADER_PX - 158),
            _note(caption.marking, 0.0, "top", 18, bold=True, shift=-(FOOTER_PX - 40)),
            _note(caption.footer()[0], 0.0, "top", 13, colour=MUTED, shift=-(FOOTER_PX - 86)),
            _note(caption.footer()[1], 0.0, "top", 13, colour=MUTED, shift=-(FOOTER_PX - 68)),
        ],
        **extra,
    }


HEADER_PX: Final = 200
FOOTER_PX: Final = 160
BAR: Final = "#0b1428"


def _bar(y: float, anchor: str) -> dict[str, Any]:
    """A solid band behind the top or bottom marking, so it reads at any size.

    Sized in pixels from the top or bottom edge of the plotting area.
    """
    low, high = (
        (HEADER_PX - 42, HEADER_PX) if anchor == "bottom" else (-FOOTER_PX, -(FOOTER_PX - 48))
    )
    return {
        "type": "rect",
        "xref": "paper",
        "yref": "paper",
        # Wide enough to reach both edges at any figure width.
        "x0": -1,
        "x1": 2,
        "ysizemode": "pixel",
        "yanchor": y,
        "y0": low,
        "y1": high,
        "fillcolor": BAR,
        "line": {"width": 0},
        "layer": "below",
    }


def _note(
    text: str,
    y: float,
    anchor: str,
    size: int,
    *,
    bold: bool = False,
    colour: str = TEXT,
    shift: int = 0,
) -> dict[str, Any]:
    return {
        "text": escape(text),
        "xref": "paper",
        "yref": "paper",
        "x": 0.5,
        "y": y,
        "yanchor": anchor,
        "yshift": shift,
        "showarrow": False,
        "font": {"size": size, "color": colour, "weight": "bold" if bold else "normal"},
    }


def panel_title(text: str, xaxis: str, yaxis_domain_top: float) -> dict[str, Any]:
    """A small label above one panel, so the PNG names what each panel shows."""
    return {
        "text": escape(text),
        "xref": f"{xaxis} domain",
        "yref": "paper",
        "x": 0.0,
        "y": yaxis_domain_top,
        "xanchor": "left",
        "yanchor": "bottom",
        "yshift": 4,
        "showarrow": False,
        "font": {"size": 14, "color": BLUE_2, "weight": "bold"},
    }


def escape(text: str) -> str:
    """Plotly renders a small HTML subset in titles and annotations, so escape it."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
