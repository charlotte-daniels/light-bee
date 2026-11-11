# AUDIT.md

The map over this baseline: which source each part came from, the parameters you set per project and how to obtain each, and the security findings carried forward. Read this once before filling in `CLAUDE.md`.

## 1. What this baseline is

Bluestaq Foundations is one archetype-aware engineering baseline merged from two real Bluestaq systems: a single-file offline static web artifact (the "static" archetype) and a server-backed LLM container app (the "server" archetype). Every skill states which archetype(s) it applies to, so one bundle serves both without losing either's rigour. The App Store doctrine (`app-store-deployment`) is the shared deployment backbone.

## 2. Domain map (skill to source)

| Skill | Static source | Server source |
|---|---|---|
| getting-started | cold-start runbook | zero-to-deploy runbook |
| environment-setup | Node pin, browser driver | Node pin, Docker |
| glossary | static term set | server term set (union) |
| code-architecture | single-file, no build | createApp factory, container-is-build |
| dependencies | zero runtime deps | recorded-reason policy |
| configuration-and-secrets | no runtime config | env-only, two-stage App Store vars |
| data-layer | data-as-literals, build-pass | atomic JSON store, anti-shrink merge |
| api-and-integration | no-egress CSP | HTTP API, health, two-tier limits |
| llm-integration | (local tooling only) | pause_turn loop, prompt cache, cost cap |
| authentication-and-authorisation | no client-side gate | timingSafeEqual, CORS fail-closed, SSO seam |
| frontend-and-rendering | inline UI, vector renderer | served-static SPA, offline-first |
| state-management | view-state, rAF, storage wrapper | hydrated state, monotonic-rev sync |
| design-system | tokens, dual palette | tokens, components | (both aligned to Bluestaq brand) |
| testing-standards | validate, render-check, static-checks | node:test, in-process HTTP, Playwright |
| ci-cd | mirror-the-loop workflow | mirror-the-loop workflow |
| packaging | entrypoint-only zip, dated copies | lean .dockerignore context |
| observability-and-audit | no telemetry, human audit row | health/readiness, JSON audit line |
| security-hardening | CSP/escaper/no-egress | key+budget threat model, controls |
| release-and-deploy | object-storage path | container-is-the-build path |
| app-store-deployment | (shared) | App Store v2.0 doctrine |

## 3. Parameter table (set per project; how to obtain each)

Fill these in `CLAUDE.md`. Record any secret value as `[REDACTED:type]`, never the real value.

| Parameter | Meaning | How to obtain |
|---|---|---|
| `${PROJECT_ONE_LINE}` | one-line description | from the project owner |
| `${ARCHETYPE}` | `static` or `server` | decided in `getting-started` Step 0 |
| `${APP_SLUG}` | App Store slug (lowercase, hyphens) | chosen; must be unique in the store |
| `${APP_STORE_TEMPLATE}` | node-react / java-spring / python / docker-only / static-html | auto-detected from package contents (`app-store-deployment`) |
| `${NODE_VERSION}` | pinned Node major | the project's runtime (here 22) |
| `${BROWSER_DRIVER_PATH}` | absolute path to the browser driver | only if auto-resolution fails (`environment-setup`) |
| `${SOURCE_PATH}` | the artifact or source entry | the project layout |
| `${ENTRYPOINT_NAME}` | served filename (static) | commonly `index.html` |
| `${PRODUCT}` | product name for dated copies | from the owner |
| `${MAJOR}` / `${MINOR}` | version numbers | the release |
| `${INSTALL_CMD}` / `${TEST_CMD}` / `${DEV_CMD}` / `${BUILD_CMD}` | canonical commands | from `package.json` scripts |
| `${S3_BUCKET}` | static upload bucket | `bluestaq-appstore-uploads` (not the stale `bluestaq-appstore-bucket`) |
| `${S3_PREFIX}` | static object prefix | per project from the platform team |
| `${LLM_API_KEY}` | LLM provider key (server) | the provider; stored as an App Store secret, `[REDACTED:key]` in docs |
| `${ALLOWED_ORIGIN}` | production CORS origin (server) | the app's real origin, e.g. `https://${APP_SLUG}.apps.bluestaq.com` |
| `${STORAGE_MOUNT_PATH}` | persistent volume path (server) | `/data` when the file-storage add-on is selected |

## 4. Security findings carried forward

- **Client-side gate is not a boundary.** A browser PIN, flag, or hidden field is a user-experience gate only; the server token and server validation are the only security boundaries (`authentication-and-authorisation`, `security-hardening`). Recorded as a standing anti-pattern.
- **`ENV PORT=` anti-pattern.** Setting a non-8080 port in the Dockerfile breaks the platform contract; the secret-scan hook flags it (`app-store-deployment`).
- **Stale bucket name.** `bluestaq-appstore-bucket` is stale; use `bluestaq-appstore-uploads` for static-html uploads (`app-store-deployment`).
- **Accepted risks (server).** The shared-token model (no per-user identity) and dataset confidentiality are deliberately out of scope; record them in the project security policy rather than leaving them implicit (`security-hardening`).

## 5. Provenance

Merged verbatim-faithfully from two extracted foundations bundles and the authoritative App Store reference (`appstore.md`, v2.0). No project-specific business data is embedded; every product-specific value is a `${PARAMETER}` set per project. Where a server-side detail was referenced but not uploaded (api-layer, auth), it was reconstructed from the security-hardening and getting-started references and marked as such in the owning skill's provenance.
