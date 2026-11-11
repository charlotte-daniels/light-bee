# Light Bee: project brief

Bluestaq Limited. Version 1.0, 10 November 2026. Owner: Charlie Daniels. Classification of this document and of the application: UNCLASSIFIED.

**Archetype: server** (a container running a Python process). **Language: Python 3.12** (FastAPI) with a thin JavaScript interface. **App Store template: `python`** (Dockerfile and `requirements.txt` at the archive root). **Target: `light-bee.apps.bluestaq.com`, deployed by Friday 13 November 2026.**

## 1. Vision and single job

**One sentence.** Light Bee tells a space event analyst, within 3 minutes, whether a satellite's brightness change matches its own seasonal history or needs investigating as a manoeuvre or a neighbour interaction.

**Single job.** Turn a NORAD ID and a change window into two evidence products, "Seasonal Behaviour?" and "Other Causes", that let the analyst decide whether a photometric change is reportable.

**Why a rebuild, not a repair.** The predecessor (SpectruMonetGram 2.0.3) proved the analysis but lost the deployment to its chassis. Its Streamlit build passed all ten pipeline stages and was then unreachable, because the App Store ingress does not forward websockets. Its first cold read of 2 years took about 7 minutes, 88% of it waiting on the UDL, because it downloaded every field of every record. Light Bee keeps the proven science, discards the chassis, and is designed around the two facts that sank it.

## 2. Users and experience

**Primary user.** A Bluestaq space event analyst on watch, investigating one flagged GEO object at a time, under time pressure. At most 2 analysts a day today.

**Secondary users.** JCO (Joint Commercial Operations) event participants in Mattermost, who see Light Bee's exported PNGs as the reasoning for whether a photometric change is reported. They never open the app, so every export must stand on its own: title, object, window, data source, generation time, classification, and a banner stating the app is under test and evaluation.

**The one effortless thing.** Drag a box on the light curve or the VisMag v Time panel and immediately see every GEO object within the screening distance (default 150 km, analyst-editable), its separation at each selected time, and how old the elset behind that number is. A selection spanning several dates lists the close approaches for every time within it.

**Look and feel.** The dark navy instrument look of the original Spectrumonetgram: deep navy chrome (`#162646`), black plot fields, Bluestaq blues for controls (`#385FAF`, `#739BCF`), green (`#27AE60`) for the longitude trace and healthy states. The product title moves from copper-amber (documents only) to `#739BCF`. Square corners, Segoe UI, monospace only for stamps (NORAD ID, version, UTC times). Motion only where it carries meaning: the progress of a run, and the selection box. The interface reads as instrumentation, never as a consumer app.

**Reference colour scales carried over.** Heatmap: VisMag 6 (dark red, bright) through white (about 10.5) to 15 (dark blue, faint), black for no data. Recent panel: plasma by days ago. Light curve: by interval day, day 0 red, older days fading blues.

## 3. Outcomes and success measures

The transformation: an analyst moves from "this object changed, I need an hour with two years of data" to "here is the evidence, decided and exported" in a few minutes.

Success, stated observably (all accepted by the owner):

| Measure | Target | How it is measured |
|---|---|---|
| First run on a new object | 3 minutes or less | Live check script, wall clock from Run to both modes rendered |
| Repeat run or mode switch | 10 seconds or less | Live check script, second run same object same session |
| Neighbourhood scan after a drag | 15 seconds or less | Live check script and browser smoke test with fixtures |
| Browser memory | under about 500 MB | Browser smoke test, `performance.memory` on a dense object |
| Parity with the old app | Matches on TJS-3 (43874), TJS-10 (58204), TJS-15 (63157) | Same object, same window: verdict agrees, heatmap structure (eclipse gaps, drift step, partial history) visibly the same |
| Exports fit for JCO use | Owner confirms | Owner review of the PNGs |

## 4. Scope and journeys

**Core journey, in order.**

