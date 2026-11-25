"""The anchored triage board (1.4): what changed around the analyst's change time.

Proposal 02 ran a blind, week-wide search on the owner's four known events,
and it failed: the events last hours, and sensors disagree with each other by
more than the events. So the board starts from the analyst's own approximate
time and looks 24 hours before it and 48 after:

* each sensor against itself over the 7 days ending 6 hours before the time,
  at the same solar phase angle and always from another night, hour by hour,
  so one sensor's offset never passes for a change and a change that began
  hours before the analyst's time is dated to when it began;
* the target's elset period steps against its own typical step over the
  30 days before;
* every pass of every neighbour within reach;
* the seasonal flag, which says it cannot judge when it cannot.

Each cause is judged on timing only, and says so when it cannot judge.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal

import numpy as np
import numpy.typing as npt

from lightbee.analysis.groups import FloatArray, IntArray
from lightbee.analysis.orbit import orbit_series
from lightbee.analysis.screen import SGP4_UNCERTAINTY_KM, Approach
from lightbee.analysis.seasonal import SeasonalFlag
from lightbee.analysis.series import DAY_MS, Series
from lightbee.udl.records import Elset

HOUR_MS: Final = 3_600_000
BEFORE_MS: Final = 24 * HOUR_MS
AFTER_MS: Final = 48 * HOUR_MS
HOURS: Final = (BEFORE_MS + AFTER_MS) // HOUR_MS
# The analyst's time may be hours out: departures from this long before it
# count, and the baseline ends this long before it, so an early start is seen.
EARLY_MS: Final = 6 * HOUR_MS
# Each sensor's baseline: its own 7 days ending EARLY_MS before the time, enough
# nights to measure its night-to-night offsets (3 nights proved too few).
BASELINE_MS: Final = 7 * 24 * HOUR_MS
# An observation is compared with the same sensor's baseline observations
# within this many degrees of solar phase angle (SEPA): brightness follows SEPA
# through every night, so this removes the nightly curve. Observations from
# its own night are left out, so every comparison is night to night.
SEPA_MATCH_DEG: Final = 3.0
OWN_NIGHT_MS: Final = 12 * HOUR_MS
MATCH_MIN: Final = 3  # baseline observations needed for one comparison
SIDE_MIN: Final = 10  # comparisons needed in the baseline, and after the time
HOUR_MIN: Final = 3  # comparisons needed in an hour to judge that hour
BASELINE_HOURS_MIN: Final = 6  # judged baseline hours needed to know the scatter
# An hour departs when its median is at least this far from the baseline and
# at least DEPART_Z times the baseline's own noise: the larger of its hourly
# and its nightly spread, which carries the night-to-night offsets real
# sensors have. Measured on about 7 nights that noise is itself uncertain, so
# the multiplier is 4, not 3 (at 3, nightly offsets of 0.1 mag alone crossed
# it in synthetic tests).
SHIFT_FLOOR_MAG: Final = 0.1
DEPART_Z: Final = 4.0
NIGHTS_MIN: Final = 3  # baseline nights needed to measure the nightly spread
# A change persists: one hour alone over the limit is noise, so a departure
# needs this many consecutive departing hours (with nightly offsets of 0.03
# mag, single hours crossed 0.1 mag in synthetic tests).
SPAN_MIN_HOURS: Final = 2
SCATTER_FLOOR_MAG: Final = 0.02
MAX_SENSOR_ROWS: Final = 12
ELSET_BASELINE_DAYS: Final = 30
BASELINE_STEPS_MIN: Final = 10
# A period step this many times the object's typical step fits a manoeuvre.
# Case 4's manoeuvre measured 526 and 1,151 times a one-week baseline; drifting
# objects reached 49 to 94 against one week, so it is judged on 30 days.
STEP_FITS_TIMES: Final = 20.0
# A period that never changed still has a typical step this small: case 4's
# typical KBR step was about 0.0002 min, so a quiet month is not a zero.
TYPICAL_FLOOR_MIN: Final = 0.0001
# One sensor cannot be checked against another.
MIN_SENSORS: Final = 2
LOOSE_ELSET_GAP_MS: Final = 12 * HOUR_MS
# A step fits the timing when its elsets bracket a moment within this of the time.
NEAR_STEP_MS: Final = 12 * HOUR_MS
NEAR_PASS_MS: Final = 6 * HOUR_MS
# A neighbour passing this many times in the 72 hours passes routinely.
ROUTINE_PASSES: Final = 3
CANNOT_JUDGE_OUTCOMES: Final = frozenset({"drifting", "relocated", "inconclusive"})

State = Literal["fits", "against", "cannot"]


@dataclass(frozen=True)
class Span:
    """Consecutive departing hours of one sensor, with their median departure."""

    start_ms: int
    end_ms: int
    median_mag: float


@dataclass(frozen=True)
class SensorRow:
    """One sensor against its own baseline, hour by hour."""

    name: str
    before: int  # comparisons in the baseline
    after: int  # comparisons after the time
    scatter_mag: float | None  # of the baseline's hourly medians; None: too few to judge
    shift_mag: float | None  # median departure after the time; None: too few
    hour_median: FloatArray  # (HOURS,), NaN where the hour cannot be judged
    hour_count: IntArray  # (HOURS,)
    spans: tuple[Span, ...]
    departs: bool  # a span starts within EARLY_MS before the time, or after it
    # (HOURS,): +1 or -1 where that hour alone departs, else 0. A single hour
    # never stands alone as a change, but it does confirm another sensor's.
    hour_sign: IntArray


@dataclass(frozen=True)
class ElsetStep:
    """A change in period between two consecutive elsets."""

    before_ms: int
    after_ms: int
    step_min: float
    times_typical: float | None


@dataclass(frozen=True)
class Pass:
    sat_no: int
    tca_ms: int
    range_km: float
    passes_in_strip: int
    held: bool = False  # within reach throughout, no distinct pass: closest moment shown


@dataclass(frozen=True)
class Card:
    key: str
    state: State
    title: str
    body: str


@dataclass(frozen=True)
class Board:
    anchor_ms: int
    start_ms: int
    end_ms: int
    data_end_ms: int
    headline: str
    lead: str
    sensors: tuple[SensorRow, ...]
    steps: tuple[ElsetStep, ...]
    typical_step_min: float | None
    baseline_steps: int
    passes: tuple[Pass, ...]
    cards: tuple[Card, ...]


def strip(anchor_ms: int) -> tuple[int, int]:
    """The 72 hours the board looks at: 24 before the time, 48 after."""
    return anchor_ms - BEFORE_MS, anchor_ms + AFTER_MS


def baseline(anchor_ms: int) -> tuple[int, int]:
    """Each sensor's baseline: the 7 days ending EARLY_MS before the time."""
    end = anchor_ms - EARLY_MS
    return end - BASELINE_MS, end


