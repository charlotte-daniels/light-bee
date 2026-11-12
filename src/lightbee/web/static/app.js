// Light Bee page behaviour. Every value from the server is written with
// textContent or as Plotly data, never as HTML.
import {
  causesQuery, checkRunInputs, exportName, latestTime, neighbourStatus, objectLabel, progressText, scanCsv, selectionTimes,
  timesInRange, todayUtc, utc, utcInput, utcInputValue,
} from './logic.mjs';

const POLL_MS = 1500;
const PLOT_CONFIG = Object.freeze({ displayModeBar: false, responsive: true, scrollZoom: false });
const RUN_FIELDS = ['sat-no', 'change-date', 'window-days', 'provider', 'threshold'];
const SERVER_FIELDS = {
  sat_no: 'sat-no', change_date: 'change-date', window_days: 'window-days', provider: 'provider',
  threshold_km: 'threshold',
};
const $ = (id) => document.getElementById(id);

const state = {
  run: null, request: null, drawn: new Set(), marking: 'UNCLASSIFIED', scan: null, timer: null,
  today: todayUtc(), running: false,
};

class ApiError extends Error {
  constructor(status, message, fields) { super(message); this.status = status; this.fields = fields || []; }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    method: options.method || 'GET',
    headers: { 'X-Light-Bee': '1', ...(options.body ? { 'Content-Type': 'application/json' } : {}) },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  let payload = {};
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok) {
    throw new ApiError(response.status, payload.error || `Request failed (${response.status}).`, payload.fields);
  }
  return payload;
}

function basis() {
  return document.querySelector('input[name="basis"]:checked').value;
}

function runPath(suffix = '') {
  return `/api/runs/${encodeURIComponent(state.run.id)}${suffix}`;
}

function show(signedIn) {
  $('sign-in-panel').hidden = signedIn;
  $('app-panel').hidden = !signedIn;
  $('sign-out').hidden = !signedIn;
  if (!signedIn) $('username').focus();
}

function setMarking(text) {
  state.marking = text || 'UNCLASSIFIED';
  $('marking-top').textContent = state.marking;
  $('marking-bottom').textContent = state.marking;
}

function setToday(today) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(today || '')) return;
  state.today = today;
  $('change-date').max = today;
  if (!$('change-date').value || $('change-date').value > today) $('change-date').value = today;
}

async function signIn(event) {
  event.preventDefault();
  $('sign-in-error').textContent = '';
  const username = $('username').value.trim();
  const password = $('password').value;
  if (!username || !password) {
    $('sign-in-error').textContent = 'Enter your UDL username and password.';
    return;
  }
  try {
    await api('/api/session', { method: 'POST', body: { username, password } });
    $('password').value = '';
    show(true);
    $('sat-no').focus();
  } catch (error) {
    $('password').value = '';
    $('sign-in-error').textContent = error.message;
  }
}

function purgePlots() {
  for (const id of ['plot-seasonal', 'plot-causes']) Plotly.purge($(id));
}

async function signOut() {
  stopPolling();
  try { await api('/api/session', { method: 'DELETE' }); } catch { /* signed out either way */ }
  purgePlots();
  state.run = null;
  $('flag').hidden = true;
  $('progress').textContent = '';
  setRunning(false);
  setMarking('UNCLASSIFIED');
  show(false);
}

function runValues() {
  return {
    satNo: $('sat-no').value.trim(),
    changeDate: $('change-date').value,
    windowDays: $('window-days').value,
    provider: $('provider').value,
    thresholdKm: $('threshold').value,
  };
}

function clearFieldErrors() {
  for (const id of RUN_FIELDS) $(id).removeAttribute('aria-invalid');
  $('run-error').textContent = '';
}

function fieldError(field, message) {
  $('run-error').textContent = message;
  if (field && $(field)) {
    $(field).setAttribute('aria-invalid', 'true');
    $(field).setAttribute('aria-describedby', 'run-error');
    $(field).focus();
  }
}

function setRunning(running) {
  state.running = running;
  // Kept focusable while a run goes, so keyboard focus is never lost.
  $('run-button').setAttribute('aria-disabled', String(running));
}

