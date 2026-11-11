# COMPLETENESS.md

Proof the merged baseline has no gaps: every source area maps to an artifact, both archetypes are covered, every standard has an owner and a gate, and the definition of done is met.

## 1. Coverage table (source area to artifact)

| Source area | Documented by |
|---|---|
| Cold start / onboarding | `getting-started`, `environment-setup`, `START-HERE.md` |
| Vocabulary | `glossary` |
| App structure | `code-architecture` |
| Dependencies and supply chain | `dependencies` |
| Config and secrets | `configuration-and-secrets` |
| Persistence | `data-layer` |
| Routes, health, rate limits, egress | `api-and-integration` |
| LLM calls (server) | `llm-integration` |
| Auth, token, CORS | `authentication-and-authorisation` |
| UI structure and rendering | `frontend-and-rendering` |
| State and sync | `state-management` |
| Visual system and brand | `design-system` |
| Tests and coverage | `testing-standards` |
| PR pipeline | `ci-cd` |
| Producing the package | `packaging` |
| Health, logging, audit | `observability-and-audit` |
| Defence posture | `security-hardening` |
| Deploy procedure | `release-and-deploy` |
| App Store target and tooling | `app-store-deployment` |
| Binding review | `agents/engineering-reviewer.md`, `agents/security-reviewer.md`, `agents/deploy-gate.md` |
| Advisory design review | `agents/design-critic.md` |
| House voice | `output-styles/house-voice.md` |
| Guardrails | `hooks/secret-scan.mjs`, `hooks/format-gate.sh`, `hooks/hooks.json` |
| Project memory | `CLAUDE.md` |
| Parameters and provenance | `AUDIT.md` |

Unmapped: none.

## 2. Both-archetype coverage proof

Every domain skill names the archetype(s) it applies to. The static archetype is fully covered (single-file architecture, no-egress posture, inline renderer, CSP/escaper hardening, entrypoint-only packaging, object-storage deploy, human audit row). The server archetype is fully covered (createApp factory, HTTP API with health and rate limits, LLM integration, token auth and CORS, monotonic-rev sync, container-is-the-build, quality-gate testing, structured audit line). Shared concerns (config and secrets, dependencies, design system, CI, App Store doctrine, the gates, the house voice) apply to both. No archetype-specific rigour from either source bundle was dropped in the merge.

## 3. Standards index (owner and gate)

| Standard | Owner | Gate |
|---|---|---|
| Archetype-correct architecture, no build (static) / container-is-build (server) | code-architecture, dependencies | engineering-reviewer |
| Exact-pinned deps, lockfile, npm ci, CVE scan clean | dependencies | engineering-reviewer + security-reviewer |
| No secret in repo/image/log; env-only config | configuration-and-secrets | secret-scan hook + security-reviewer |
| Atomic writes, anti-shrink merge, boundary validation | data-layer | security-reviewer |
| No egress (static) / health 200, two-tier limits (server) | api-and-integration | deploy-gate / security-reviewer |
| LLM key server-side, bounded loop, fail-closed parse, cost cap | llm-integration | security-reviewer |
| Constant-time token, route gating, CORS fail-closed, no client gate | authentication-and-authorisation | security-reviewer |
| Escape all reflected input; instant-close panels; offline-first | frontend-and-rendering | engineering-reviewer + design-critic |
| One state object; rAF coalescing / monotonic-rev sync; prototype-strip | state-management | engineering-reviewer + security-reviewer |
| Token-only colours, Bluestaq palette, one accent, WCAG AA | design-system | design-critic (advisory) |
| Verification loop / coverage 80%, parity, security-property tests | testing-standards | engineering-reviewer |
| CI mirrors local loop, least privilege | ci-cd | CI itself |
| Entrypoint-only / lean context, SHA-256, version normalised | packaging | deploy-gate |
| No telemetry + audit row (static) / health + JSON audit (server); no secret in logs | observability-and-audit | engineering-reviewer / deploy-gate |
| Full defence posture, each control a build-failing check | security-hardening | security-reviewer |
| Gated, human-confirmed deploy; non-root; port 8080; tested rollback | release-and-deploy | deploy-gate + human confirmation |
| Template, port, quality gate, env two-stage, never-do list | app-store-deployment | deploy-gate |
| House voice (Smart Brevity, UK English, no invented facts) | output-styles/house-voice.md | design-critic (advisory) |

Every standard has an owner; every standard except the two advisory ones is verified by a fail-closed gate.

## 4. Definition of done

| # | Item | State |
|---|---|---|
| 1 | Both source bundles inventoried and merged without losing context | TRUE |
| 2 | Every source area maps to an artifact (coverage table) | TRUE |
| 3 | All 20 skills present with the full completeness-limb structure | TRUE |
| 4 | Skills named in plain English and as Claude-compatible skill names | TRUE |
| 5 | Modern frontmatter (name + description; deploy skills disable-model-invocation) | TRUE |
| 6 | Both archetypes fully covered; rigour preserved from each | TRUE |
| 7 | Flat file layout rehydrating to .claude/ via REHYDRATE.md | TRUE |
| 8 | App Store doctrine embedded as the deployment backbone | TRUE |
| 9 | House rules honoured (UK English, no em-dash, no dividers, no "+") | TRUE |
| 10 | plugin.json lists all 20 skills, 4 agents, output style, hooks | TRUE |

No item is false.