def sensor_rows(series: Series, anchor_ms: int) -> list[SensorRow]:
    """Every sensor that saw the object in the strip, the busiest first."""
    start, end = strip(anchor_ms)
    part = series.between(min(start, baseline(anchor_ms)[0]), end)
    part = part.take(np.isfinite(part.mag) & np.isfinite(part.sepa))
    seen = part.sensor[(part.epoch_ms >= start) & (part.epoch_ms < end)]
    rows = [
        _sensor_row(name, part.take(part.sensor == index), anchor_ms)
        for index, name in enumerate(series.sensors)
        if np.any(seen == index)
    ]
    rows.sort(key=lambda r: -(r.before + r.after))
    return rows[:MAX_SENSOR_ROWS]


def _sensor_row(name: str, mine: Series, anchor_ms: int) -> SensorRow:
    start, end = strip(anchor_ms)
    base_start, base_end = baseline(anchor_ms)
    in_base = (mine.epoch_ms >= base_start) & (mine.epoch_ms < base_end)
    res = _residuals(mine, in_base)
    judged = np.isfinite(res)
    base_res = res[in_base & judged]
    after = res[(mine.epoch_ms >= anchor_ms) & (mine.epoch_ms < end) & judged]
    scatter = _hourly_scatter(res, mine.epoch_ms, in_base & judged, base_start)
    shift = float(np.median(after)) if after.size >= SIDE_MIN and scatter is not None else None
    inside = (mine.epoch_ms >= start) & (mine.epoch_ms < end)
    hour = (mine.epoch_ms - start) // HOUR_MS
    medians = np.full(HOURS, np.nan)
    counts = np.zeros(HOURS, dtype=np.int64)
    for h in range(HOURS):
        values = res[inside & (hour == h) & judged]
        counts[h] = values.size
        if values.size and scatter is not None:
            medians[h] = float(np.median(values))
    sign = _hour_signs(medians, counts, scatter)
    spans = _spans(res, hour, inside & judged, sign, start)
    departs = any(s.end_ms > anchor_ms - EARLY_MS for s in spans)
    return SensorRow(
        name,
        int(base_res.size),
        int(after.size),
        scatter,
        shift,
        medians,
        counts,
        spans,
        departs,
        sign,
    )