async function startRun(event) {
  event.preventDefault();
  if (state.running) {
    $('run-error').textContent = 'A run is in progress; it finishes before a new one starts.';
    return;
  }
  clearFieldErrors();
  const values = runValues();
  const problem = checkRunInputs(values, state.today);
  if (problem) { fieldError(problem.field, problem.message); return; }
  const body = {
    sat_no: Number(values.satNo), change_date: values.changeDate, window_days: Number(values.windowDays),
    provider: values.provider, threshold_km: Number(values.thresholdKm),
  };
  try {
    stopPolling();
    state.drawn = new Set();
    state.request = body;
    purgePlots();
    for (const id of ['export-seasonal', 'export-causes', 'scan-button']) $(id).disabled = true;
    $('flag').hidden = true;
    state.run = await api('/api/runs', { method: 'POST', body });
    setRunning(true);
    poll();
  } catch (error) {
    if (error.status === 401) { show(false); return; }
    fieldError(SERVER_FIELDS[error.fields[0]], error.message);
  }
}

function stopPolling() {
  if (state.timer) clearTimeout(state.timer);
  state.timer = null;
}

async function poll() {
  if (!state.run) return;
  let finished = false;
  try {
    const run = await api(runPath());
    state.run = run;
    finished = run.stage === 'done' || run.stage === 'failed';
    $('progress').textContent = progressText(run);
    await drawWhatIsReady(run);
  } catch (error) {
    if (error.status === 401) { setRunning(false); show(false); return; }
    // A 409 while the run goes means a stage was announced a moment before
    // its result: poll again. Once the run has finished, nothing more is
    // coming, so any refusal is shown and polling stops.
    if (error.status !== 409 || finished) {
      $('progress').textContent = error.message;
      setRunning(false);
      return;
    }
  }
  if (finished) {
    setRunning(false);
    $('run-error').textContent = '';
    if (state.run.stage === 'failed' && state.drawn.has('flag')) {
      $('progress').textContent += ' The flag above stands; the rest could not be completed.';
    }
    return;
  }
  state.timer = setTimeout(poll, POLL_MS);
}

async function drawWhatIsReady(run) {
  const ready = new Set(run.ready);
  if (ready.has('recent') && !state.drawn.has('recent')) {
    await drawSeasonal();
    state.drawn.add('recent');
  }
  if (ready.has('flag') && !state.drawn.has('flag')) {
    await drawSeasonal();
    await drawFlag();
    state.drawn.add('flag');
  }
  if (ready.has('seasonal') && !state.drawn.has('seasonal')) {
    await drawSeasonal();
    await drawFlag();
    state.drawn.add('seasonal');
  }
  if (run.stage === 'done' && !state.drawn.has('done')) {
    await drawCauses();
    state.drawn.add('done');
    $('scan-button').disabled = false;
    presetScanRange();
  }
}

// Plotly sizes a figure to its container when drawn; a figure drawn into a
// hidden tab would be drawn at a default width, so it is given the width the
// tab area actually has, and is resized whenever its tab is shown.
function width() {
  return Math.max(800, $('app-panel').clientWidth || 1200);
}

async function draw(plotId, figure) {
  const plot = $(plotId);
  // A figure with a set width (the portrait Seasonal plot) keeps it.
  figure.layout.width = figure.layout.width || width();
  await Plotly.react(plot, figure.data, figure.layout, PLOT_CONFIG);
  // The figure is described by the container's label; the SVG detail is hidden.
  plot.setAttribute('aria-label', figure.summary || 'Figure');
  for (const svg of plot.querySelectorAll('svg')) svg.setAttribute('aria-hidden', 'true');
}

async function drawSeasonal() {
  const figure = await api(runPath(`/figures/seasonal?basis=${encodeURIComponent(basis())}`));
  setMarking(markingOf(figure));
  await draw('plot-seasonal', figure);
  $('export-seasonal').disabled = false;
}

function markingOf(figure) {
  const first = (figure.layout.annotations || [])[0];
  return first ? first.text : 'UNCLASSIFIED';
}

async function drawFlag() {
  const flag = await api(runPath(`/flag?basis=${encodeURIComponent(basis())}`));
  $('flag-headline').textContent = flag.headline;
  $('flag-detail').textContent = flag.detail;
  $('flag').dataset.outcome = flag.outcome;
  const rows = $('flag-bins');
  rows.replaceChildren();
  flag.bins.sepa_centre.forEach((sepa, index) => {
    const row = document.createElement('tr');
    for (const value of [sepa, flag.bins.recent[index], flag.bins.reference[index], flag.bins.robust_z[index]]) {
      cell(row, value === null ? '' : String(value));
    }
    rows.append(row);
  });
  $('flag').hidden = false;
}

// Each Other Causes request is numbered, so a slow reply to an earlier toggle
// never draws over a newer one.
let causesRequest = 0;

