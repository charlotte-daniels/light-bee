// Pure helpers for the Light Bee page: no DOM, no network, so Node can test them.

export const STAGES = Object.freeze({
  queued: 'Waiting to start',
  recent: 'Reading the recent weeks of photometry',
  seasons: 'Reading the same season in earlier years for the flag',
  history: 'Flag ready; reading the rest of two years of history for the heatmap',
  elsets: 'Reading the target’s elsets',
  catalogue: 'Reading the GEO snapshot and screening close approaches',
  done: 'Done',
  failed: 'Failed',
});

export const MAX_SELECTED = 50000;

/** One line describing a run's progress, for the live status region. */
export function progressText(state) {
  if (!state || typeof state.stage !== 'string') return '';
  const label = STAGES[state.stage] ?? 'Working';
  if (state.stage === 'failed') return `Failed: ${state.message || 'the run did not complete.'}`;
  if (state.stage === 'done') {
    const warm = state.warm ? ' (from this session’s cache)' : '';
    return `Done in ${formatSeconds(state.seconds)}${warm}.`;
  }
  const records = Number.isFinite(state.records) && state.records > 0
    ? `, ${state.records.toLocaleString('en-GB')} observations so far` : '';
  return `${label}${records} (${formatSeconds(state.seconds)}).`;
}

export function formatSeconds(seconds) {
  const value = Number.isFinite(seconds) ? seconds : 0;
  if (value < 60) return `${value.toFixed(0)} s`;
  return `${Math.floor(value / 60)} min ${Math.round(value % 60)} s`;
}

/** The distinct observation times (ms) carried by a Plotly selection, oldest first. */
export function selectionTimes(event) {
  if (!event || !Array.isArray(event.points)) return [];
  const times = new Set();
  for (const point of event.points) {
    const value = point?.customdata;
    if (Number.isInteger(value) && value >= 0) times.add(value);
  }
  return [...times].sort((a, b) => a - b).slice(0, MAX_SELECTED);
}

/** A UTC timestamp as 2026-11-10 03:04:05Z. */
export function utc(ms) {
  if (!Number.isFinite(ms)) return '';
  return new Date(ms).toISOString().replace('T', ' ').replace(/\.\d{3}Z$/, 'Z');
}

/** Today's date in UTC as YYYY-MM-DD, the change date default. */
export function todayUtc(now = new Date()) {
  return now.toISOString().slice(0, 10);
}

/** The scan as CSV: one row per neighbour per selected time. */
export function scanCsv(scan, marking) {
  const lines = [`# ${csvSafe(marking)}`, 'norad_id,time_utc,separation_km,within_threshold'];
  for (const neighbour of scan.neighbours || []) {
    scan.times_ms.forEach((time, index) => {
      const km = neighbour.separations_km[index];
      const within = km !== null && km <= scan.threshold_km ? 'yes' : 'no';
      lines.push([neighbour.sat_no, utc(time), km === null ? '' : km, within].join(','));
    });
  }
  return lines.join('\n') + '\n';
}

/** Text placed in a CSV cell cannot start a formula in a spreadsheet. */
export function csvSafe(text) {
  const value = String(text ?? '').replace(/[\r\n,]/g, ' ');
  return /^[=+\-@\t]/.test(value) ? `'${value}` : value;
}

/** How a neighbour is described in the list. */
export function neighbourStatus(neighbour) {
  if (neighbour.inside && neighbour.borderline) return 'Inside (near the threshold)';
  if (neighbour.inside) return 'Inside';
  if (neighbour.borderline) return 'Outside (near the threshold)';
  return 'Outside';
}

/** Validates the run form; returns { field, message } for the first problem, or null. */
export function checkRunInputs(values, today) {
  const satNo = Number(values.satNo);
  if (!/^\d{1,5}$/.test(String(values.satNo).trim()) || satNo < 1 || satNo > 99999) {
    return { field: 'sat-no', message: 'Enter a NORAD ID from 1 to 99999.' };
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(values.changeDate) || values.changeDate > today) {
    return { field: 'change-date', message: `Choose a change date no later than today (${today}, UTC).` };
  }
  const window = Number(values.windowDays);
  if (!Number.isInteger(window) || window < 1 || window > 31) {
    return { field: 'window-days', message: 'The window must be 1 to 31 days.' };
  }
  const km = Number(values.thresholdKm);
  if (!Number.isFinite(km) || km < 1 || km > 2000) {
    return { field: 'threshold', message: 'The screening distance must be 1 to 2,000 km.' };
  }
  return null;
}

/** A datetime-local value ("2026-11-10T03:04") read as UTC milliseconds, or NaN. */
export function utcInput(value) {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/.test(String(value))) return Number.NaN;
  return Date.parse(`${value}${value.length === 16 ? ':00' : ''}Z`);
}

/** Milliseconds as a datetime-local value in UTC. */
export function utcInputValue(ms) {
  return new Date(ms).toISOString().slice(0, 16);
}

/** Every observation time in a figure's traces that falls in [from, to], oldest first. */
export function timesInRange(traces, from, to) {
  const times = new Set();
  for (const trace of traces || []) {
    for (const value of trace.customdata || []) {
      if (Number.isInteger(value) && value >= from && value <= to) times.add(value);
    }
  }
  return [...times].sort((a, b) => a - b).slice(0, MAX_SELECTED);
}

/** The latest observation time (ms) in a figure's traces, or null if there is none. */
export function latestTime(traces) {
  let latest = null;
  for (const trace of traces || []) {
    for (const value of trace.customdata || []) {
      if (Number.isInteger(value) && (latest === null || value > latest)) latest = value;
    }
  }
  return latest;
}

/** The Other Causes figure path for a magnitude basis, with ghost years on or off. */
export function causesQuery(basis, ghosts) {
  const safeBasis = basis === 'raw' ? 'raw' : 'normalised';
  return `/figures/causes?basis=${safeBasis}&ghosts=${ghosts === true}`;
}

/** A file-name-safe stem for exports. */
export function exportName(satNo, mode, changeDate) {
  const safeMode = String(mode).replace(/[^a-z-]/gi, '').toLowerCase();
  return `lightbee-${Number(satNo)}-${safeMode}-${String(changeDate).replace(/[^0-9-]/g, '')}`;
}