def _hour_signs(medians: FloatArray, counts: IntArray, scatter: float | None) -> IntArray:
    """+1 or -1 for each hour that departs on its own, else 0."""
    if scatter is None:
        return np.zeros(HOURS, dtype=np.int64)
    limit = max(SHIFT_FLOOR_MAG, DEPART_Z * scatter)
    departing = (counts >= HOUR_MIN) & (np.abs(np.nan_to_num(medians)) >= limit)
    return np.where(departing, np.sign(np.nan_to_num(medians)), 0).astype(np.int64)


def _hourly_scatter(
    res: FloatArray, times: IntArray, chosen: npt.NDArray[np.bool_], base_start: int
) -> float | None:
    """The noise a departing hour must stand above, or None if too few hours.

    The larger of two: the robust scatter of the baseline's hourly medians, and
    the spread of its nightly medians. Hours of one night share that night's
    offset, so the nightly spread is what a whole night's departure is judged
    against; it is a plain standard deviation, as a robust one from about 7
    nights is too unsteady.
    """
    hour = (times[chosen] - base_start) // HOUR_MS
    values = res[chosen]
    medians = [
        float(np.median(values[hour == h]))
        for h in np.unique(hour)
        if np.count_nonzero(hour == h) >= HOUR_MIN
    ]
    if len(medians) < BASELINE_HOURS_MIN or values.size < SIDE_MIN:
        return None
    hourly = np.asarray(medians)
    spread = 1.4826 * float(np.median(np.abs(hourly - np.median(hourly))))
    night = hour // 24
    nightly = [
        float(np.median(values[night == n]))
        for n in np.unique(night)
        if np.count_nonzero(night == n) >= SIDE_MIN
    ]
    if len(nightly) >= NIGHTS_MIN:
        spread = max(spread, float(np.std(nightly, ddof=1)))
    return max(spread, SCATTER_FLOOR_MAG)


def _residuals(mine: Series, in_base: npt.NDArray[np.bool_]) -> FloatArray:
    """Each observation less the median of the same sensor's baseline at the same SEPA.

    Baseline observations from the observation's own night are left out, so
    every comparison is night to night, the baseline's own included.
    """
    order = np.flatnonzero(in_base)[np.argsort(mine.sepa[in_base], kind="stable")]
    sepa, mag, when_ms = mine.sepa[order], mine.mag[order], mine.epoch_ms[order]
    out = np.full(mine.epoch_ms.size, np.nan)
    for i in range(mine.epoch_ms.size):
        lo = int(np.searchsorted(sepa, mine.sepa[i] - SEPA_MATCH_DEG, "left"))
        hi = int(np.searchsorted(sepa, mine.sepa[i] + SEPA_MATCH_DEG, "right"))
        other_night = np.abs(when_ms[lo:hi] - mine.epoch_ms[i]) >= OWN_NIGHT_MS
        near = mag[lo:hi][other_night]
        if near.size >= MATCH_MIN:
            out[i] = mine.mag[i] - float(np.median(near))
    return out


