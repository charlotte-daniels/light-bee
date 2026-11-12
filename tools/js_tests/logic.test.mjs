// node --test tools/js_tests/*.test.mjs  (never shipped)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  causesQuery, checkRunInputs, csvSafe, exportName, formatSeconds, latestTime, neighbourStatus, objectLabel, progressText,
  scanCsv,
  selectionTimes, timesInRange, todayUtc, utc, utcInput, utcInputValue,
} from '../../src/lightbee/web/static/logic.mjs';

test('progress text names the stage and never shows raw values as HTML', () => {
  assert.equal(progressText({ stage: 'history', records: 12345, seconds: 75 }),
    'Flag ready; reading the rest of two years of history for the heatmap, 12,345 observations so far (1 min 15 s).');
  assert.equal(progressText({ stage: 'seasons', records: 0, seconds: 5 }),
    'Reading the same season in earlier years for the flag (5 s).');
  assert.equal(progressText({ stage: 'done', seconds: 4, warm: true }),
    'Done in 4 s (from this session’s cache).');
  assert.equal(progressText({ stage: 'failed', message: 'The UDL read failed.' }), 'Failed: The UDL read failed.');
  assert.equal(progressText({ stage: 'failed' }), 'Failed: the run did not complete.');
  assert.equal(progressText(null), '');
  assert.equal(progressText({ stage: 'odd', seconds: 1 }), 'Working (1 s).');
  assert.equal(formatSeconds(undefined), '0 s');
});

test('a selection yields distinct whole-number times, oldest first, capped', () => {
  const event = { points: [{ customdata: 30 }, { customdata: 10 }, { customdata: 30 }, { customdata: 'x' },
    { customdata: -1 }, { customdata: 1.5 }, {}, null] };
  assert.deepEqual(selectionTimes(event), [10, 30]);
  assert.deepEqual(selectionTimes(undefined), []);
  const many = { points: Array.from({ length: 60000 }, (_, i) => ({ customdata: i })) };
  assert.equal(selectionTimes(many).length, 50000);
});

test('times are shown in UTC and today is the UTC date', () => {
  assert.equal(utc(Date.UTC(2026, 10, 10, 3, 4, 5, 678)), '2026-11-10 03:04:05Z');
  assert.equal(utc(NaN), '');
  assert.equal(todayUtc(new Date(Date.UTC(2026, 10, 10, 23, 59))), '2026-11-10');
});

test('the run form is checked before anything is sent, naming the field', () => {
  const good = { satNo: '43874', changeDate: '2026-11-10', windowDays: '7', thresholdKm: '150' };
  assert.equal(checkRunInputs(good, '2026-11-10'), null);
  assert.equal(checkRunInputs({ ...good, satNo: '100000' }, '2026-11-10').field, 'sat-no');
  assert.equal(checkRunInputs({ ...good, satNo: '4.5' }, '2026-11-10').field, 'sat-no');
  assert.equal(checkRunInputs({ ...good, satNo: '1e3' }, '2026-11-10').field, 'sat-no');
  const late = checkRunInputs({ ...good, changeDate: '2026-11-11' }, '2026-11-10');
  assert.equal(late.field, 'change-date');
  assert.match(late.message, /2026-11-10, UTC/);
  assert.equal(checkRunInputs({ ...good, windowDays: '32' }, '2026-11-10').field, 'window-days');
  assert.equal(checkRunInputs({ ...good, thresholdKm: '0' }, '2026-11-10').field, 'threshold');
});

test('a keyboard range picks the observation times the figure carries', () => {
  assert.equal(utcInput('2026-11-10T03:04'), Date.UTC(2026, 10, 10, 3, 4));
  assert.equal(utcInput('2026-11-10T03:04:05'), Date.UTC(2026, 10, 10, 3, 4, 5));
  assert.ok(Number.isNaN(utcInput('10/11/2026')));
  assert.equal(utcInputValue(Date.UTC(2026, 10, 10, 3, 4, 59)), '2026-11-10T03:04');
  const traces = [{ customdata: [5, 10, 15] }, { customdata: [10, 20, 'x'] }, {}];
  assert.deepEqual(timesInRange(traces, 10, 20), [10, 15, 20]);
  assert.deepEqual(timesInRange(undefined, 0, 1), []);
  assert.equal(latestTime(traces), 20);
  assert.equal(latestTime([{ x: [1] }]), null);
  assert.equal(latestTime(undefined), null);
});

test('the scan CSV carries the marking, cannot start a formula, and lists every time', () => {
  const scan = { times_ms: [0, 60000], threshold_km: 150,
    neighbours: [
      { sat_no: 40001, name: 'TJS-3', separations_km: [100.5, null] },
      { sat_no: 40002, name: '=cmd,x', separations_km: [90] },
      { sat_no: 40003, separations_km: [80] },
    ] };
  const csv = scanCsv(scan, 'UNCLASSIFIED//PR-MRKT');
  assert.deepEqual(csv.trim().split('\n'), [
    '# UNCLASSIFIED//PR-MRKT', 'norad_id,name,time_utc,separation_km,within_threshold',
    '40001,TJS-3,1970-01-01 00:00:00Z,100.5,yes', '40001,TJS-3,1970-01-01 00:01:00Z,,no',
    // A name cannot start a formula or add a column; a missing one is an empty cell.
    "40002,'=cmd x,1970-01-01 00:00:00Z,90,yes", '40002,\'=cmd x,1970-01-01 00:01:00Z,,no',
    '40003,,1970-01-01 00:00:00Z,80,yes', '40003,,1970-01-01 00:01:00Z,,no',
  ]);
  assert.equal(objectLabel({ sat_no: 40001, name: 'TJS-3' }), 'NORAD 40001 (TJS-3)');
  assert.equal(objectLabel({ sat_no: 40003 }), 'NORAD 40003');
  assert.equal(objectLabel({ sat_no: 40003, name: 7 }), 'NORAD 40003');
  assert.equal(csvSafe('=HYPERLINK("x")'), '\'=HYPERLINK("x")');
  assert.equal(csvSafe('a,b\nc'), 'a b c');
  assert.equal(csvSafe(undefined), '');
});

test('neighbours are described against the threshold and exports get safe names', () => {
  assert.equal(neighbourStatus({ inside: true, borderline: true }), 'Inside (near the threshold)');
  assert.equal(neighbourStatus({ inside: true, borderline: false }), 'Inside');
  assert.equal(neighbourStatus({ inside: false, borderline: true }), 'Outside (near the threshold)');
  assert.equal(neighbourStatus({ inside: false, borderline: false }), 'Outside');
  assert.equal(exportName(43874, 'other-causes', '2026-11-10'), 'lightbee-43874-other-causes-2026-11-10');
  assert.equal(exportName('1;rm', '../x', '2026/11/10<'), 'lightbee-NaN-x-20261110');
});

test('the Other Causes path carries the basis and the ghost years choice, nothing else', () => {
  assert.equal(causesQuery('normalised', false), '/figures/causes?basis=normalised&ghosts=false');
  assert.equal(causesQuery('raw', true), '/figures/causes?basis=raw&ghosts=true');
  assert.equal(causesQuery('raw&x=1', 'true'), '/figures/causes?basis=normalised&ghosts=false');
  assert.equal(causesQuery(undefined, 1), '/figures/causes?basis=normalised&ghosts=false');
});
