---
name: configuration-and-secrets
description: Configuration and secrets handling. Use when reading config, adding an environment variable, or handling a credential. Covers env-only configuration, the committed .env.example, the no-secrets-in-repo rule, the App Store two-stage save then apply of environment variables, and why secrets are never baked into a Dockerfile.
---

# Configuration and secrets

## Purpose and scope

How the project is configured and how credentials are kept out of source. Configuration is read from environment variables only. Real secret values never enter the repository, the image, or any log. Scope covers the config surface, the `.env.example` contract, and the App Store environment-variable lifecycle. It does not cover the deploy mechanics (`release-and-deploy`) or the full submission flow (`app-store-deployment`).

## When to use

- Adding or changing any tunable value (port, origin, key, feature flag).
- Wiring a credential the app needs at runtime.
- Preparing environment variables for an App Store submission.

## Prerequisites

- `environment-setup` complete.
- Archetype known (`code-architecture`). Static artifacts usually have no runtime config; server apps read config from the environment.

## Procedure

1. **Read all config from the environment.** No config file holds a real secret, ever.
   ```js
   const PORT = Number(process.env.PORT) || 8080;     // server reads at request time, not module load, for add-on vars
   const ALLOWED_ORIGIN = process.env.ALLOWED_ORIGIN;  // required in prod when a token is set
   ```
2. **Maintain a committed `.env.example`** listing every variable by name, with a placeholder, never a real value.
   ```
   PORT=8080
   ALLOWED_ORIGIN=https://${APP_SLUG}.apps.bluestaq.com
   API_TOKEN=[REDACTED:token]
   LLM_API_KEY=[REDACTED:key]
   ```
3. **Keep real values out of the repo.** `.env` is git-ignored. Use `[REDACTED:type]` as the placeholder convention in any shared example or doc.
4. **App Store: stage then apply.** `save_env_vars` writes the FULL set (it is a complete replacement, not a merge), then `apply_env_vars` makes them live. Read injected add-on variables (database URL, storage path) at request time, not at module load, because the platform injects them into the running container.
5. **Never bake a secret into the image.** No `ENV API_TOKEN=` in the Dockerfile. The secret-scan hook fails the build on a Dockerfile `ENV` that names a secret-like key.

## Decision rules

- **Secret needed at build time?** It is not; the build must not need secrets. Re-scope so the secret is read at runtime from the environment.
- **New variable added?** Add it to `.env.example` in the same change, with a placeholder.
- **`save_env_vars` vs `apply_env_vars`?** Save writes the full replacement set; apply makes it live. Always send the complete set to save, or you drop the omitted ones.
- **Add-on variable (Postgres/Redis/storage)?** Read it at request time. Reading at module load captures an empty value before the platform injects it.

## Standards (checkable assertions)

- No real secret appears in the repo, the image, or any log (secret-scan hook clean).
- Every runtime variable is listed in `.env.example` with a placeholder.
- The server reads config from `process.env`, not from a committed config file.
- No Dockerfile `ENV` line names a secret-like key.
- App Store env vars are sent to `save_env_vars` as the complete set.

## Failure modes and remedies

- **App boots with empty add-on config.** Cause: read at module load before injection. Fix: read `process.env.X` at request time.
- **Half the env vars vanish after a save.** Cause: `save_env_vars` was sent a partial set; it replaced the whole set. Fix: always send the full set.
- **Secret committed by accident.** Detect: secret-scan hook (exit 2). Fix: rotate the credential immediately, purge from history, re-commit with a placeholder.
- **Secret found in a log line.** Fix: remove the field; log a sanitised, capped actor reference only (`observability-and-audit`).

## Verification

`grep -rnE "(api[_-]?key|token|secret|password)\s*[:=]" <repo>` returns only placeholders; the secret-scan hook passes; `.env.example` lists every variable the app reads; for an App Store app, the saved env set is complete before apply.

## Worked example

Adding an LLM key to a server app: add `LLM_API_KEY=[REDACTED:key]` to `.env.example`; read `process.env.LLM_API_KEY` at request time in the integration module (`llm-integration`); for deployment, `save_env_vars` with the full set including the new key, then `apply_env_vars`. No key value ever touches the repo, the Dockerfile, or a log line.

## Glossary

- **`.env.example`:** committed template of variable names with placeholders, no real values.
- **`[REDACTED:type]`:** placeholder convention marking where a real secret would go.
- **save then apply:** App Store two-stage env-var lifecycle; save is a full replacement, apply makes it live.
- Other terms: `glossary`.

## Provenance

Merged from the static no-runtime-config posture, the server env-only configuration with request-time reads, the `.env.example` contracts in both bundles, and the App Store env-var two-stage lifecycle in `appstore.md`.
