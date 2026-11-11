# Light Bee: build plan

Bluestaq Limited. 10 November 2026. Companion to `BRIEF.md`. No code is written until the owner gives the go-ahead.

**How every step runs.** Tests are written with the code in the same step. Each step ends with the local loop green (`pytest` with coverage, `ruff`, `mypy --strict`, the complexity and duplication gates) and a short commit. A step that touches credentials, sessions, the UDL client or the CSP also passes `security-reviewer`. Bugs found at any step are fixed in that step, not deferred. Nothing is carried as debt: a shortcut is either done properly or cut from scope and recorded.

**Schedule.** Tuesday afternoon to Friday morning, with Friday held for pipeline iterations. The day 1 live check is the decision point for the 3 minute target.

## Phase 0: compliant scaffold (Tuesday)

1. **Fill `CLAUDE.md`** with the real values (server, `python` template, `light-bee`, commands), remove `llm-integration`, record the archetype.
2. **Scaffold the compliance from the first commit**, adapted from the predecessor's proven parts rather than re-derived:
   * flattened, pruned multi-stage `Dockerfile` (prep stage, suid sweep last, `FROM scratch`, numeric non-root user, one uvicorn worker, `PORT` default 8080 in the command, no `ENV PORT`);
   * `requirements.txt` (runtime and test tooling, exact pins, resolvable on Python 3.11 and 3.12) and `requirements-runtime.txt` (image only);
   * `pyproject.toml`, `sonar-project.properties` (sources `src`, vendored Plotly excluded, coverage at `coverage.xml`);
   * `.gitignore`, `.dockerignore`, allowlist packager that reads the archive back and rejects anything forbidden;
   * `tools/gates.py` (complexity 15, parameters, duplication, pins agree with the declared Python floor, no non-GET verb, Dockerfile instructions) and `tools/prove_gates.py` that plants each violation and confirms each gate bites;
   * `tools/simulate_pipeline.sh` (unzip the real archive, add a platform-style CI file, fresh install, `GITLAB_CI=true pytest --cov`, check `coverage.xml` exists).
3. **Walking skeleton:** `create_app(deps)`, the five health paths answering 200 unauthenticated, `/readyz` reporting version and state, a static page served by key. Tests: in-process HTTP for every path.
4. **Build and probe the image** locally: `docker build`, run, `id` is non-root, `curl` health paths return 200, image contents contain no pip, tests or `.env`.
   *Exit check:* loop green, gates proved, simulation green, image probe green, `engineering-reviewer` PASS.

## Phase 1: the UDL client and the live check (Tuesday to Wednesday morning)

5. **Read-only client** (`udl/client.py`): one private request method with a literal GET, path allowlist (observation history, live and tuple; elset tuple; queryhelp; count), base URL fixed to HTTPS, redirects refused, timeouts, 24 requests in flight halved on the first 429, a slice at the page cap halved and re-read, never a partial answer.
   *Tests:* recorded-request read-only gate, redirect refusal, 429 backoff, cap-and-halve, timeout-and-halve, credentials never in an exception string.
6. **Query layer** (`udl/queries.py`): column-trimmed observation reads with a recent probe slice that sizes the rest; target and catalogue elset reads; field-by-field validation and the classification refusal.
   *Tests:* fixtures shaped exactly to the supplied OpenAPI schemas; malformed and above-UNCLASSIFIED records refused and counted.
7. **Live check script** (`tools/live_check.py`) for Charlie to run on their own machine, credentials from a prompt, read-only, reporting only pass/fail, counts and timings:
   * trimmed versus full read speed on one dense object over 30 days;
   * how often `solarEqPhaseAngle`, `magNormRange` and `geolon` are populated, and SEPA sign agreement with the predecessor;
   * the `KBR` elset source string and GEO catalogue slice size;
   * a full cold run on TJS-3, TJS-10 and TJS-15 against the 3 minute target.
   **Decision point:** if trimmed reads put a dense object over 3 minutes, switch on the progressive fall-back in the brief before building further; if SEPA is sparse, port the predecessor's geometry module with its tests.
   *Exit check:* `security-reviewer` PASS on the client and the script.

## Phase 2: the analysis engine (Wednesday)

8. **Seasonal grid** (`analysis/seasonal.py`): date by SEPA raster (2 degree bins, daily median, minimum 3 points per bin, row 0 most recent), daily average longitude, drift detection.
9. **Seasonal flag:** recent window against the same calendar season in each prior year, bin by bin, using the prior years' own scatter (thresholds 0.2 mag and 2 sigma, minimum 8 comparable bins), four outcomes, suppressed with a stated reason during drift, working shown.
   *Tests:* synthetic seasonal object reads consistent; synthetic step reads departs; sparse data reads inconclusive; drift suppresses; parity fixtures for 43874, 58204 and 63157 (partial history) agree with the predecessor's verdicts.
