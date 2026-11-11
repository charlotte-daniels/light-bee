# Light Bee runbook

How to deploy, check, roll back and triage Light Bee on the Bluestaq App Store. The deployment reference is `.claude/skills/app-store-deployment`; this page is the Light Bee specifics.

## The release at a glance

| Item | Value |
|---|---|
| App Store slug, display name | `light-bee`, Light Bee |
| Template, app type | `python` (detected from the root `requirements.txt`; the root `Dockerfile` builds the image), Web App |
| Category, visibility | Data Analytics, Private (owner, 11 November 2026); widen visibility only after the post-deploy pre-flight passes |
| Descriptions | short: the first paragraph of the App Store listing below; full: all three paragraphs |
| Version | the `pyproject.toml` version, `MAJOR.MINOR.0`; V1.0 is the first deployed release |
| Container budget | 2Gi memory, 2 CPU (measured: about 350 MB for one dense 2-year object; the cache is held to 900 MB across sessions) |
| Port | `PORT`, default 8080, bound to 0.0.0.0; never set `PORT` in the Environment Variables tab |
| Environment Variables tab | empty: every setting has a safe code default (`src/lightbee/config.py`) |
| Health | `/`, `/healthz`, `/livez`, `/readyz`, `/ping` answer 200 unauthenticated; `/readyz` reports the version |
| Workers | one uvicorn worker (sessions live in process memory) |
| Add-ons | none: nothing is stored, so no database or persistent volume |
| `securityContext.fsGroup` | not applicable: no volume is mounted and the app writes nothing to disk |
| Base image | `python:3.12-alpine`, flattened `FROM scratch`, user 10001 (`docs/evidence/container-scan-1.0.0.md`) |

## Deploy

1. `bash tools/check.sh` must end `LOOP: PASS`.
2. `.venv/bin/python tools/package_appstore.py` builds and verifies the upload archive.
3. `bash tools/simulate_pipeline.sh` must end `SIMULATION: PASS` on that archive.
4. Check Plotly's vendored version against current advisories and record the result in `docs/DEPENDENCIES.md`.
5. Build the image from the archive with `docker build --pull`, probe the health paths with `PORT` injected, and scan it with grype: no critical or high finding may remain (`docs/evidence/container-scan-1.0.0.md` records the first scan).
6. The `deploy-gate` agent validates the release and every submission field. Nothing is uploaded without its PASS and Charlie's explicit confirmation.
7. Upload with a fresh fileRef, leave the Environment Variables tab empty, submit, and watch every stage: setup, check, build, test, scan, package, containerize, container-scan, deploy.

## After the deploy (pre-flight)

1. The app shows Healthy; `GET /` and `/healthz` return 200; `/readyz` reports the deployed version.
2. Charlie signs in with his UDL login and runs 43874 end to end: the flag appears within 3 minutes, Other Causes draws, a drag-select scan lists neighbours, both PNG exports carry the marking, banner and footer.
3. The JSON audit lines (`lightbee.audit`) show the run and scan, with no username or credentials.
4. Keep the uploaded archive as the rollback.

## Roll back

Re-upload the retained archive of the previous release and submit it. Sessions are in memory, so a rollback or restart signs everyone out; analysts sign in again. Nothing is stored, so there is no data to migrate or restore.

## Triage by stage

Diagnose from the raw log before changing anything; never retry a real failure.

| Stage | Likely cause for Light Bee | Fix |
|---|---|---|
| zero stages run | app-record residue or a bad slug | recreate under `light-bee`, confirm old records cleared |
| build, containerize | the archive is nested | rebuild with the packager, which keeps `Dockerfile` at the root |
| test | a test or the coverage path | reproduce with `tools/simulate_pipeline.sh` |
| quality gate | a Sonar violation, coverage, a hotspot | run `tools/gates.py`; fix the metric named |
| container-scan | an OS or package CVE, a setuid bit | read the approval page, not just the job log; reproduce with grype; rebuild with `--pull` on the patched Alpine base; the image is flattened and has no pip, apk binary or SQLite |
| deploy | port, bind or user | the runtime contract above; read the raw pod log |
| green, but the browser gets 401 | the platform's single sign-on gateway in front of the app | if `/healthz` also 401s, it is the gateway: raise with the platform team |
| runs fail with "did not accept your credentials" | the analyst's UDL login | the analyst checks their UDL account |
| runs fail with "The UDL read failed" | UDL outage, rate limit or a changed field | the message names the stage; re-run `tools/live_check.py` |

## Operational limits

Runs 20 an hour per session and 60 across the service; scans 120 and 600 an hour, one at a time per session, each stopped at 15 s; failed sign-ins 10 per username and 100 across the service per 15 minutes; requests 600 a minute per session. Sessions end after 8 idle hours; at most 16 at once. Accepted risks are in `docs/SECURITY.md`.

## App Store listing

**Light Bee** tells a space event analyst, in under 3 minutes, whether a GEO satellite's brightness change matches its own seasonal history or needs investigating as a manoeuvre or a neighbour interaction.

Sign in with your own UDL login (held in memory for your session only), enter a NORAD ID and a change date, and Light Bee reads the object's photometry, elsets and the GEO catalogue from the UDL, read-only. **Seasonal Behaviour?** compares the last week with the same season in earlier years and says whether the change is consistent, marginal or departs from it. **Other Causes** lines up the light curve with period, inclination, RAAN and close approaches, and a drag-select lists every GEO object within the screening distance at the selected times. Exports are marked and stand alone for sharing.

UNCLASSIFIED only. Under test and evaluation: not for operational decisions.
