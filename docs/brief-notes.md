# Light Bee: interview notes (working record for the project brief)

## Batch 1 answers (Charlie, 2026-11-10)

- Vision: "Light Bee tells a space event analyst, within 3 minutes, whether a satellite's brightness change matches its own seasonal history or needs investigating as a manoeuvre or a neighbour interaction." Agreed.
- Judgement: plots plus advisory flags that show their working (option b). The analyst decides.
- Users: Bluestaq space event analysts. Products support JCO events in Mattermost as reasoning for whether a photometric change is reportable. Products must be downloadable with a banner stating the app is under test and evaluation.
- Effortless thing: drag-select on the plot opens a box listing every satellite within the screening distance (default 150 km, user-editable) and its separation at the selected time(s).
- Selection spanning several dates: list close approaches for all times and dates within the selection.
- Outputs: branded PNGs of the Seasonal Behaviour (Spectrumonetgram) product and the Other Causes stacked plots, with the test-and-evaluation banner.
- Look and feel: the dark navy style of the original Spectrumonetgram graphic.
- Success measures (accepted): cold run 3 minutes or less; repeat run or mode switch 10 seconds or less; neighbourhood scan 15 seconds or less after the drag; browser memory under about 500 MB; parity with the old app on TJS-3 (43874) plus 2 further reference objects.
- Out of scope: Residuals, Heatmap, RF and Lat/Lon tabs from the reference tool.

## Reference products

- Seasonal Behaviour: top panel VisMag v SEPA, last 15 days, coloured by days ago (plasma-like). Bottom panel 2-year heatmap, SEPA (-180 to 180) on x, date on y (recent at top), colour VisMag 6 (red) to 15 (dark blue), green average geo longitude overlay. Eclipse-season gaps near SEPA 0.
- Other Causes: Light Curve (VisMag v SEPA, 7 days, coloured by interval day 0 to 6, day 0 red); VisMag v Time (coloured by sensor, absolute vs normalised toggle, display mode selector); stacked period, inclination, RAAN time series; close approaches v time; drag-select neighbourhood scan.

## Foundations

- Baseline rehydrated from the flat bundle (20 skills, 4 agents, 2 hooks, house voice, settings, plugin manifest). CLAUDE.md at the root.
- The org skills zip holds newer, expanded versions of 16 bundle skills plus 7 extra skills (appstore-gate-compliance, deploy-recipes, toolchain-adapters, app-store-readiness, accessibility, flight-plan, working-with-claude). Stored under docs/foundations/org-skills for reference.

## Batch 2 answers (Charlie, 2026-11-10)

- Org skill versions adopted as authoritative (16 replaced, 7 added: appstore-gate-compliance, deploy-recipes, toolchain-adapters, app-store-readiness, accessibility, flight-plan, working-with-claude). Bundle-only skills kept: getting-started, glossary, configuration-and-secrets, authentication-and-authorisation.
- Change date and window are analyst-settable; default is today and the last 7 days.
- Neighbourhood scan population: GEO only, NORAD-catalogued objects only.
- Seasonal match flag: agreed (same SEPA range and day-of-year window in prior years; suppressed during longitude drift).
- Stack: Python backend agreed.
- Data strategy: "A plus" (on-demand per object, column-trimmed fetch, per-user session cache, nothing stored long-term). Option B is a documented phase 2 only.

## UDL API facts (from the supplied OpenAPI specs, UDL Services API 1.43.0)

- /udl/eoobservation/tuple and /udl/elset/tuple and /udl/elset/current/tuple exist; `columns` selects fields; classificationMarking always returned.
- /count endpoints exist for sizing a query before fetching it.
- EOObservation_Full has 106 fields. Needed: obTime, satNo, mag, magNormRange, solarEqPhaseAngle (SEPA, delivered by UDL), geolon, idSensor, dataMode, uct (about 9 of 106).
- Elset fields needed: satNo, epoch, meanMotion, eccentricity, inclination, raan, argOfPerigee, meanAnomaly, bStar, period, line1, line2.
- 429 "Rate of Requests Exceeds Allowed Limit" is a documented response: backoff required.
- Not in the specs: authentication scheme, paging parameters, rate limit values, range-filter syntax (in queryhelp). TBC, re-verify against a live queryhelp call.

## Batch 3 answers (Charlie, 2026-11-10)

- Concurrency: maximum 2 analysts per day currently.
- Magnitude: range-normalised (magNormRange) is the default.
- Filtering: dataMode REAL only. Observations are queried by satNo, so uncorrelated (UCT) observations are excluded by construction.
- Sensor country flags: not needed for V1 (sensor IDs only).
- Elsets: analyst chooses the elset provider; default KBR. Exact UDL `source` string for KBR: TBC, re-verify via the live check script.
- Classification: strictly UNCLASSIFIED. Any record whose classificationMarking is not unclassified is refused (fail closed); products are marked UNCLASSIFIED.
- UDL login: username and password, entered once per session, held in server memory only, never logged, persisted or returned to the browser.
- Testing: build against recorded fixtures shaped to the specs; Charlie runs a read-only live check script locally that reports pass/fail and timings only.
- Open: whether the App Store passes the signed-in user identity to the app. TBC, re-verify with the platform docs (appstore MCP read_doc) or the platform team.
