"""The Seasonal Behaviour? figure: the last 15 days over SEPA, and two years beneath.

Drawn as the predecessor's SpectruMonetGram was: magnitude against SEPA for
the recent days, coloured by days ago (newest yellow), and a date by SEPA
heatmap coloured from red (bright) through white to dark blue (faint), with
the daily mean longitude as a green trace on the same axis.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import numpy as np
import numpy.typing as npt

from lightbee.analysis.lightcurve import RECENT_PANEL_DAYS, DayColoured, by_day
from lightbee.analysis.seasonal import SeasonalFlag, SepaGrid
from lightbee.analysis.series import DAY_MS, Series
from lightbee.figures.common import (
    FOOTER_PX,
    HEADER_PX,
    MUTED,
    Caption,
    axis,
    layout,
    panel_title,
    rounded,
    thin,
)

# SVG markers cost the browser about 9 KB each; this keeps the panel light.
RECENT_POINTS: Final = 10_000

# The predecessor's "Dynamic Range" product (matplotlib's seismic, reversed):
# dark red brightest, white in the middle, deep navy faintest. Listed faint to
# bright, because the heatmap is drawn on negated magnitudes so that the colour
# bar reads bright at the top, as the predecessor's did.
HEAT_SCALE: Final = [
    [0.0, "#00004d"],
    [0.25, "#0000ff"],
    [0.5, "#ffffff"],
    [0.75, "#ff0000"],
    [1.0, "#800000"],
]
GEO_LON_COLOUR: Final = "#00ff00"
DATE_TICKS: Final = 28
# Plasma, newest (0 days ago) yellow to oldest dark purple, as the predecessor drew it.
DAYS_AGO_SCALE: Final = [
    [0.0, "#f0f921"],
    [0.125, "#fdca26"],
    [0.25, "#fb9f3a"],
    [0.375, "#ed7953"],
    [0.5, "#d8576b"],
    [0.625, "#bd3786"],
    [0.75, "#9c179e"],
    [0.875, "#7201a8"],
    [1.0, "#0d0887"],
]
# Colour and axis limits are fitted to the object's own magnitudes, as the
# predecessor's Dynamic Range plots were, so its changes use the whole scale:
# a few glints are ignored at each end, and a flat object still gets a 1 mag span.
LIMIT_PERCENTILES: Final = (0.5, 99.5)
LIMIT_MARGIN_MAG: Final = 0.4
MIN_SPAN_MAG: Final = 1.0


def _magnitude_title(basis: str) -> str:
    return "Range-normalised mag" if basis == "range-normalised" else "Visual magnitude"


def magnitude_limits(*pools: npt.ArrayLike) -> tuple[float, float]:
    """Bright and faint limits fitted to the magnitudes given, robust to glints."""
    values = np.concatenate([np.asarray(p, dtype=np.float64).ravel() for p in pools])
    values = values[np.isfinite(values)]
    if not values.size:
        return (7.0, 15.0)
    low, high = np.percentile(values, LIMIT_PERCENTILES)
    bright, faint = float(low) - LIMIT_MARGIN_MAG, float(high) + LIMIT_MARGIN_MAG
    if faint - bright < MIN_SPAN_MAG:
        centre = (bright + faint) / 2
        bright, faint = centre - MIN_SPAN_MAG / 2, centre + MIN_SPAN_MAG / 2
    return round(bright, 2), round(faint, 2)


def seasonal_figure(
    recent: Series, grid: SepaGrid | None, flag: SeasonalFlag | None, caption: Caption, end_ms: int
) -> dict[str, Any]:
    """The whole figure. `flag` is None until the earlier seasons are read, `grid`
    until the whole history is."""
    rows = grid.values.shape[0] if grid is not None else DEFAULT_ROWS
    height, recent_domain, heat_domain = geometry(rows)
    panel = by_day(recent, end_ms=end_ms, days=RECENT_PANEL_DAYS)
    bright, faint = magnitude_limits(panel.mag, grid.values if grid is not None else [])
    traces = [_recent_trace(panel, recent_domain)]
    if grid is not None:
        traces += _heatmap_traces(grid, recent.basis, heat_domain, (bright, faint))
    headline = flag.headline if flag is not None else "Reading the same season in earlier years"
    basis = "range-normalised magnitude" if recent.basis == "range-normalised" else "magnitude"
    if flag is not None and grid is None:
        basis += "; heatmap still being read"
    figure_layout = layout(
        caption,
        f"NORAD {caption.sat_no} Seasonal Behaviour?",
        f"{headline} ({basis})",
        height,
        evidence=evidence(flag) if flag is not None else "",
        xaxis=axis("", range=[-180, 180], anchor="y", matches="x2", showticklabels=False),
        yaxis=axis("Vis mag", domain=list(recent_domain), range=[faint, bright]),
        xaxis2=axis("SEPA (solar equatorial phase angle) [°]", range=[-180, 180], anchor="y2"),
        yaxis2=axis(
            "Observation date (recent at top)",
            domain=list(heat_domain),
            type="date",
            **(_date_ticks(grid, end_ms) if grid is not None else {"tickformat": "%Y-%m-%d"}),
        ),
        showlegend=grid is not None,
        legend={"x": 0.01, "y": heat_domain[1] - 0.01, "xanchor": "left", "yanchor": "top"},
        width=WIDTH_PX,
    )
    figure_layout["annotations"] += [
        panel_title(
            f"Last {RECENT_PANEL_DAYS} days to the change date (colour: days ago, newest yellow)",
            "x",
            recent_domain[1],
        ),
        panel_title("Daily median over two years, by SEPA (recent at top)", "x2", heat_domain[1]),
    ]
    summary = f"Seasonal Behaviour? for NORAD {caption.sat_no}: {headline}."
    if flag is not None:
        summary += " " + evidence(flag)
    return {"data": traces, "layout": figure_layout, "summary": summary}


# Portrait, as the reference plots were, with every day of the heatmap exactly
# ROW_PX pixels tall: a row of a pixel or less was dropped or doubled by the
# renderer, which drew the two years as broken streaks (owner, 11 November 2026).
WIDTH_PX: Final = 1100
ROW_PX: Final = 2
RECENT_PX: Final = 330
GAP_PX: Final = 90
DEFAULT_ROWS: Final = 731  # two years of days, the size before the history arrives


def geometry(rows: int) -> tuple[int, tuple[float, float], tuple[float, float]]:
    """Figure height and the two panels' domains, so each heatmap row is ROW_PX tall."""
    heat = rows * ROW_PX
    area = RECENT_PX + GAP_PX + heat
    return HEADER_PX + FOOTER_PX + area, ((heat + GAP_PX) / area, 1.0), (0.0, heat / area)