10. **Light curve and VisMag v Time** (`analysis/lightcurve.py`): window selection, interval-day colouring, sensor grouping, raw and normalised magnitude.
11. **Orbital series** (`analysis/orbit.py`): period, inclination and RAAN from the chosen provider's elsets.
12. **Neighbourhood screen** (`analysis/screen.py`): GEO snapshot, mean motion and inclination cuts, vectorised SGP4 coarse sweep then refinement, separations at every selected time, elset age per neighbour, an uncertainty band near the threshold; close approaches v time over the whole window.
   *Tests:* two synthetic co-located objects found at the right range; a distant object excluded; a multi-day selection lists every time; scan of a full GEO snapshot completes well inside 15 seconds on the test runner.
   *Exit check:* coverage of `analysis/` 95% or more; `engineering-reviewer` PASS.

## Phase 3: the service and interface (Wednesday to Thursday)

13. **Sessions** (`web/session.py`): sign-in stores credentials in memory only; cookie random, HttpOnly, Secure, SameSite=Strict; 8 hour idle expiry; session cap; sign-out wipes.
14. **Runs:** single-flight background job per session and object, each with its own UDL client and throttle, so a failed run's requests still in flight never take slots from the next; progress endpoint polled by the browser; staged results (recent, then history, then elsets and catalogue); two-tier rate limits; one JSON audit line per run and scan.
    *Carried from the Phase 2 reviews:* escape `UdlRequestError.detail` and refused-marking labels wherever they are rendered (they may hold `<>&"'`); keep a scan's selected instants inside the run's read window; run `approaches()` off the request thread with a time limit (about 30 s at the 3,000-candidate and 31-day caps).
    *Tests:* session isolation (one analyst cannot read another's run), credential absent from every response and log line, boundary rejection of every parameter, rate limits return 429.
15. **Figures** (`figures/`): Plotly JSON builders for both modes from the shared navy theme tokens, with title, object, window, source, generation time, UNCLASSIFIED markings and the test and evaluation banner built into every figure, so the exported PNG carries them.
16. **Interface:** one page, two mode tabs, inputs with defaults (today, 7 days, KBR, 150 km), progress line, seasonal flag panel, drag selection relayed to the scan endpoint, neighbour list pop-up, export buttons. Strict CSP, no inline script, all values escaped, keyboard-operable, labelled controls, contrast checked.
    *Tests:* `node --test` for the JavaScript; headless browser smoke with a fake UDL that renders both modes, drags, reads the neighbour list, exports, and checks memory under 500 MB.
    *Exit check:* `security-reviewer` PASS, `design-critic` advisory actioned where it affects correctness or accessibility.

## Phase 4: verification, audit and the creative risk (Thursday)

17. **Ghost years** on the Other Causes light curve, from the prior-year windows already fetched. Built only if steps 1 to 16 are green by Thursday midday; otherwise recorded as V1.1.
18. **V&V pass:** Charlie runs the live check on the three reference objects; timings and parity recorded in `docs/VERIFICATION.md`. Any miss is fixed and re-run. The Phase 2 parity fixtures for 43874, 58204 and 63157 were moved here (Phase 2 review, 10 November 2026): they need real photometry, so the live check's cold runs now report the seasonal verdict, comparable bins and median offset for each object, and Charlie compares them with the predecessor's verdicts for the same windows.
19. **Audit:** full `engineering-reviewer` and `security-reviewer` over the whole tree, a tech-debt sweep (no TODOs, no dead code, no skipped tests, no suppressions without a written reason), the duplication gate at 3% or less, and `app-store-readiness` scored.
20. **Documents:** `SECURITY.md` (threat model and accepted risks), `CHANGELOG.md` (V1.0), `RUNBOOK.md` (deploy, rollback, triage by stage), and the App Store listing text.

## Phase 5: deploy (Friday)

20a. Before packaging: check Plotly 2.35.3 against current vulnerability advisories (npm audit or the GitHub advisory database) and record the result; ask the platform team whether the ingress rate-limits and passes a trustworthy client address (if so, key the sign-in and request limits by address in a later release).
21. Build the archive with the packager, run the pipeline simulation against it, probe the image again.
22. `deploy-gate` validates the release and the submission field by field.
23. On `deploy-gate` PASS and Charlie's explicit confirmation, upload and submit. Watch all ten stages; diagnose any red stage from the raw log before changing anything; fix, re-simulate, re-upload.
24. Post-deploy pre-flight: Healthy, `GET /` returns 200, `/readyz` reports V1.0, Charlie signs in, runs one reference object end to end, and confirms the exports. Retain the archive as the rollback.

## Biggest risks, and what is done about each

| Risk | Mitigation |
|---|---|
| A cold read of a dense object exceeds 3 minutes | Measured on day 1 before the analysis is built; progressive rendering and shorter default history ready as fall-backs |
| Container scan findings from the base image | No pandas, matplotlib or Pillow; pruned, flattened runtime image from the first commit |
| A gate that only reveals itself on the platform | Every Sonar rule the predecessor met has a local gate proved to bite; the pipeline simulation runs against the real archive |
| JavaScript lowers overall coverage | Figures built and tested in Python; the JavaScript kept to about 200 lines |
| The deadline | Compliance first, creative risk last, Friday reserved for pipeline iterations |
