"""The triage board's strip: 72 hours around the analyst's change time.

The sensor panel (1.5) has one row per sensor that could be compared: a band
for that sensor's own normal range, a dot for each hour (grey inside the band,
blue brighter or orange fainter beyond it), and a box round each departure.
Up is brighter. Below, the elset period steps and every pass within reach. The
change time is the amber line. The verdict cards are drawn by the page, not
here; the figure carries the headline, so the PNG still says what the board
found.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

from lightbee.analysis.triage import (
    HOUR_MIN,
    HOUR_MS,
    HOURS,
    PASSES_LABELLED,
    Board,
    SensorRow,
    marks,
    name_of,
    panel_rows,
    summary,
    when,
)
from lightbee.figures.common import (
    AMBER,
    MUTED,
    TEXT,
    Caption,
    axis,
    escape,
    layout,
    panel_title,
)

BRIGHTER: Final = "#4a8fe7"  # 5.6:1 on black
FAINTER: Final = "#f39c12"  # 9.6:1 on black
WITHIN: Final = "#8a9bb3"
BAND: Final = "rgba(115,155,207,0.30)"
# The normal range fills the middle of a row: a value this many times the
# range reaches the row's edge, and anything larger is drawn at the edge
# (its hover gives the real value).
ROW_SCALE: Final = 2.2
ROW_HALF: Final = 0.42  # of a row's height, each side of its centre
# Steps smaller than this many times typical are not marked on the strip.
MARKED_STEP_TIMES: Final = 5.0
ROW_PX: Final = 30
EVENTS_PX: Final = 110
NEIGHBOUR_MARK: Final = "#ffffff"
STEP_COLOUR: Final = "#c8d3e0"
# Passes labelled on the strip, the nearest the time; the rest show on hover.
LABELLED_PASSES: Final = PASSES_LABELLED
PANEL_TITLE: Final = "Each sensor against its own previous 7 days · up is brighter"
# The key in words, at the foot of the figure so the PNG stands alone; one line
# that fits narrower exports (the page has its own key, with swatches).
KEY: Final = (
    "Shaded: its normal range · grey: an hour within it · blue brighter or orange fainter: "
    "beyond it · boxed: a departure"
)
KEY_SHIFT_PX: Final = -44  # below the time axis labels, above the footer lines
EDGE: Final = "#739bcf"  # the band's edges, 7.4:1 on black, so the range reads at a glance


def board_figure(board: Board, caption: Caption, names: Mapping[int, str]) -> dict[str, Any]:
    rows = panel_rows(board.sensors)
    plot_px = max(len(rows), 1) * ROW_PX + EVENTS_PX + 60
    split = EVENTS_PX / plot_px
    traces, shapes, notes = _panel(board, rows)
    traces.extend(_steps(board))
    traces.extend(_passes(board, names))
    anchor = _iso(board.anchor_ms)
    count = max(len(rows), 1)
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
            range=[count - 0.5, -0.5],  # the first row at the top
            tickvals=list(range(len(rows))),
            ticktext=[f"{escape(r.name)}  ±{r.limit_mag or 0.0:.2f} mag" for r in rows],
            showgrid=False,
            zeroline=False,
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
        *shapes,
        # A dark casing under the amber line keeps it visible over orange dots.
        *(
            {
                "type": "line",
                "xref": "x",
                "yref": "paper",
                "x0": anchor,
                "x1": anchor,
                "y0": 0,
                "y1": 1,
                "line": line,
            }
            for line in ({"color": "#000000", "width": 7}, {"color": AMBER, "width": 3})
        ),
    ]
    figure_layout["annotations"] = [
        *figure_layout["annotations"],
        *notes,
        panel_title(PANEL_TITLE, "x", 1.0),
        {
            "text": escape(KEY),
            "xref": "paper",
            "yref": "paper",
            "x": 0.5,
            "y": 0.0,
            "yanchor": "top",
            "yshift": KEY_SHIFT_PX,
            "showarrow": False,
            "font": {"color": MUTED, "size": 12},
        },
        {
            "text": f"your change time, {escape(when(board.anchor_ms))}",
            "xref": "x",
            "yref": "paper",
            # Above the plot, clear of the step labels and the panel title.
            "x": anchor,
            "y": 1.0,
            "xanchor": "left",
            "yanchor": "bottom",
            "xshift": 4,
            "yshift": 22,
            "showarrow": False,
            "font": {"color": AMBER, "size": 13},
        },
    ]
    if not rows:
        figure_layout["annotations"].append(
            {
                "text": "No sensor could be compared with its own previous 7 days.",
                "xref": "paper",
                "yref": "paper",
                "x": 0.5,
                "y": (1 + split) / 2,
                "showarrow": False,
                "font": {"color": MUTED, "size": 14},
            }
        )
    summary = _described(board, rows)
    return {"data": traces, "layout": figure_layout, "summary": summary}


def _described(board: Board, rows: list[SensorRow]) -> str:
    """The figure's text for a screen reader: what the panel shows, not the cards again."""
    shown = [
        f"{m.name} {m.median_mag:+.2f} mag from {when(m.start_ms)}"
        + (", confirming" if m.confirms else "")
        for m in marks(rows, board.anchor_ms)
    ]
    steps = sum(1 for s in board.steps if (s.times_typical or 0.0) >= MARKED_STEP_TIMES)
    parts = [
        f"Triage strip. {board.headline}",
        summary(board.sensors, board.anchor_ms),
        f"Departures: {'; '.join(shown)}." if shown else "No departures.",
        f"{steps} elset {'step' if steps == 1 else 'steps'} marked; "
        f"{len(board.passes)} {'pass' if len(board.passes) == 1 else 'passes'} within reach.",
    ]
    return " ".join(parts)