1. Sign in to the App Store and open Light Bee.
2. Enter UDL username and password once for the session.
3. Enter a NORAD ID. The change date defaults to today and the window to the last 7 days; both are editable. Choose the elset provider (default KBR).
4. Press Run. "Seasonal Behaviour?" renders as soon as it is ready; Other Causes fills in behind it. A progress line reports what is being read and how long it has taken.
5. Read the seasonal flag on "Seasonal Behaviour?": consistent, offset within scatter, departs, or inconclusive, with its working shown, and suppressed with a stated reason while the longitude trace shows drift.
6. Switch to Other Causes: light curve (with ghost years beneath), VisMag v Time by sensor, stacked period, inclination and RAAN, and close approaches v time.
7. Drag to select a change; read the neighbour list; adjust the screening distance if needed.
8. Export the "Seasonal Behaviour?" PNG and the Other Causes stacked PNG.
9. Sign out, or leave; the session and its data expire after 8 hours.

**In scope (V1).** Both modes; range-normalised magnitude (`magNormRange`) by default with a raw magnitude toggle; REAL data only; analyst-selected elset provider; editable change date, window and screening distance; drag-select neighbourhood scan over GEO NORAD-catalogued objects; ghost years; branded PNG export with the test and evaluation banner.

**Explicitly out of scope.** Residuals, Heatmap, RF and Lat/Lon tabs from the reference tool; sensor country flags; non-GEO screening; objects without a NORAD ID; any write to the UDL; any classified data; a shared or persistent observation store (Option B, documented as phase 2 only); conjunction assessment (Light Bee finds neighbours, it does not assess collision risk); a stated verdict with a confidence level (option c).

## 5. Archetype and stack

**Server, because** Light Bee must call the UDL with the analyst's credentials, and the static archetype forbids all network egress and holds no secret. A static artifact cannot do this job.

**Stack.**

* Python 3.12, FastAPI, uvicorn with exactly one worker (credentials and results live in process memory; a second worker would sign analysts out at random).
* `httpx` for the UDL client, `numpy` for binning and statistics, `sgp4` (`SatrecArray`) for vectorised propagation. No pandas, no matplotlib, no Pillow: fewer packages means fewer container scan findings, and the old app spent a cycle on 91 of them.
* Interface: one static HTML page, one small JavaScript module, and a vendored, version-pinned Plotly.js served by the app itself (no CDN). Python builds every figure as Plotly JSON, so the chart logic is Python and is tested in Python. The JavaScript only renders, relays selections, and exports. PNG export is done in the browser by Plotly, so the server needs no imaging stack.
* No websocket. The browser polls for run progress over plain HTTP, the pattern proven on this platform.

**Deployment target.** Bluestaq App Store, `python` template, port 8080 on `0.0.0.0`, non-root, flattened hardened image, no add-ons, memory request 2Gi (headroom over the measured need; see section 7).

## 6. Structure

```
light-bee/
  Dockerfile  requirements.txt  requirements-runtime.txt  pyproject.toml  sonar-project.properties
  src/
    asgi.py                    entry point the container runs
    lightbee/
      config.py                environment-only settings with validated defaults
      udl/client.py            read-only client: GET only, path allowlist, no redirects, backoff on 429
      udl/queries.py           column-trimmed observation and elset queries, time slicing
      analysis/seasonal.py     SEPA by date grid, seasonal flag, drift detection
      analysis/lightcurve.py   light curve, VisMag v Time, ghost years
      analysis/orbit.py        period, inclination and RAAN series from elsets
      analysis/screen.py       GEO neighbourhood screen (vectorised SGP4)
      figures/                 Plotly figure JSON builders and the shared theme
      web/app.py               create_app(deps): routes, health, sessions, run jobs
      web/session.py           per-analyst session: credentials, cache, expiry
      web/static/              index.html, app.js, app.css, vendor/plotly.min.js
  tests/                       pytest suite, fixtures shaped to the UDL specs
  tools/                       live check script, pipeline simulation, gates, packager
  docs/                        brief, build plan, security policy, changelog, runbook
```