def evidence(flag: SeasonalFlag) -> str:
    """One line of the numbers behind the verdict, so the PNG carries its reasoning."""
    context = flag.context
    if flag.outcome == "drifting":
        return f"Longitude moved {context.recent_spread_deg:.1f}° within the window."
    if flag.outcome == "relocated":
        return (
            f"Longitude {context.where_then.removeprefix('at ')}, "
            f"{context.recent_longitude_deg:.1f}°E now: no like-for-like season to compare."
        )
    if flag.outcome == "inconclusive":
        return f"{flag.comparable_bins} comparable SEPA bins; at least 8 are needed."
    direction = "fainter" if flag.median_delta_mag > 0 else "brighter"
    years = " and ".join(str(y) for y in context.years_used)
    return (
        f"{abs(flag.median_delta_mag):.2f} mag {direction} than the same season {years} "
        f"year{'s' if context.years_used != (1,) else ''} ago, {flag.median_robust_z:+.1f} sigma "
        f"across {flag.comparable_bins} SEPA bins; longitude {context.recent_longitude_deg:.1f}°E."
    )


def _beside(domain: tuple[float, float]) -> dict[str, float]:
    """A colour bar exactly as tall as its panel."""
    return {"y": (domain[0] + domain[1]) / 2, "len": domain[1] - domain[0]}