def _panel(
    board: Board, rows: list[SensorRow]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """The sensor rows: the dots, the bands and boxes, and the departure labels."""
    groups: dict[str, dict[str, list[Any]]] = {
        key: {"x": [], "y": [], "text": []} for key in ("within", "brighter", "fainter")
    }
    shapes: list[dict[str, Any]] = []
    band = ROW_HALF / ROW_SCALE  # the normal range, as a share of a row
    for i, row in enumerate(rows):
        shapes.append(_rect(board.start_ms, board.end_ms, i - band, i + band, fill=BAND))
        shapes.extend(_edge(board, i + side * band) for side in (-1, 1))
        _dots(board, i, row, groups)
    boxes, notes = _boxes(board, rows)
    style = {
        "within": (WITHIN, 6),
        "brighter": (BRIGHTER, 10),
        "fainter": (FAINTER, 10),
    }
    traces = [
        {
            "type": "scatter",
            "mode": "markers",
            "x": group["x"],
            "y": group["y"],
            "hovertext": group["text"],
            "hovertemplate": "%{hovertext}<extra></extra>",
            "marker": {"color": style[key][0], "size": style[key][1]},
            "name": key,
            "xaxis": "x",
            "yaxis": "y",
        }
        for key, group in groups.items()
        if group["x"]
    ]
    return traces, shapes + boxes, notes


def _dots(board: Board, i: int, row: SensorRow, groups: dict[str, dict[str, list[Any]]]) -> None:
    """One dot per judged hour of one sensor, in its row, sorted by whether it departs."""
    limit = row.limit_mag or 0.0
    reach = ROW_SCALE * limit
    for h in range(HOURS):
        value, count = float(row.hour_median[h]), int(row.hour_count[h])
        if count < HOUR_MIN or math.isnan(value):  # an hour too thin to judge, or none
            continue
        if abs(value) < limit:
            key = "within"
        elif value < 0:
            key = "brighter"
        else:
            key = "fainter"
        drawn = max(-reach, min(reach, value)) / reach * ROW_HALF
        centre = board.start_ms + h * HOUR_MS + HOUR_MS / 2
        groups[key]["x"].append(_iso(centre))
        groups[key]["y"].append(round(i + drawn, 4))
        groups[key]["text"].append(
            f"{escape(row.name)}<br>{escape(when(centre - HOUR_MS / 2))} hour: "
            f"{value:+.2f} mag against its previous 7 days<br>{count} observations; "
            f"normal range ±{limit:.2f}"
        )


def _boxes(
    board: Board, rows: list[SensorRow]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """A box round each departure, and its size beside it."""
    where = {r.name: i for i, r in enumerate(rows)}
    shapes: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    for mark in marks(rows, board.anchor_ms):
        if mark.name not in where:
            continue
        i = where[mark.name]
        colour = BRIGHTER if mark.median_mag < 0 else FAINTER
        shapes.append(
            _rect(mark.start_ms, mark.end_ms, i - ROW_HALF - 0.02, i + ROW_HALF + 0.02, line=colour)
        )
        label = f"{mark.median_mag:+.2f} mag{', confirms' if mark.confirms else ''}"
        notes.append(
            {
                "text": f"<b>{escape(label)}</b>",
                "xref": "x",
                "yref": "y",
                "x": _iso(mark.end_ms),
                "y": i,
                "xanchor": "left",
                "xshift": 4,
                "showarrow": False,
                "bgcolor": "#000000",
                "font": {"color": colour, "size": 12},
            }
        )
    return shapes, notes


def _edge(board: Board, y: float) -> dict[str, Any]:
    return {
        "type": "line",
        "xref": "x",
        "yref": "y",
        "x0": _iso(board.start_ms),
        "x1": _iso(board.end_ms),
        "y0": y,
        "y1": y,
        "line": {"color": EDGE, "width": 1},
        "layer": "below",
    }


def _rect(
    start_ms: float,
    end_ms: float,
    low: float,
    high: float,
    *,
    fill: str = "rgba(0,0,0,0)",
    line: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "rect",
        "xref": "x",
        "yref": "y",
        "x0": _iso(start_ms),
        "x1": _iso(end_ms),
        "y0": low,
        "y1": high,
        "fillcolor": fill,
        "line": {"width": 1.5, "color": line} if line else {"width": 0},
        "layer": "below" if line is None else "above",
    }


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
    labels = [escape(f"{name_of(p.sat_no, names)} {p.range_km:,.0f} km") for p in board.passes]
    # Only the passes nearest the time are labelled, so a busy strip stays
    # legible (case 4 had 16); every pass keeps its marker and its hover label.
    # Labels go to the real passes nearest the time, as the Neighbour card counts
    # them; a neighbour within reach throughout keeps its marker and hover label.
    real = [i for i, p in enumerate(board.passes) if not p.held]
    nearest = sorted(real, key=lambda i: abs(board.passes[i].tca_ms - board.anchor_ms))[
        :LABELLED_PASSES
    ]
    shown = sorted(nearest)
    return [
        {
            "type": "scatter",
            "mode": "markers+text",
            "x": [_iso(p.tca_ms) for p in board.passes],
            "y": [0] * len(board.passes),
            "marker": {"symbol": "diamond", "size": 12, "color": NEIGHBOUR_MARK},
            "text": [label if i in nearest else "" for i, label in enumerate(labels)],
            "hovertext": labels,
            # Labelled passes alternate above and below, so two close in time stay apart.
            "textposition": [
                "top center" if i in nearest and shown.index(i) % 2 == 0 else "bottom center"
                for i in range(len(labels))
            ],
            "textfont": {"color": TEXT, "size": 11},
            "hovertemplate": "%{hovertext}<br>%{x|%d %b %H:%M}Z<extra></extra>",
            "xaxis": "x",
            "yaxis": "y2",
        }
    ]


def _iso(ms: float) -> str:
    return datetime.fromtimestamp(float(ms) / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%S")