**Where data and state live.** Nothing is written to disk. Each analyst session holds, in server memory only: the UDL credentials, the trimmed observation arrays and elsets for the objects run in that session, and the finished figure JSON. Sessions are keyed by a random, HttpOnly, SameSite=Strict cookie, capped in number, and wiped at sign-out or after 8 hours idle. The browser holds only what it is drawing. This is option "A plus".

**Run flow.** Run starts one single-flight background job per session and object. It reads the recent window first and renders the top panel, then the history in parallel slices, then the elsets for the orbital panels and a GEO catalogue snapshot for screening. The drag scan reuses the prefetched snapshot, so it is propagation only and never waits on the UDL.

## 7. Data and integrations

**One integration: the Unified Data Library, read-only.** Every request is a GET with an empty body to an allowlisted path under the analyst-supplied base URL default `https://unifieddatalibrary.com`, over HTTPS, with redirects refused. A test records every request a full run makes and fails on any other verb, body, host or path.

| Data | UDL endpoint | Columns requested | Notes |
|---|---|---|---|
| Photometry | `/udl/eoobservation/history` (archive) and live collection for the latest hours, with `columns=` | `obTime, mag, magNormRange, georange, solarEqPhaseAngle, geolon, idSensor, dataMode` | 8 of 106 fields. `dataMode=REAL`. Queried by `satNo`, so uncorrelated tracks are excluded by construction |
| Elsets, target | `/udl/elset/tuple` | `satNo, epoch, source, dataMode, ephemType`, and the SGP4 mean elements `meanMotion, eccentricity, inclination, raan, argOfPerigee, meanAnomaly, bStar, meanMotionDot, meanMotionDDot, period` | Provider chosen by the analyst, default `KBR` (the predecessor's verified source string). `dataMode=REAL`; SGP4 elsets (`ephemType` 0) only. `line1` and `line2` cannot be selected on the tuple endpoint (live check, 10 November 2026), so SGP4 is initialised from the elements. An absent drag term (`bStar`, `meanMotionDot`, `meanMotionDDot`) is taken as zero, negligible at GEO; an absent `period` is derived from `meanMotion`. A present but invalid value still refuses the elset, and the field is counted. The tuple endpoint returns the `satNo` column under the key `onOrbit.satNo`, alongside `idOnOrbit` (the catalogue number as text for the public catalogue). Every identity key present must parse and agree, and agree with the object a per-object read asked for; only when no key is present does a per-object read take the number it filtered on. An elset whose epoch is outside the window asked for is refused |
| GEO catalogue snapshot | `/udl/elset/tuple`, a 12 hour epoch slice around the window | as above, plus `idOnOrbit` | Kept: mean motion 0.9 to 1.1 rev/day. Screened: eccentricity 0.1 or less (which bounds relative speed, so a 5 minute sweep cannot step over a pass) and within 3 degrees of the target's inclination; more than 3,000 candidates is refused as not a GEO snapshot |

**Volumes, measured by the predecessor against the live service.** Dense GEO objects carry about 1,000 to 1,700 observations a day: 50321 returned 1.06 million records over 2 years, 55131 returned 1.22 million. The service is record-bound (about 2,800 records a second at 24 requests in flight, all fields); the best slice is about 7,000 records; `maxResults` accepts at least 50,000; there is no offset, so windows are divided, not paged; bounded time filters use `start..end`; `/count` is too slow to size a read (70 seconds once), so a recent probe slice sizes it instead. Two years of trimmed records is about 30 MB in memory, against about 170 MB for the predecessor's full frames.

**The open question that decides the 3 minute target.** The predecessor never used the `columns` option, so whether a trimmed request returns records faster than a full one is unmeasured. The live check script measures it on day 1. If trimmed reads still miss 3 minutes on a dense object, the fall-back order is: (1) render progressively, with the seasonal comparison windows read first so the flag arrives inside 3 minutes and the full heatmap completes behind it; (2) reduce default history to 18 months; (3) only then return to the owner with Option B and its data-rights question.

**SEPA orientation (measured 10 November 2026).** The UDL's `solarEqPhaseAngle` is the mirror of the predecessor's SEPA on live data, so Light Bee flips it once at the boundary and draws the reference plots' orientation.

**SEPA and magnitude, a parity decision.** The predecessor computed SEPA itself from sensor position and line of sight, and drew `mag`. Light Bee takes the UDL's own `solarEqPhaseAngle` and draws `magNormRange` by default, which removes about 300 lines of geometry and 5 columns from every request. The live check measures how often `solarEqPhaseAngle` is populated and whether its sign convention matches. If it is sparse or disagrees, the predecessor's tested geometry module is ported as the fallback. Parity runs use raw `mag` so like is compared with like.

**Sensitivity.** UNCLASSIFIED only. Every UDL record carries `classificationMarking`; any record not marked unclassified is refused and the run reports how many were refused. UDL data may carry provider data-rights restrictions, which is why nothing is shared between analysts and nothing persists.

## 8. Security and classification

**Classification.** UNCLASSIFIED, confirmed by the owner. Exports carry the marking top and bottom, with any accepted control on the source records ("UNCLASSIFIED//PR-MRKT" for current UDL data).

**The assets.** (1) The analyst's UDL credentials. (2) The integrity of the UDL, which Light Bee must never modify. (3) Each analyst's fetched data, which another user must not see.

**The attackers.** An unauthenticated client reaching the public ingress; a signed-in user trying to read another session; a malformed request (oversize body, prototype-style payload, log injection); a hostile or unexpected UDL response (redirect, oversized page, wrong types); an accidental code change that introduces a write.

**Controls.**

* Credentials are posted once, held only in server memory against the session, sent only to the configured UDL host as Basic auth, never logged, never written, never returned to the browser (it learns `signedIn: true` only), and wiped at sign-out or expiry.
* Session cookie: random 256-bit, HttpOnly, Secure, SameSite=Strict; sessions capped; constant-time lookups.
* Read-only UDL guarantee enforced structurally (single request method, literal GET, allowlist, no redirects) and proved by the recorded-request test.
* Every request body and query parameter validated at the boundary with explicit bounds (NORAD ID 1 to 99999, five digits only for V1 by the owner's decision of 10 November 2026, window 1 to 31 days, screening distance 1 to 2,000 km, history 0.25 to 2 years); bad input rejected, never coerced.
* Every UDL response validated field by field before use; non-numeric or out-of-range values dropped and counted.
* Strict Content-Security-Policy with no inline script and `connect-src 'self'`; no third-party hosts at all.
* Two-tier rate limit: broad on all routes, strict on Run and Scan.
* Generic client errors; detail in server logs with user-supplied fragments sanitised and capped. One JSON audit line per run and per scan: session hash, NORAD ID, window, record counts, timings. No credential, ever.
* `/`, `/healthz`, `/readyz`, `/livez`, `/ping` answer 200 unauthenticated and touch nothing; `/readyz` reports version and state in its body.

**Accepted risks, recorded in the security policy.** In-memory sessions are lost on a pod restart (the analyst signs in again). App Store sign-in identity is not yet confirmed to reach the app, so sessions are bound to the browser cookie rather than the user identity (TBC, re-verify with the platform docs).

## 9. Code standards

* Python 3.12, fully type-annotated; `ruff` (lint and format) and `mypy --strict` clean.
* Cognitive complexity 15 or less and at most 7 parameters per function, enforced per commit by a local gate that is itself proved to fail on a planted violation.
* `create_app(deps)` factory; configuration from the environment only with validated defaults; no `ENV PORT` or other config baked into the Dockerfile.
* Static assets served by dictionary key, never by a path built from a request string. No user data reaches a filesystem path or an unsanitised log.
* UK English in all interface text, comments, documentation and commits. House voice throughout.
* **Must never appear:** a credential or token in any file or history; any HTTP verb other than GET towards the UDL; a URL other than the UDL base and the app's own origin; data above UNCLASSIFIED; a client-side access gate; a dependency range (every pin is `==`); a vendored file inside Sonar's analysed sources.

## 10. Quality and testing bar

**Tested means:** every behaviour has a test written with it, in the same change; the suite is green from a fresh clone with only `pip install -r requirements.txt` before it; and the pipeline simulation passes against the actual upload archive.

* **Coverage:** gate floor 80%, working target 90% or more, on all of `src/`, because on a new project every line is new code. The JavaScript is deliberately thin (about 200 lines) because the platform's test stage runs pytest only and cannot credit it. It is tested locally with `node --test` and counted honestly in the overall figure.
* **Tiers:** unit tests on pure analysis; in-process HTTP tests through `create_app` with a fake UDL; parity tests against the three reference objects using recorded fixtures; one headless browser smoke test that renders both modes, performs a drag selection and checks memory.
* **Security-property tests:** read-only request recorder, credential never in any response or log line, boundary rejection, session isolation, classification refusal, redirect refusal.
* **Gates (all four App Store conditions, plus the image):** zero new issues (local Sonar-equivalent pass); coverage 80% or more; duplication 3% or less; every security hotspot reviewed or designed out; container scan clean from a flattened, pruned runtime image.
* **Binding reviews before done:** `engineering-reviewer` PASS and `security-reviewer` PASS on every change; `deploy-gate` PASS before any upload; `design-critic` advisory on the interface.

## 11. Constraints and ownership

* **Deadline:** deployed and healthy by Friday 13 November 2026. That leaves about 3 working days, including pipeline iterations, so the plan builds compliance in from the first commit and puts the creative risk last.
* **Budget:** within the App Store envelope (8Gi, 6 CPU); Light Bee requests 2Gi and no add-ons. No paid services.
* **Owner and decision-maker:** Charlie Daniels. Charlie runs the live check script with their own credentials, confirms every upload and publish explicitly, and confirms the exports are fit for JCO use. No other sign-off required.
* **App Store listing:** slug `light-bee`, display name "Light Bee", app type Web App. Visibility: set by the owner at submission.
* **Irreversible actions** (upload, submit, environment changes) happen only after `deploy-gate` PASS and Charlie's explicit "yes".

## 12. The one creative risk

**Ghost years.** On the Other Causes light curve, the same calendar window from each prior year is drawn faintly beneath the current week, aligned on SEPA. The seasonal question is answered inside the plot the analyst is already reading, without switching modes, and it travels with the exported PNG into Mattermost, where the reader never sees "Seasonal Behaviour?" at all. It costs no extra UDL reads, because the prior-year windows are already fetched for the seasonal flag. It is spent in one place only, and it is built last so it can slip without touching the deadline.

## 13. Definition of done

Light Bee V1 is done when all of these are true:

1. `pytest` is green with coverage 80% or more, `ruff`, `mypy --strict` and the complexity gate are clean, and every local gate is proved to bite.
2. The pipeline simulation is green against the real upload archive.
3. `engineering-reviewer` and `security-reviewer` both return `VERDICT: PASS`, and `deploy-gate` returns `VERDICT: PASS`.
4. All ten App Store stages are green through Deploy, and `light-bee.apps.bluestaq.com` is Healthy with `GET /` returning 200.
5. Charlie's live check passes on TJS-3, TJS-10 and TJS-15: 3 minutes or less cold, 10 seconds or less warm, 15 seconds or less per scan.
6. Results match the old app on those three objects.
7. Charlie confirms the exported PNGs are fit for JCO use.
8. Version, changelog, security policy and runbook are committed, and the previous good archive is retained as the rollback.

## Foundations skills satisfied, by part

| Part of Light Bee | Foundations skills |
|---|---|
| Archetype, structure, `create_app` factory | `getting-started`, `code-architecture`, `toolchain-adapters` |
| Python 3.12 environment, pinned closure, two requirements files | `environment-setup`, `dependencies`, `toolchain-adapters` |
| Environment-only config, no secrets, credential handling | `configuration-and-secrets`, `security-hardening` |
| Read-only UDL client, health paths, rate limits, background runs | `api-and-integration`, `observability-and-audit` |
| Sessions and cookies, no client-side gate | `authentication-and-authorisation`, `state-management` |
| In-memory per-session data, boundary validation | `data-layer`, `state-management` |
| Plotly interface, escaping, CSP, drag selection | `frontend-and-rendering`, `accessibility` |
| Navy instrument theme, tokens, colour scales | `design-system`, `design-critic` (advisory) |
| Unit, HTTP, parity, security and browser tests, coverage | `testing-standards` |
| Local gates mirroring Sonar, pipeline simulation | `ci-cd`, `appstore-gate-compliance` |
| Dockerfile, flattened image, archive allowlist | `deploy-recipes`, `packaging`, `release-and-deploy` |
| Submission, pipeline triage, pre-flight | `app-store-deployment`, `app-store-readiness`, `deploy-gate` |
| Reviews and working method | `engineering-reviewer`, `security-reviewer`, `working-with-claude`, house voice |
| Not used (no LLM call) | `llm-integration`, removed from this project |

## Open items

* What the predecessor's FastAPI 2.0.3 upload last failed on, if known. It shapes which gate to rehearse hardest.
* Whether the App Store passes the signed-in identity to the app (TBC, re-verify).
* Mode name: "Seasonal Behaviour?" (confirmed by the owner, 10 November 2026).
* NORAD ID range: decided 10 November 2026, five digits (1 to 99999) only for V1. Alpha-5 is a later addition if an object of interest needs it.

## Decisions from the first live check (10 November 2026)

* **Marking.** Every live record is `U//PR-MRKT`. The owner accepted UNCLASSIFIED with the PR-MRKT control, then (after the third run showed older photometry marked `U//PR-EXO-OBS` or `U//DS-NSFCTC-OBS` and every KBR elset `U//PR-KBR-ELSETS`) those three controls too. Any other control, or an unusual marking form, is still refused. Each product is marked with the controls on the records it was made from, for example "UNCLASSIFIED//PR-MRKT", and may be shared in JCO Mattermost.
* **Magnitude.** `magNormRange` was empty on every record. Light Bee computes it from `georange` with the UDL's formula (`mag - 5 log10(georange / 1,000,000)`) when that is filled, and otherwise falls back to raw `mag` for the whole run, labelled as raw. The two are never mixed in one plot. The UDL's formula normalises to 1,000,000 km, so normalised values sit about 7 magnitudes fainter than raw at GEO; the colour scale is set at Phase 2 parity (TBC, re-verify against the live data).

## Decisions from the owner's service check (11 November 2026)

* **The flag first, the heatmap behind it.** Two years of a dense object is 500,000 to 860,000 observations, and the UDL delivers about 4,000 a second across 24 parallel requests, so the full history alone takes 2 to 3 minutes. The flag needs only the recent window and the same season of each earlier year (about 7% of the history), so those are read first and the flag is published as soon as they land; the rest of the history follows for the heatmap. The flag is the 3-minute answer; the heatmap may complete after it on the densest objects when the UDL is slow (accepted by the owner, 11 November 2026).
* **Each earlier season is read whole.** Two years of history ends a few days after the anniversary of the window two years ago, so the two-year season had been compared mostly on its padding days. The seasons are now read whole even where they start before the history; the heatmap still shows two years.
* **The size probe is read in 8 parallel pieces.** The UDL returns a few hundred records a second per request, so a dense week read as one request took over a minute.
* **Each earlier season's longitude is stated on its own.** A mean across years at different slots names a place the object never was (43874 read 128.7°E).
* **Heatmap colours follow the predecessor's Dynamic Range product.** Limits are fitted to each object's magnitudes, so colours are comparable within a plot, not between plots of different objects; the colour bar states the limits on every export.