def _date_ticks(grid: SepaGrid, end_ms: int) -> dict[str, Any]:
    """About 28 dated rows, evenly spaced, as the predecessor labelled them; the
    axis ends at the change date."""
    rows = grid.row_start_ms.size
    picked = np.unique(np.linspace(0, rows - 1, min(DATE_TICKS, rows)).astype(np.int64))
    starts = grid.row_start_ms[picked]
    return {
        "range": [int(grid.row_start_ms.min()), end_ms],
        "tickmode": "array",
        "tickvals": [int(v) for v in starts + DAY_MS // 2],
        "ticktext": [
            datetime.fromtimestamp(int(v) / 1000, UTC).strftime("%Y-%m-%d") for v in starts
        ],
    }


def _recent_trace(days: DayColoured, domain: tuple[float, float]) -> dict[str, Any]:
    keep = thin(days.epoch_ms.size, RECENT_POINTS)
    return {
        "type": "scatter",
        "mode": "markers",
        "name": f"Last {RECENT_PANEL_DAYS} days",
        "showlegend": False,
        "x": rounded(days.sepa[keep], 2),
        "y": rounded(days.mag[keep], 3),
        "customdata": [int(v) for v in days.epoch_ms[keep]],
        "xaxis": "x",
        "yaxis": "y",
        "marker": {
            "size": 3,
            "color": rounded(days.days_ago[keep], 2),
            "cmin": 0,
            "cmax": RECENT_PANEL_DAYS,
            "colorscale": DAYS_AGO_SCALE,
            "colorbar": {
                "title": {"text": "Days ago", "side": "right"},
                **_beside(domain),
                "tickfont": {"color": MUTED},
            },
        },
        "hovertemplate": "SEPA %{x:.1f}°<br>mag %{y:.2f}<extra></extra>",
    }


def _heatmap_traces(
    grid: SepaGrid, basis: str, domain: tuple[float, float], limits: tuple[float, float]
) -> list[dict[str, Any]]:
    bright, faint = limits
    middles = [int(v) for v in grid.row_start_ms + DAY_MS // 2]
    known = np.isfinite(grid.geo_longitude)
    marks = np.arange(np.ceil(bright), np.floor(faint) + 1)
    return [
        {
            "type": "heatmap",
            "name": "Daily median",
            "showlegend": False,
            "x": rounded(grid.sepa_centres, 1),
            "y": middles,
            # Negated, so the colour bar reads bright at the top; the true
            # magnitude travels in customdata for the hover.
            "z": [rounded(-row, 2) for row in grid.values],
            "customdata": [rounded(row, 2) for row in grid.values],
            "zmin": -faint,
            "zmax": -bright,
            "colorscale": HEAT_SCALE,
            "xaxis": "x2",
            "yaxis": "y2",
            "colorbar": {
                "title": {"text": _magnitude_title(basis), "side": "right"},
                **_beside(domain),
                "tickmode": "array",
                "tickvals": [float(-m) for m in marks],
                "ticktext": [f"{m:g}" for m in marks],
                "tickfont": {"color": MUTED},
            },
            "hovertemplate": "SEPA %{x}°<br>%{y|%Y-%m-%d}<br>mag %{customdata:.2f}<extra></extra>",
        },
        {
            "type": "scatter",
            "mode": "lines",
            "name": "Avg Geo Lon (°E, on the SEPA axis)",
            "x": rounded(grid.geo_longitude[known], 2),
            "y": [m for m, k in zip(middles, known, strict=True) if k],
            "xaxis": "x2",
            "yaxis": "y2",
            "line": {"color": GEO_LON_COLOUR, "width": 1.5},
            "hovertemplate": "%{y|%Y-%m-%d}<br>longitude %{x:.2f}°E<extra></extra>",
        },
    ]
