# CLAUDE.md

Always-true conventions for this project. Procedures live in `.claude/skills/`. The house voice is in `.claude/output-styles/house-voice.md`. When a rule here and a skill disagree, this file wins for conventions and the skill wins for procedure. The brief is `docs/BRIEF.md`; the build plan is `docs/BUILD-PLAN.md`.

## What this project is

Light Bee tells a space event analyst, within 3 minutes, whether a satellite's brightness change matches its own seasonal history or needs investigating as a manoeuvre or a neighbour interaction. Two modes: "Seasonal Behaviour?" and "Other Causes". Archetype: `server` (Python 3.12, FastAPI, one uvicorn worker). Deployment target: the Bluestaq App Store at `light-bee.apps.bluestaq.com`, detected template `python`. Classification: UNCLASSIFIED only.

## Hard rules (never violate)

- **No secrets in any shippable file**, in source or in history. Read secrets from the environment; render any value in docs as `[REDACTED:type]`. UDL credentials are entered by the analyst, held in server memory for the session only, and never logged, written or returned to the browser.
- **The UDL is read-only to this app.** Every UDL request is a GET with an empty body to an allowlisted path on the configured HTTPS base URL; redirects are refused. A test records every request and fails on anything else.
- **No client-side access gate.** Real gates are server-side.
- **Surgical edits only.** Change the smallest region that satisfies the request.
- **Never invent a name, title, date, organisation, or figure.** Mark unknowns `TBC, re-verify`.
- **Every untrusted value is validated at the boundary** (request bodies, query parameters, every UDL field), and a control that cannot be verified is treated as failed (fail closed). Records not marked unclassified are refused.
- **The container is the whole build.** Flattened, pruned image; non-root numeric user; no pip in the runtime image.
- **Listen on `PORT`, default 8080, bound to `0.0.0.0`.** Never add `ENV PORT=` to the Dockerfile. `/`, `/healthz`, `/readyz`, `/livez`, `/ping` answer HTTP 200 unauthenticated and touch nothing.
- **One worker.** Sessions live in process memory.
- **No dependency ranges.** Every pin is `==`, resolvable on Python 3.11 and 3.12.

## Commands

```
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt   # install
.venv/bin/pytest                                   # tests with coverage to coverage.xml (the platform's test stage)
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy   # lint, format, types
.venv/bin/python tools/gates.py                    # local Sonar-equivalent gates
.venv/bin/python tools/prove_gates.py              # prove every gate bites
.venv/bin/pytest tools/tests --no-cov -o pythonpath="src tools ."   # tool tests (local only; tools never ship)
.venv/bin/uvicorn asgi:app --app-dir src --port 8080   # run locally
.venv/bin/python tools/package_appstore.py         # build and verify the upload archive
bash tools/check.sh                                 # the whole local loop: run before every commit
bash tools/simulate_pipeline.sh                    # the platform test stage against the real archive
```

Every change runs the loop above, then passes the `engineering-reviewer` and `security-reviewer` gates before it is done. Anything that deploys, publishes, or mutates external state requires the `deploy-gate` verdict and an explicit human confirmation from Charlie.

## Directory layout

```
src/asgi.py             the entry point the container runs
src/lightbee/           the application package
tests/                  pytest suite (ships in the archive; excluded from the image)
tools/                  gates, packager, pipeline simulation, live check (archive no, image no: SonarQube would analyse it)
docs/                   brief, build plan, security policy, changelog, runbook
.claude/                the Foundations baseline
```

## Naming and versioning

- Releases are `V${MAJOR}.${MINOR}`, written `MAJOR.MINOR.0` in `pyproject.toml` and `src/lightbee/__init__.py`, which must agree (a test checks). Build phases are 0.x; the first deployed release is 1.0. Add one `CHANGELOG.md` row on every change.
- App Store slug: `light-bee`. Display name: Light Bee.

## House voice (applies to all prose, UI copy, comments, commits)

UK English. No em-dashes. No `+` meaning "and". Use the `£`, `$`, `%` symbols. Expand an uncommon acronym in full on first use. Lead with the decision, then the reasoning. Never fabricate data. Full voice: `.claude/output-styles/house-voice.md`.