async function drawCauses() {
  causesRequest += 1;
  const mine = causesRequest;
  const figure = await api(runPath(causesQuery(basis(), $('ghost-years').checked)));
  if (mine !== causesRequest) return;
  const plot = $('plot-causes');
  await draw('plot-causes', figure);
  $('causes-summary').textContent = figure.summary || '';
  // Re-attached after every draw, never remembered by a flag: Plotly drops a
  // plot's listeners whenever it purges it (a new run, sign-out, or a redraw
  // it turns into a full replot), and 1.0 to 1.1 then ignored drag selections.
  plot.removeListener('plotly_selected', selected);
  plot.on('plotly_selected', selected);
  $('export-causes').disabled = false;
}

// The keyboard scan starts on the six hours up to the latest observation,
// so its first use always finds data.
function presetScanRange() {
  const latest = latestTime($('plot-causes').data);
  if (latest === null) return;
  const end = Math.floor(latest / 60000) * 60000;
  $('scan-from').value = utcInputValue(end - 6 * 3600000);
  $('scan-to').value = utcInputValue(end);
}

function scanError(field, message) {
  for (const id of ['scan-from', 'scan-to']) $(id).removeAttribute('aria-invalid');
  $('scan-error').textContent = message;
  if (field) {
    $(field).setAttribute('aria-invalid', 'true');
    $(field).setAttribute('aria-describedby', 'scan-error');
    $(field).focus();
  }
}

function scanThreshold() {
  const km = Number($('threshold').value);
  return Number.isFinite(km) && km >= 1 && km <= 2000 ? km : null;
}

async function scanRange(event) {
  event.preventDefault();
  scanError(null, '');
  const from = utcInput($('scan-from').value);
  const to = utcInput($('scan-to').value);
  if (!Number.isFinite(from)) { scanError('scan-from', 'Choose a start time (UTC).'); return; }
  if (!Number.isFinite(to) || to < from) {
    scanError('scan-to', 'Choose an end time after the start (UTC).');
    return;
  }
  const times = timesInRange($('plot-causes').data, from, to + 59999);
  if (!times.length) {
    scanError('scan-from', 'There are no observations in that range; widen it.');
    return;
  }
  await scan(times);
}

// A drag selection: scanned, or a stated reason why not, never silence.
async function selected(event) {
  if (!Array.isArray(event?.points)) return;
  const times = selectionTimes(event);
  if (!times.length) {
    $('progress').textContent = 'No observations in that selection; drag over the points to scan.';
    return;
  }
  await scan(times);
}

async function scan(times) {
  if (!times.length || state.run?.stage !== 'done') return;
  const threshold = scanThreshold();
  if (threshold === null) { fieldError('threshold', 'The screening distance must be 1 to 2,000 km.'); return; }
  $('progress').textContent = `Scanning ${times.length.toLocaleString('en-GB')} selected observations…`;
  try {
    const result = await api(runPath('/scan'), { method: 'POST', body: { times_ms: times, threshold_km: threshold } });
    state.scan = result;
    // The scan may have read names too: the page and the CSV carry its marking.
    if (typeof result.marking === 'string' && result.marking) setMarking(result.marking);
    showScan(result);
    $('progress').textContent = progressText(state.run);
  } catch (error) {
    $('progress').textContent = `Scan failed: ${error.message}`;
  }
}

function cell(row, text) {
  const td = document.createElement('td');
  td.textContent = text;
  row.append(td);
  return td;
}

function showScan(result) {
  const first = result.times_ms[0];
  const last = result.times_ms[result.times_ms.length - 1];
  const found = result.neighbours.length + result.more;
  $('scan-summary').textContent = `${found} object${found === 1 ? '' : 's'} within ${result.threshold_km} km `
    + `(plus the ${result.uncertainty_km} km propagation uncertainty) at some point between ${utc(first)} and ${utc(last)}, `
    + `across ${result.times_ms.length.toLocaleString('en-GB')} minutes scanned; ${result.screened} GEO objects screened.`
    + (result.thinned ? ` The selection was thinned evenly to ${result.max_times.toLocaleString('en-GB')} instants.` : '')
    + (result.more ? ` The nearest ${result.neighbours.length} are listed.` : '');
  const rows = $('scan-rows');
  rows.replaceChildren();
  const details = $('scan-details');
  details.replaceChildren();
  for (const neighbour of result.neighbours) {
    const row = document.createElement('tr');
    cell(row, String(neighbour.sat_no));
    // A muted marker, not a blank: the UDL gave no name, or one not fit to show.
    if (!neighbour.name) cell(row, 'No name').classList.add('unknown');
    else cell(row, neighbour.name);
    cell(row, neighbour.min_km.toFixed(1));
    cell(row, utc(neighbour.min_at_ms));
    cell(row, neighbourStatus(neighbour));
    cell(row, neighbour.elset_age_hours.toFixed(1));
    rows.append(row);
    details.append(separationDetails(result, neighbour));
  }
  $('scan-dialog').showModal();
}