def _spans(
    res: FloatArray,
    hour: IntArray,
    judged: npt.NDArray[np.bool_],
    sign: IntArray,
    start: int,
) -> tuple[Span, ...]:
    """Runs of at least SPAN_MIN_HOURS consecutive departing hours of one sign."""
    spans: list[Span] = []
    h = 0
    while h < HOURS:
        if sign[h] == 0:
            h += 1
            continue
        first = h
        while h + 1 < HOURS and sign[h + 1] == sign[first]:
            h += 1
        if h - first + 1 >= SPAN_MIN_HOURS:
            inside = judged & (hour >= first) & (hour <= h)
            spans.append(
                Span(
                    start + first * HOUR_MS,
                    start + (h + 1) * HOUR_MS,
                    float(np.median(res[inside])),
                )
            )
        h += 1
    return tuple(spans)


def elset_steps(
    elsets: Sequence[Elset], source: str, anchor_ms: int
) -> tuple[list[ElsetStep], float | None, int]:
    """Period steps in the strip, each against the typical step of the 30 days before it.

    Returns the steps, the typical step (None if too few to know) and how many
    steps the baseline held.
    """
    start, end = strip(anchor_ms)
    orbit = orbit_series(elsets, source, start_ms=start - ELSET_BASELINE_DAYS * DAY_MS, end_ms=end)
    if len(orbit) < 2:
        return [], None, 0
    t, steps = orbit.epoch_ms, np.diff(orbit.period_min)
    finite = np.isfinite(steps)
    baseline = np.abs(steps[(t[1:] < start) & finite])
    typical = (
        max(float(np.median(baseline)), TYPICAL_FLOOR_MIN)
        if baseline.size >= BASELINE_STEPS_MIN
        else None
    )
    found = [
        ElsetStep(
            int(t[k]),
            int(t[k + 1]),
            float(steps[k]),
            abs(float(steps[k])) / typical if typical else None,
        )
        for k in range(steps.size)
        if finite[k] and t[k + 1] >= start and t[k] < end
    ]
    return found, typical, int(baseline.size)


def passes_in_strip(
    approaches: Sequence[Approach], anchor_ms: int, reach_km: float = np.inf
) -> list[Pass]:
    """Every pass of every neighbour inside the strip, in time order.

    A neighbour that stays within reach without a distinct pass in the strip
    (one sharing the target's slot) is listed once, at its closest there.
    """
    start, end = strip(anchor_ms)
    found: list[Pass] = []
    for item in approaches:
        mine = [(t, d) for t, d in (item.passes or ((item.tca_ms, item.range_km),))]
        inside = [(t, d) for t, d in mine if start <= t < end]
        found.extend(Pass(item.sat_no, int(t), float(d), len(inside)) for t, d in inside)
        if not inside:
            held = _held(item, start, end, reach_km)
            if held is not None:
                found.append(held)
    return sorted(found, key=lambda p: p.tca_ms)


def _held(item: Approach, start: int, end: int, reach_km: float) -> Pass | None:
    """A neighbour within reach in the strip with no pass there: its closest moment."""
    times, km = item.track_ms, item.track_km
    inside = (times >= start) & (times < end) & np.isfinite(km)
    if not inside.any():
        return None
    k = int(np.argmin(np.where(inside, km, np.inf)))
    if km[k] > reach_km:
        return None
    return Pass(item.sat_no, int(times[k]), float(km[k]), 0, held=True)


@dataclass(frozen=True)
class Inputs:
    """What one run holds that the board reads."""

    series: Series | None
    elsets: Sequence[Elset]
    source: str
    approaches: Sequence[Approach]
    flag: SeasonalFlag | None
    threshold_km: float
    names: Mapping[int, str]
    approaches_note: str = ""


def board(given: Inputs, *, anchor_ms: int, data_end_ms: int) -> Board:
    """The whole board for one run and one change time."""
    start, end = strip(anchor_ms)
    rows = sensor_rows(given.series, anchor_ms) if given.series is not None else []
    steps, typical, baseline = elset_steps(given.elsets, given.source, anchor_ms)
    passes = passes_in_strip(given.approaches, anchor_ms, given.threshold_km + SGP4_UNCERTAINTY_KM)
    has_elsets = bool(given.elsets)
    cards = (
        _manoeuvre(steps, typical, baseline, given.source, anchor_ms, has_elsets=has_elsets),
        _neighbour(
            passes, given.threshold_km, given.names, anchor_ms, given.approaches_note, has_elsets
        ),
        _season(given.flag, rows),
        _sensor(rows, anchor_ms),
    )
    return Board(
        anchor_ms,
        start,
        end,
        data_end_ms,
        _headline(rows, cards, anchor_ms),
        _lead(anchor_ms, data_end_ms, given.source),
        tuple(rows),
        tuple(steps),
        typical,
        baseline,
        tuple(passes),
        cards,
    )


