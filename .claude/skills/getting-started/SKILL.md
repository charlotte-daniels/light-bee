---
name: getting-started
description: Zero-to-deploy runbook for a Bluestaq project. Start here on a fresh checkout or empty repository. Decides your archetype, then threads every other skill in order: environment, dependencies, secrets, build, test, the three review gates, and App Store deploy. Use when bootstrapping, onboarding, or unsure which skill applies. Make sure to use this skill whenever someone says "where do I start", "set up a new project", "get this deployable", or "ship to the App Store".
---

# Getting started: empty repository to live App Store deployment

## Purpose and scope

The single runbook that takes a newcomer from a clean machine to a deployed Bluestaq App Store application, branching into detailed skills only where depth is needed. It covers the whole happy path and the order of the gates. It does not teach each domain in depth (each step links to the owning skill) nor author new product features.

## When to use

- You just cloned or created the project and need it running and deployable.
- You are unsure which skill to read: start here and follow the link.
- You are about to deploy and want the full chain in front of you.

## Prerequisites

1. **Git.** Verify: `git --version`.
2. **A POSIX shell** (macOS/Linux native; Windows use Windows Subsystem for Linux or Git Bash). Verify: `echo $0`.

All other tools (the pinned Node runtime, a headless browser, Docker for the server archetype) are installed in step 1 via `environment-setup`, which assumes nothing.

## Step 0: decide your archetype (two questions)

1. **Does the app run a server process at runtime (an HTTP Application Programming Interface, a backend, a database, an LLM call)?** If no, you are **static**. If yes, you are **server**.
2. **Does it have a build step (a bundler producing `dist/`)?** Static with no build, App Store template `static-html`. Static with a build, `node-react`. Server, one of `node-react`, `java-spring`, `python`, `docker-only`.

Record the archetype and template in `CLAUDE.md`. They decide which branch of each step below you follow.

## Procedure

Run in order. Do not skip a gate. Each step states the expected result; do not proceed past a result you did not see.

1. **Set up the environment.** Follow `environment-setup` fully.
   ```
   node --version          # expect the pinned major, e.g. v22.x
   git --version
   ```
2. **Install dependencies.** `npm ci` (honours the lockfile). Expect "added N packages", exit 0. Detail: `dependencies`.
3. **Configure parameters and secrets.** Follow `configuration-and-secrets`. Static: set any build params, confirm no secret is tracked. Server: `cp .env.example .env` and fill it; never commit `.env`. Expect no secret in any tracked file.
4. **Greenfield only (empty repository).** This bundle ships standards, not a starter app. Create the skeleton from the skills in this order, copying each skill's Procedure snippets into the named files. Static: the single artifact at `${SOURCE_PATH}` plus `scripts/validate.mjs`, `scripts/render-check.mjs`, `scripts/static-checks.sh`, `scripts/build-package.sh`. Server: `code-architecture` (the `createApp(deps)` factory in `src/app.js`, listener in `src/index.js`), then `configuration-and-secrets`, `data-layer`, `api-and-integration`, `authentication-and-authorisation`, `llm-integration` (if used), `frontend-and-rendering`, `design-system`, `testing-standards`, `ci-cd`, and `release-and-deploy` (the `Dockerfile` and `.dockerignore`). Then `npm install` once to generate the lockfile and commit it.
5. **Understand the app.** Skim `code-architecture` and the relevant frontend/data/state skills for your archetype to know where things live. No command; you are orienting.
6. **Test first.** Run the verification loop (`testing-standards`).
   ```
   npm test                # static: three green lines | server: unit tests pass, coverage written
   npm run test:e2e        # server: browser smoke (or deferred to CI if no local browser)
   ```
