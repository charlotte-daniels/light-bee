"""The Other Causes figure: light curve, VisMag v Time, orbit, close approaches.

One figure, so one PNG carries the whole argument. The light curve sits on its
own SEPA axis at the top with the same week of earlier years drawn faintly
beneath it (ghost years); every panel below shares one time axis, so a step in
brightness, a manoeuvre in the elements and a neighbour's approach line up.
Every observation point carries its time, so a drag selection on either of the
first two panels can be sent for a neighbourhood scan.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import numpy as np

from lightbee.analysis.lightcurve import by_day, by_sensor, ghost_years
from lightbee.analysis.orbit import OrbitSeries
from lightbee.analysis.screen import SGP4_UNCERTAINTY_KM, Approach
from lightbee.analysis.series import Series
from lightbee.figures.common import (
    BLUE_2,
    MUTED,
    TEXT,
    Caption,
    axis,
    layout,
    panel_title,
    rounded,
    thin,
)

# Day 0, the newest, is red; earlier days fade from a blue that reads on black
# (about 4:1) to near white, spread across however many days the window has.
NEWEST: Final = "#d0021b"
FADE: Final = ((0x3A, 0x6F, 0xC4), (0xEE, 0xF4, 0xFB))
SENSOR_COLOURS: Final = (
    "#a55eea",
    "#26de81",
    "#fc5c65",
    "#45aaf2",
    "#fed330",
    "#fd9644",
    "#2bcbba",
    "#eb3b5a",
    "#778ca3",
    "#4b7bec",
    "#20bf6b",
    "#f7b731",
)
# Neighbours get their own dashed palette, so none is mistaken for a sensor.
NEIGHBOUR_COLOURS: Final = ("#ffffff", "#ffb3c1", "#b8f2e6", "#ffd6a5", "#cdb4db")
GHOST_COLOURS: Final = ("rgba(200,200,200,0.55)", "rgba(160,160,160,0.45)")
MAX_SENSORS: Final = len(SENSOR_COLOURS)
MAX_APPROACH_TRACES: Final = 20
LABELLED_APPROACHES: Final = 5
LABEL_LEAN_SHARE: Final = 0.15
# Markers drawn as SVG cost the browser about 9 KB each, so each panel has a
# budget, shared across its traces in proportion to their size: about 1,000
# a night over a 7-day window still covers nearly every minute for a scan.
LIGHT_CURVE_POINTS: Final = 7_000
SENSOR_POINTS: Final = 7_000
GHOST_POINTS: Final = 2_000
TIME_HOVER: Final = "%{x|%Y-%m-%d %H:%M:%S}Z"
NAMED: Final = "<extra>%{fullData.name}</extra>"
# Rows from the top, as fractions of the plotting height.
ROWS: Final = {
    "light": (0.81, 1.0),
    "time": (0.595, 0.74),
    "period": (0.45, 0.56),
    "inclination": (0.31, 0.42),
    "raan": (0.17, 0.28),
    "approach": (0.0, 0.13),
}


@dataclass(frozen=True, slots=True)
class CausesInputs:
    """Everything the Other Causes figure draws."""

    light: Series  # the basis the analyst chose for the light curve and VisMag v Time
    history: Series | None  # for ghost years, once the history is read
    orbit: OrbitSeries | None
    approaches: Sequence[Approach]
    window: tuple[int, int]
    threshold_km: float
    approaches_note: str = ""


def day_colour(day: int, days: int = 7) -> str:
    """Day 0 red; days 1 to the oldest fade evenly from blue to near white."""
    if day <= 0:
        return NEWEST
    share = (day - 1) / max(1, days - 2)
    low, high = FADE
    rgb = (round(a + (b - a) * min(1.0, share)) for a, b in zip(low, high, strict=True))
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def causes_figure(inputs: CausesInputs, caption: Caption) -> dict[str, Any]:
    start, end = inputs.window
    traces: list[dict[str, Any]] = []
    if inputs.history is not None:
        traces += _ghost_traces(inputs.history, start, end)
    traces += _light_curve_traces(inputs.light, start, end)
    traces += _sensor_traces(inputs.light, start, end)
    if inputs.orbit is not None:
        traces += _orbit_traces(inputs.orbit)
    traces += _approach_traces(inputs.approaches, inputs.threshold_km, start, end)
    basis = "range-normalised" if inputs.light.basis == "range-normalised" else "raw"
    subtitle = (
        f"Light curve and VisMag v Time ({basis} magnitude); elements from "
        f"{inputs.orbit.source if inputs.orbit else 'no provider'}; close approaches within "
        f"{inputs.threshold_km:g} km. {inputs.approaches_note}"
    ).strip()
    span = [start, end]
    figure_layout = layout(
        caption,
        f"NORAD {caption.sat_no} Other Causes",
        subtitle,
        2250,
        xaxis=axis("SEPA [°]", anchor="y"),
        yaxis=axis("Vis mag", domain=list(ROWS["light"]), autorange="reversed"),
        xaxis2=axis("", anchor="y2", type="date", range=span),
        yaxis2=axis("Vis mag", domain=list(ROWS["time"]), autorange="reversed"),
        xaxis3=axis("", anchor="y3", type="date", matches="x2", showticklabels=False),
        yaxis3=axis("Period [min]", domain=list(ROWS["period"])),
        xaxis4=axis("", anchor="y4", type="date", matches="x2", showticklabels=False),
        yaxis4=axis("Inclination [°]", domain=list(ROWS["inclination"])),
        xaxis5=axis("", anchor="y5", type="date", matches="x2", showticklabels=False),
        yaxis5=axis("RAAN [°]", domain=list(ROWS["raan"])),
        xaxis6=axis("Time (UTC)", anchor="y6", type="date", matches="x2"),
        yaxis6=axis(
            "Separation [km]", domain=list(ROWS["approach"]), range=[0, inputs.threshold_km * 4]
        ),
    )
    figure_layout["annotations"] += _panel_titles() + _approach_labels(
        inputs.approaches, inputs.threshold_km, (start, end)
    )
    figure_layout["shapes"] += [
        {
            # The band where SGP4's own error (87 km at the 95th percentile) means
            # the propagation cannot say which side of the threshold a pass falls.
            "type": "rect",
            "xref": "x6 domain",
            "yref": "y6",
            "x0": 0,
            "x1": 1,
            "y0": max(0.0, inputs.threshold_km - SGP4_UNCERTAINTY_KM),
            "y1": inputs.threshold_km + SGP4_UNCERTAINTY_KM,
            "fillcolor": "rgba(115,155,207,0.25)",
            "line": {"width": 1, "dash": "dot", "color": "rgba(115,155,207,0.8)"},
            "layer": "below",
        }
    ]
    return {"data": traces, "layout": figure_layout, "summary": summary(inputs, caption)}


def summary(inputs: CausesInputs, caption: Caption) -> str:
    """The figure in words, for screen readers and for the page."""
    inside = [a for a in inputs.approaches if a.inside]
    text = (
        f"Other Causes for NORAD {caption.sat_no}, {caption.window}: "
        f"{len(inside)} object{'s' if len(inside) != 1 else ''} within "
        f"{inputs.threshold_km:g} km during the window"
    )
    if inputs.approaches:
        nearest = inputs.approaches[0]
        when = datetime.fromtimestamp(nearest.tca_ms / 1000, UTC).strftime("%Y-%m-%d %H:%MZ")
        text += f"; nearest NORAD {nearest.sat_no} at {nearest.range_km:.0f} km on {when}"
    text += "."
    if inputs.orbit is not None and len(inputs.orbit):
        text += (
            f" Largest steps between {inputs.orbit.source} elsets: period "
            f"{inputs.orbit.largest_step('period_min'):.3f} min, inclination "
            f"{inputs.orbit.largest_step('inclination_deg'):.3f}°."
        )
    return text


def _panel_titles() -> list[dict[str, Any]]:
    return [
        panel_title("Light curve over SEPA (earlier years faint beneath)", "x", ROWS["light"][1]),
        panel_title("VisMag v Time by sensor", "x2", ROWS["time"][1]),
        panel_title("Period", "x3", ROWS["period"][1]),
        panel_title("Inclination", "x4", ROWS["inclination"][1]),
        panel_title("RAAN", "x5", ROWS["raan"][1]),
        panel_title(
            "Close approaches (shaded: propagation uncertainty)", "x6", ROWS["approach"][1]
        ),
    ]


def _approach_labels(
    found: Sequence[Approach], threshold_km: float, window: tuple[int, int]
) -> list[dict[str, Any]]:
    start, end = window
    labels: list[dict[str, Any]] = [
        {
            "text": f"{threshold_km:g} km",
            "xref": "x6",
            "yref": "y6",
            "x": end,
            "y": threshold_km,
            "xanchor": "left",
            "showarrow": False,
            "font": {"size": 12, "color": MUTED},
        }
    ]
    labels.extend(
        {
            "text": f"NORAD {approach.sat_no}: {approach.range_km:.0f} km",
            "xref": "x6",
            "yref": "y6",
            "x": approach.tca_ms,
            "y": approach.range_km,
            **_label_offset(approach.tca_ms, start, end),
            "arrowcolor": MUTED,
            "font": {"size": 12, "color": TEXT},
        }
        for approach in list(found)[:LABELLED_APPROACHES]
    )
    return labels


def _ghost_traces(history: Series, start: int, end: int) -> list[dict[str, Any]]:
    traces = []
    for ghost in ghost_years(history, start_ms=start, end_ms=end):
        keep = thin(ghost.sepa.size, GHOST_POINTS)
        traces.append(
            {
                "type": "scatter",
                "mode": "markers",
                "name": f"{ghost.years_ago} year{'s' if ghost.years_ago > 1 else ''} ago",
                "x": rounded(ghost.sepa[keep], 2),
                "y": rounded(ghost.mag[keep], 3),
                "xaxis": "x",
                "yaxis": "y",
                "marker": {"size": 4, "color": GHOST_COLOURS[min(ghost.years_ago, 2) - 1]},
                "hoverinfo": "skip",
                "legendgroup": "ghost",
                "legendgrouptitle": {"text": "Same week, earlier years"},
            }
        )
    return traces


def _light_curve_traces(light: Series, start: int, end: int) -> list[dict[str, Any]]:
    span = max(1, round((end - start) / 86_400_000))
    days = by_day(light, end_ms=end, days=span)
    kept = np.zeros(days.day.size, dtype=bool)
    kept[thin(days.day.size, LIGHT_CURVE_POINTS)] = True
    traces = []
    for day in sorted({int(d) for d in days.day}, reverse=True):  # day 0 drawn last, on top
        mine = days.day == day
        keep = kept[mine]
        traces.append(
            {
                "type": "scatter",
                "mode": "markers",
                "name": f"Day {day}",
                "x": rounded(days.sepa[mine][keep], 2),
                "y": rounded(days.mag[mine][keep], 3),
                "customdata": [int(v) for v in days.epoch_ms[mine][keep]],
                "xaxis": "x",
                "yaxis": "y",
                "marker": {"size": 4, "color": day_colour(day, span)},
                "legendgroup": "days",
                "legendgrouptitle": {"text": "Light curve: day (0 newest)"},
                "hovertemplate": "SEPA %{x:.1f}°<br>mag %{y:.2f}<extra>Day "
                + str(day)
                + "</extra>",
            }
        )
    return traces


def _sensor_traces(light: Series, start: int, end: int) -> list[dict[str, Any]]:
    tracks = by_sensor(light, start_ms=start, end_ms=end)
    total = max(1, sum(track.epoch_ms.size for track in tracks[:MAX_SENSORS]))
    traces = []
    for index, track in enumerate(tracks[:MAX_SENSORS]):
        share = max(1, round(SENSOR_POINTS * track.epoch_ms.size / total))
        keep = thin(track.epoch_ms.size, share)
        times = [int(v) for v in track.epoch_ms[keep]]
        traces.append(
            {
                "type": "scatter",
                "mode": "markers",
                "name": track.sensor,
                "x": times,
                "y": rounded(track.mag[keep], 3),
                "customdata": times,
                "xaxis": "x2",
                "yaxis": "y2",
                "marker": {
                    "size": 4,
                    "symbol": "cross-thin",
                    "line": {"width": 1.5, "color": SENSOR_COLOURS[index]},
                    "color": SENSOR_COLOURS[index],
                },
                "legendgroup": "sensors",
                "legendgrouptitle": {"text": "VisMag v Time: sensor"},
                "hovertemplate": TIME_HOVER + "<br>mag %{y:.2f}" + NAMED,
            }
        )
    return traces


def _orbit_traces(orbit: OrbitSeries) -> list[dict[str, Any]]:
    times = [int(v) for v in orbit.epoch_ms]
    panels = (
        ("Period", orbit.period_min, "y3", "x3"),
        ("Inclination", orbit.inclination_deg, "y4", "x4"),
        ("RAAN", orbit.raan_deg, "y5", "x5"),
    )
    return [
        {
            "type": "scatter",
            "mode": "lines+markers",
            "name": f"{name} ({orbit.source})",
            "showlegend": False,
            "x": times,
            "y": rounded(values, 5),
            "xaxis": x,
            "yaxis": y,
            "line": {"color": BLUE_2, "width": 1.5},
            "marker": {"size": 4, "color": BLUE_2},
            "hovertemplate": "%{x|%Y-%m-%d %H:%M}Z<br>%{y}<extra>" + name + "</extra>",
        }
        for name, values, y, x in panels
    ]


def _approach_traces(
    found: Sequence[Approach], threshold_km: float, start: int, end: int
) -> list[dict[str, Any]]:
    traces: list[dict[str, Any]] = [
        {
            "type": "scatter",
            "mode": "lines",
            "name": f"{threshold_km:g} km",
            "x": [start, end],
            "y": [threshold_km, threshold_km],
            "xaxis": "x6",
            "yaxis": "y6",
            "line": {"color": MUTED, "dash": "dash", "width": 1},
            "hoverinfo": "skip",
            "showlegend": False,
        }
    ]
    traces.extend(
        {
            "type": "scatter",
            "mode": "lines",
            "name": f"NORAD {approach.sat_no}",
            "x": [int(v) for v in approach.track_ms],
            "y": rounded(approach.track_km, 1),
            "xaxis": "x6",
            "yaxis": "y6",
            "line": {
                "width": 2,
                "dash": "dash",
                "color": NEIGHBOUR_COLOURS[index % len(NEIGHBOUR_COLOURS)],
            },
            "legendgroup": "approaches",
            "legendgrouptitle": {"text": "Close approaches"},
            "hovertemplate": TIME_HOVER + "<br>%{y:.1f} km" + NAMED,
        }
        for index, approach in enumerate(list(found)[:MAX_APPROACH_TRACES])
    )
    return traces


def _label_offset(at_ms: int, start: int, end: int) -> dict[str, Any]:
    """Labels near the start of the window lean right, clear of the axis."""
    if at_ms - start < LABEL_LEAN_SHARE * (end - start):
        return {"ax": 40, "ay": -20, "xanchor": "left"}
    return {"ax": 0, "ay": -24}