def when(ms: float) -> str:
    moment = datetime.fromtimestamp(ms / 1000, UTC)
    return f"{moment.day} {moment:%b %H:%M}Z"


def apart(ms: float, anchor_ms: int) -> str:
    hours = abs(ms - anchor_ms) / HOUR_MS
    side = "before" if ms < anchor_ms else "after"
    return f"{hours:.1f} hours {side} your time"


def name_of(sat_no: int, names: Mapping[int, str]) -> str:
    name = names.get(sat_no)
    return f"NORAD {sat_no} ({name})" if name else f"NORAD {sat_no}"


def joined(items: Sequence[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _manoeuvre(
    steps: Sequence[ElsetStep],
    typical: float | None,
    baseline: int,
    source: str,
    anchor_ms: int,
    *,
    has_elsets: bool,
) -> Card:
    title = "Manoeuvre"
    if not has_elsets or not steps:
        return Card("manoeuvre", "cannot", title, f"No {source} elsets either side of your time.")
    if typical is None:
        return Card(
            "manoeuvre",
            "cannot",
            title,
            f"Too few {source} elsets in the {ELSET_BASELINE_DAYS} days before "
            f"({baseline} steps) to know this object's typical step.",
        )
    large = [s for s in steps if (s.times_typical or 0.0) >= STEP_FITS_TIMES]
    if not large:
        times = max(s.times_typical or 0.0 for s in steps)
        return Card(
            "manoeuvre",
            "against",
            title,
            f"The largest {source} period step in these 72 hours is {times:,.0f} times this "
            f"object's typical step over the {ELSET_BASELINE_DAYS} days before: "
            "no manoeuvre shows in the elements.",
        )
    # A step fits when the two elsets around it bracket a moment near the time.
    near = [
        s
        for s in large
        if s.before_ms <= anchor_ms + NEAR_STEP_MS and s.after_ms >= anchor_ms - NEAR_STEP_MS
    ]
    big = max(near or large, key=lambda s: abs(s.step_min))
    body = _step_text(big, source, anchor_ms)
    if not near:
        return Card(
            "manoeuvre", "against", title, f"{body} That is too far from your time to explain it."
        )
    return Card("manoeuvre", "fits", title, body)


def _step_text(big: ElsetStep, source: str, anchor_ms: int) -> str:
    body = (
        f"{source} period changed by {big.step_min:+.3f} min, "
        f"{big.times_typical or 0.0:,.0f} times this object's typical step over the "
        f"{ELSET_BASELINE_DAYS} days before: the last elset without it is "
        f"{when(big.before_ms)}, the first with it {when(big.after_ms)} "
        f"({apart(big.after_ms, anchor_ms)})."
    )
    if big.after_ms - big.before_ms > LOOSE_ELSET_GAP_MS:
        gap = (big.after_ms - big.before_ms) / HOUR_MS
        body += f" Those elsets are {gap:.0f} hours apart, so the timing is loose."
    return body


def _neighbour(
    passes: Sequence[Pass],
    threshold_km: float,
    names: Mapping[int, str],
    anchor_ms: int,
    note: str,
    has_elsets: bool,
) -> Card:
    title = "Neighbour"
    if not has_elsets or note:
        reason = note or "no elsets for the target, so nothing could be screened"
        return Card("neighbour", "cannot", title, f"Not screened: {reason}.")
    if not passes:
        return Card(
            "neighbour",
            "against",
            title,
            f"No GEO object came within {threshold_km:,.0f} km in these 72 hours.",
        )
    held = [p for p in passes if p.held]
    alongside = "".join(
        f" {name_of(p.sat_no, names)} stays within reach throughout (closest "
        f"{p.range_km:,.0f} km, {when(p.tca_ms)})."
        for p in held
    )
    real = [p for p in passes if not p.held]
    if not real:
        return Card(
            "neighbour",
            "cannot",
            title,
            f"No distinct pass in these 72 hours.{alongside} Something always this close "
            "cannot be judged on timing.",
        )
    near = min(real, key=lambda p: abs(p.tca_ms - anchor_ms))
    said = (
        f"{name_of(near.sat_no, names)} passed at {near.range_km:,.0f} km on "
        f"{when(near.tca_ms)}, {apart(near.tca_ms, anchor_ms)}."
    )
    if near.passes_in_strip >= ROUTINE_PASSES:
        said += (
            f" It passes {near.passes_in_strip} times in these 72 hours, so a pass near "
            "your time is not unusual on its own."
        )
    listed = f" Every pass is listed: {len(real)} in all.{alongside}"
    if abs(near.tca_ms - anchor_ms) > NEAR_PASS_MS:
        return Card("neighbour", "against", title, f"Nearest in time: {said}{listed}")
    if near.range_km > threshold_km:
        return Card(
            "neighbour",
            "cannot",
            title,
            f"{said} That is beyond your {threshold_km:,.0f} km but within the elsets' "
            f"uncertainty.{listed}",
        )
    return Card("neighbour", "fits", title, said + listed)


def _season(flag: SeasonalFlag | None, rows: Sequence[SensorRow]) -> Card:
    title = "Season"
    if flag is None:
        return Card("season", "cannot", title, "The seasonal flag is not ready.")
    quoted = f"The flag says: “{flag.headline}”"
    if flag.outcome in CANNOT_JUDGE_OUTCOMES:
        return Card(
            "season", "cannot", title, f"{quoted}. A seasonal comparison is not valid here."
        )
    judged = _judged(rows)
    if not judged:
        return Card(
            "season",
            "cannot",
            title,
            f"No sensor could be compared with itself around your time, so the timing "
            f"cannot be judged. {quoted} across the window.",
        )
    if any(r.departs for r in judged):
        return Card(
            "season",
            "against",
            title,
            f"Seasons change over weeks; this change is within hours. {quoted}.",
        )
    if flag.outcome == "consistent":
        return Card("season", "fits", title, f"{quoted}.")
    return Card("season", "against", title, f"{quoted}.")


def _sensor(rows: Sequence[SensorRow], anchor_ms: int) -> Card:
    title = "Sensor"
    judged = _judged(rows)
    if len(judged) < MIN_SENSORS:
        return Card(
            "sensor",
            "cannot",
            title,
            f"{len(judged)} sensor saw the object enough both sides of your time; "
            "at least 2 are needed to tell a sensor's artefact from a change."
            if len(judged) == 1
            else "No sensor saw the object enough both sides of your time.",
        )
    found = _agreement(rows, anchor_ms)
    if found is None:
        return Card(
            "sensor",
            "cannot",
            title,
            f"None of {len(judged)} sensors departs from its own previous 7 days, "
            "so there is no change to put down to a sensor.",
        )
    hours = f"{when(found.span.start_ms)} to {when(found.span.end_ms)}"
    way = "fainter" if found.span.median_mag > 0 else "brighter"
    aside = "".join(
        f" {r.name} departs separately, from {when(s.start_ms)}; that is not judged here."
        for r, s in found.separate
    )
    if found.conflicting:
        return Card(
            "sensor",
            "cannot",
            title,
            f"{joined([r.name for r in found.agreeing])} {way} from {hours}, but "
            f"{joined([r.name for r in found.conflicting])} went the other way in those "
            f"hours, so the sensors disagree.{aside}",
        )
    if len(found.agreeing) >= MIN_SENSORS:
        return Card(
            "sensor",
            "against",
            title,
            f"{joined([r.name for r in found.agreeing])} depart the same way ({way}) in the "
            f"same hours, {hours}, each against its own previous 7 days, so it is not one "
            f"sensor's artefact.{aside}",
        )
    only = found.agreeing[0].name
    if not found.level:
        return Card(
            "sensor",
            "cannot",
            title,
            f"Only {only} departs ({way}, {hours}), and no other sensor observed those "
            f"hours, so one sensor's artefact cannot be told from a change.{aside}",
        )
    level = joined([r.name for r in found.level])
    return Card(
        "sensor",
        "fits",
        title,
        f"Only {only} departs ({way}, {hours}); {level}, observing the same hours, "
        f"{'stays' if len(found.level) == 1 else 'stay'} level. One sensor's artefact is "
        f"likely.{aside}",
    )


@dataclass(frozen=True)
class Agreement:
    """How every sensor stands against one reference departure."""

    span: Span  # the reference: the counted departure nearest the time
    agreeing: list[SensorRow]  # depart the same way in at least one of its hours
    conflicting: list[SensorRow]  # depart the other way in at least one of its hours
    level: list[SensorRow]  # observed at least half its hours and did not depart
    separate: list[tuple[SensorRow, Span]]  # departures elsewhere, not judged


def _agreement(rows: Sequence[SensorRow], anchor_ms: int) -> Agreement | None:
    """Every sensor judged against the counted departure nearest the time.

    One departing hour of the same sign, even alone, confirms the reference;
    only a sensor that observed at least half its hours can say it stayed level.
    """
    start, _ = strip(anchor_ms)
    counted = [
        (r, s)
        for r in _judged(rows)
        if r.departs
        for s in r.spans
        if s.end_ms > anchor_ms - EARLY_MS
    ]
    if not counted:
        return None
    owner, span = min(
        counted,
        key=lambda c: (abs((c[1].start_ms + c[1].end_ms) / 2 - anchor_ms), -abs(c[1].median_mag)),
    )
    sign = 1 if span.median_mag > 0 else -1
    hours = range((span.start_ms - start) // HOUR_MS, (span.end_ms - start) // HOUR_MS)
    found = Agreement(span, [owner], [], [], [])
    for row in rows:
        if row is owner or row.scatter_mag is None:
            continue
        seen = [h for h in hours if row.hour_count[h] >= HOUR_MIN]
        if any(row.hour_sign[h] == sign for h in seen):
            found.agreeing.append(row)
        elif any(row.hour_sign[h] == -sign for h in seen):
            found.conflicting.append(row)
        elif 2 * len(seen) >= len(hours):
            found.level.append(row)
    judged_here = {id(r) for r in (*found.agreeing, *found.conflicting)}
    found.separate.extend((r, s) for r, s in counted if id(r) not in judged_here and r is not owner)
    return found


def _judged(rows: Sequence[SensorRow]) -> list[SensorRow]:
    """Sensors with enough comparisons both sides of the time to judge."""
    return [r for r in rows if r.scatter_mag is not None and r.after >= SIDE_MIN]


def _headline(rows: Sequence[SensorRow], cards: Sequence[Card], anchor_ms: int) -> str:
    judged = _judged(rows)
    found = _agreement(rows, anchor_ms) if judged else None
    if not judged:
        bright = "Too few observations near your time to compare any sensor with itself"
    elif found is not None:
        agree = len(found.agreeing)
        seen = agree + len(found.conflicting) + len(found.level)
        way = "fainter" if found.span.median_mag > 0 else "brighter"
        bright = (
            f"{agree} of {seen} {'sensor' if seen == 1 else 'sensors'} observing those hours "
            f"{'turns' if agree == 1 else 'turn'} {way} against "
            f"{'its' if agree == 1 else 'their'} own previous 7 days, "
            f"from {when(found.span.start_ms)}"
        )
        if found.conflicting:
            bright += f", and {len(found.conflicting)} the other way"
    else:
        bright = f"None of {len(judged)} sensors departs from its own previous 7 days"
    fitting = [c.title for c in cards if c.state == "fits"]
    if fitting:
        return f"{bright}. Fits the timing: {joined(fitting)}."
    return f"{bright}. No cause fits the timing."


def _lead(anchor_ms: int, data_end_ms: int, source: str) -> str:
    start, end = strip(anchor_ms)
    text = (
        f"72 hours from {when(start)}: each sensor against its own previous 7 days at the same "
        f"SEPA, {source} elset steps against the {ELSET_BASELINE_DAYS} days before, and every "
        "pass within reach."
    )
    if data_end_ms < end:
        text += f" Observations after your time run to {when(data_end_ms)} only."
    return text
