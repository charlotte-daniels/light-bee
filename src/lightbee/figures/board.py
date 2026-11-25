"""The triage board's strip: 72 hours around the analyst's change time.

Each sensor's row shows, hour by hour, how far it sits from its own day before
at the same SEPA (fainter is warmer). Below, the elset period steps and every
pass within reach. The change time is the amber line. The verdict cards are
drawn by the page, not here; the figure carries the headline, so the PNG still
says what the board found.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

from lightbee.analysis.triage import HOUR_MS, HOURS, Board, name_of, when
from lightbee.figures.common import (
    AMBER,
    MUTED,
    TEXT,
    Caption,
    axis,
    escape,
    layout,
    panel_title,
    rounded,
)

# Brighter than the day before is blue, fainter is orange; both ends read on
# black, and the middle is the plot's own dark grey, so an hour with nothing
# to compare is a gap, not a zero.
DEPARTURE_SCALE: Final = [[0.0, "#3a7bd5"], [0.5, "#1b2433"], [1.0, "#f39c12"]]
DEPARTURE_RANGE_MAG: Final = 0.5
# Steps smaller than this many times typical are not marked on the strip.
MARKED_STEP_TIMES: Final = 5.0
ROW_PX: Final = 34
EVENTS_PX: Final = 120
NEIGHBOUR_MARK: Final = "#ffffff"
STEP_COLOUR: Final = "#c8d3e0"


def board_figure(board: Board, caption: Caption, names: Mapping[int, str]) -> dict[str, Any]:
    rows = board.sensors
    height_rows = max(len(rows), 1) * ROW_PX
    plot_px = height_rows + EVENTS_PX + 60
    split = EVENTS_PX / plot_px
    hours = [_iso(board.start_ms + h * HOUR_MS + HOUR_MS / 2) for h in range(HOURS)]
    traces: list[dict[str, Any]] = []
    if rows:
        traces.append(
            {
                "type": "heatmap",
                "x": hours,
                "y": [r.name for r in rows],
                "z": [rounded(r.hour_median, 3) for r in rows],
                "customdata": [r.hour_count.tolist() for r in rows],
                "zmin": -DEPARTURE_RANGE_MAG,
                "zmax": DEPARTURE_RANGE_MAG,
                "zmid": 0.0,
                "colorscale": DEPARTURE_SCALE,
                "xgap": 1,
                "ygap": 2,
                "hoverongaps": False,
                "hovertemplate": (
                    "%{y}<br>%{x|%d %b %H}:00Z hour<br>%{z:+.2f} mag against its day before"
                    "<br>%{customdata} observations<extra></extra>"
                ),
                "colorbar": {
                    "title": {"text": "mag v. day before<br>(fainter +)", "side": "right"},
                    "tickfont": {"color": MUTED},
                    "len": 1 - split,
                    "y": 1.0,
                    "yanchor": "top",
                },
                "xaxis": "x",
                "yaxis": "y",
            }
        )
    traces.extend(_steps(board))
    traces.extend(_passes(board, names))
    anchor = _iso(board.anchor_ms)
    extra = {
        "showlegend": False,
        "xaxis": axis(
            "",
            type="date",
            range=[_iso(board.start_ms), _iso(board.end_ms)],
            anchor="y2",
            dtick=6 * HOUR_MS,
            tickformat="%d %b<br>%H:%MZ",
        ),
        "yaxis": axis(
            "",
            domain=[split + 0.04, 1.0],
            autorange="reversed",
            type="category",
            showgrid=False,
        ),
        "yaxis2": axis(
            "",
            domain=[0.0, split],
            range=[-0.6, 1.7],
            tickvals=[0, 1],
            ticktext=["Passes", "Elset steps"],
            showgrid=False,
        ),
    }
    figure_layout = layout(
        caption,
        "Triage around your change time",
        board.headline,
        plot_px + 360,
        evidence=board.lead,
        **extra,
    )
    figure_layout["dragmode"] = False
    figure_layout["shapes"] = [
        *figure_layout["shapes"],
        {
            "type": "line",
            "xref": "x",
            "yref": "paper",
            "x0": anchor,
            "x1": anchor,
            "y0": 0,
            "y1": 1,
            "line": {"color": AMBER, "width": 3},
        },
    ]
    figure_layout["annotations"] = [
        *figure_layout["annotations"],
        panel_title("Each sensor against itself the day before, hour by hour", "x", 1.0),
        {
            "text": f"your change time, {escape(when(board.anchor_ms))}",
            "xref": "x",
            "yref": "paper",
            "x": anchor,
            "y": split,
            "xanchor": "left",
            "yanchor": "top",
            "xshift": 4,
            "yshift": -2,
            "showarrow": False,
            "font": {"color": AMBER, "size": 13},
        },
    ]
    if not rows:
        figure_layout["annotations"].append(
            {
                "text": "No sensor saw the object in these 72 hours.",
                "xref": "paper",
                "yref": "paper",
                "x": 0.5,
                "y": (1 + split) / 2,
                "showarrow": False,
                "font": {"color": MUTED, "size": 14},
            }
        )
    verdicts = " ".join(f"{c.title}: {c.body}" for c in board.cards)
    summary = f"Triage strip. {board.headline} {verdicts}"
    return {"data": traces, "layout": figure_layout, "summary": summary}


def _steps(board: Board) -> list[dict[str, Any]]:
    """Each marked period step as a bar from the last elset without it to the first with it."""
    out = []
    for step in board.steps:
        times = step.times_typical
        if times is None or times < MARKED_STEP_TIMES:
            continue
        label = f"{step.step_min:+.3f} min, {times:,.0f} times"
        hover = f"{escape(label)} the typical step<extra></extra>"
        middle = _iso((step.before_ms + step.after_ms) / 2)
        out.append(
            {
                "type": "scatter",
                "mode": "lines",
                "x": [_iso(step.before_ms), _iso(step.after_ms)],
                "y": [1, 1],
                "line": {"color": STEP_COLOUR, "width": 8},
                "hovertemplate": hover,
                "xaxis": "x",
                "yaxis": "y2",
            }
        )
        # The label sits above its bar, never on it.
        out.append(
            {
                "type": "scatter",
                "mode": "text",
                "x": [middle],
                "y": [1.35],
                "text": [label],
                "textposition": "middle center",
                "textfont": {"color": TEXT, "size": 12},
                "hovertemplate": hover,
                "xaxis": "x",
                "yaxis": "y2",
            }
        )
    return out


def _passes(board: Board, names: Mapping[int, str]) -> list[dict[str, Any]]:
    if not board.passes:
        return []
    labels = [f"{name_of(p.sat_no, names)} {p.range_km:,.0f} km" for p in board.passes]
    return [
        {
            "type": "scatter",
            "mode": "markers+text",
            "x": [_iso(p.tca_ms) for p in board.passes],
            "y": [0] * len(board.passes),
            "marker": {"symbol": "diamond", "size": 12, "color": NEIGHBOUR_MARK},
            "text": [escape(label) for label in labels],
            # Alternate above and below, so passes close in time stay legible.
            "textposition": [
                "top center" if i % 2 == 0 else "bottom center" for i in range(len(labels))
            ],
            "textfont": {"color": TEXT, "size": 11},
            "hovertemplate": "%{text}<br>%{x|%d %b %H:%M}Z<extra></extra>",
            "xaxis": "x",
            "yaxis": "y2",
        }
    ]


def _iso(ms: float) -> str:
    return datetime.fromtimestamp(float(ms) / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%S")