7. **Make your change** (if any) with surgical edits per `code-architecture`. Re-run the loop until green.
8. **Pass the engineering gate.** Invoke `engineering-reviewer` on your diff. Expect `VERDICT: PASS`. Fix any BLOCKER/MAJOR and re-run.
9. **Pass the security gate** for any security-relevant change. Invoke `security-reviewer`. Expect `VERDICT: PASS`.
10. **Build the package or image.** Static: `./scripts/build-package.sh <version> <date>`, then `unzip -l` shows only `${ENTRYPOINT_NAME}` (`packaging`). Server: `docker build -t ${APP_SLUG} .` succeeds (`release-and-deploy`).
11. **Pass the deploy gate.** Invoke `deploy-gate`. Expect `VERDICT: PASS` with every precondition green (clean tree, version bumped, both reviews PASS, package/image meets the contract, submission prepared per `app-store-deployment`, rollback known).
12. **Deploy.** Follow `release-and-deploy`, which hands the prepared submission to `app-store-deployment` for the platform. The irreversible publish requires explicit human confirmation. Expect the deployment Healthy and the app reachable at `${APP_SLUG}.apps.bluestaq.com`.
13. **Verify post-deploy.** Run the pre-flight routine: every pipeline stage green, pods Running and Healthy, `GET /` returns 200, rollback still available (`app-store-deployment` -> pre-flight).

## Decision rules

- **Content edit or structural change?** Content edit: steps 6 to 12. Structural change (new domain, dependency, CSP change, new service): read the relevant domain skill in full first.
- **`npm test` failed at step 6 before you changed anything?** The checkout or environment is wrong, not your work. Return to `environment-setup` -> Failure modes.
- **First-ever deploy?** Do the one-time platform configuration in `configuration-and-secrets` (static: `S3_BUCKET`/`S3_PREFIX`; server: the deploy-time env contract) before step 12.
- **A gate returned FAIL?** Stop. Fix every BLOCKER and MAJOR. Re-run the gate. Never deploy past a FAIL.
- **Local single-user vs hosted team (server).** No team token set, runs locally with auth off bound to loopback. To host for a team, set the team token AND the allowed origin together (`authentication-and-authorisation`); a token without an origin makes the app refuse to start in production.

## Standards (checkable assertions)

- The verification loop is green before any gate runs.
- Both `engineering-reviewer` and `security-reviewer` return PASS before packaging.
- The package contains only `${ENTRYPOINT_NAME}` (static) or the image runs non-root on port 8080 (server).
- `deploy-gate` returns PASS before any irreversible step.
- The publish was confirmed by a human, not fired autonomously.
- Post-deploy health returns 200 and a tested rollback exists.

## Failure modes and remedies

- **`npm ci` fails with a lockfile error.** Node version mismatch. Fix: install the pinned Node (`environment-setup`).
- **`render-check` cannot find the browser driver** (static). Fix: install it and/or set `${BROWSER_DRIVER_PATH}` (`environment-setup`).
- **App will not start, "Refusing to start: ALLOWED_ORIGIN is '*' with a token set"** (server). Fix: set the allowed origin to the app's real origin (`authentication-and-authorisation`).
- **Health check fails after deploy though the app logs "listening"** (server). The platform probe port does not match. Fix: read `process.env.PORT`, default 8080, bind `0.0.0.0` (`release-and-deploy`).
- **Pipeline red after deploy.** Do not retry blindly. Run `get_pipeline_diagnosis` first, fix the cause, resubmit (`app-store-deployment` -> triage).

## Verification

Done when, in one session from a clean checkout: the loop is green, both review gates PASS, the package/image meets the contract, `deploy-gate` PASSed, the publish was human-confirmed, the pipeline is green, and the deployed URL renders and functions with `GET /` returning 200 and rollback ready.

## Worked example

A newcomer clones the repo. Step 0: it has an HTTP API and an LLM scan, so it is **server**, template `node-react`. They install the pinned Node, `npm ci` (42 packages), `cp .env.example .env` and paste their LLM key, `npm run seed` (79 records), `npm run dev` (banner; `curl /healthz` returns 200), `npm test` (61 pass with coverage). No change yet, so straight to `docker build -t demo .` (succeeds), run `deploy-gate` (FAIL: version not bumped), bump it, re-run (PASS), follow `release-and-deploy` to submit through `app-store-deployment`, confirm the publish, see the deployment Healthy with `GET /` returning 200. No question was unanswered.

## Glossary

- **Archetype:** static (no server) or server (container) shape, decided in Step 0.
- **Gate:** a fail-closed agent returning `VERDICT: PASS`/`FAIL`; work does not proceed past a FAIL.
- **Verification loop:** the local checks run by `npm test` (and `npm run test:e2e` on the server archetype).
- Full terms: `glossary`.

## Provenance

Merged from both source bundles' getting-started runbooks, their verification and ship commands, the engineering and deployment personas, and the App Store golden path.