function separationDetails(result, neighbour) {
  const block = document.createElement('details');
  const summary = document.createElement('summary');
  summary.textContent = `${objectLabel(neighbour)}: separation at every selected time`;
  block.append(summary);
  block.addEventListener('toggle', () => {
    if (!block.open || block.dataset.filled) return;
    const table = document.createElement('table');
    table.className = 'data';
    const body = document.createElement('tbody');
    result.times_ms.forEach((time, index) => {
      const row = document.createElement('tr');
      const km = neighbour.separations_km[index];
      cell(row, utc(time));
      cell(row, km === null ? 'no position' : `${km.toFixed(1)} km`);
      body.append(row);
    });
    table.append(body);
    block.append(table);
    block.dataset.filled = 'yes';
  });
  return block;
}

function downloadScanCsv() {
  if (!state.scan) return;
  const blob = new Blob([scanCsv(state.scan, state.marking)], { type: 'text/csv' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = `${exportName(state.request.sat_no, 'scan', state.request.change_date)}.csv`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

function exportPng(plotId, mode) {
  const plot = $(plotId);
  if (!plot.layout) return;
  // Twice the pixel density, so the marking still reads in a chat preview.
  Plotly.downloadImage(plot, {
    format: 'png', width: plot.layout.width || 1600, height: plot.layout.height || 1450, scale: 2,
    filename: exportName(state.request.sat_no, mode, state.request.change_date),
  });
}

function selectTab(tab) {
  for (const other of document.querySelectorAll('[role="tab"]')) {
    const selected = other === tab;
    other.setAttribute('aria-selected', String(selected));
    other.tabIndex = selected ? 0 : -1;
    $(other.getAttribute('aria-controls')).hidden = !selected;
  }
  tab.focus();
  // Only Other Causes follows the page width; Seasonal is portrait at a fixed width.
  if ($('plot-causes').layout) Plotly.relayout($('plot-causes'), { width: width() });
}

function tabKeys(event) {
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  const index = tabs.indexOf(event.target);
  if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
    const step = event.key === 'ArrowRight' ? 1 : -1;
    selectTab(tabs[(index + step + tabs.length) % tabs.length]);
    event.preventDefault();
  }
}

async function basisChanged() {
  if (!state.run || !state.drawn.has('seasonal')) return;
  try {
    await drawSeasonal();
    await drawFlag();
    if (state.drawn.has('done')) await drawCauses();
  } catch (error) {
    $('progress').textContent = error.message;
  }
}

// Ghost years are redrawn from the history the run already holds; the UDL is
// not read again. The choice stays for every later draw and run.
async function ghostsChanged() {
  if (!state.run || !state.drawn.has('done')) return;
  try {
    await drawCauses();
    // The live status line tells a screen reader the redraw happened.
    $('progress').textContent = $('ghost-years').checked
      ? 'Ghost years on: the same week in earlier years is drawn beneath the light curve.'
      : 'Ghost years off.';
  } catch (error) {
    $('progress').textContent = error.message;
  }
}

window.addEventListener('resize', () => {
  const plot = $('plot-causes');
  if (plot.layout && !plot.closest('[hidden]')) Plotly.relayout(plot, { width: width() });
});

async function init() {
  setToday(todayUtc());
  $('sign-in-form').addEventListener('submit', signIn);
  $('sign-out').addEventListener('click', signOut);
  $('run-form').addEventListener('submit', startRun);
  $('scan-form').addEventListener('submit', scanRange);
  $('export-seasonal').addEventListener('click', () => exportPng('plot-seasonal', 'seasonal'));
  $('export-causes').addEventListener('click', () => exportPng('plot-causes', 'other-causes'));
  $('scan-close').addEventListener('click', () => $('scan-dialog').close());
  $('scan-csv').addEventListener('click', downloadScanCsv);
  for (const id of RUN_FIELDS) $(id).addEventListener('input', () => $(id).removeAttribute('aria-invalid'));
  for (const tab of document.querySelectorAll('[role="tab"]')) {
    tab.addEventListener('click', () => selectTab(tab));
    tab.addEventListener('keydown', tabKeys);
  }
  for (const radio of document.querySelectorAll('input[name="basis"]')) {
    radio.addEventListener('change', basisChanged);
  }
  $('ghost-years').addEventListener('change', ghostsChanged);
  try {
    const session = await api('/api/session');
    setToday(session.today);
    show(session.signed_in);
  } catch {
    show(false);
  }
}

await init();
