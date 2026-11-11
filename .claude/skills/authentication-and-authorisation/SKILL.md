---
name: authentication-and-authorisation
description: Authentication and authorisation. Use when gating a route, comparing a token, configuring CORS, or reasoning about who may do what. Covers the server constant-time token compare, route gating with public health paths, fail-closed CORS in production, the single-sign-on seam, and the static posture that a client-side gate is never a security boundary.
---

# Authentication and authorisation

## Purpose and scope

Who may call what, and how that is enforced. The server app gates every cost-incurring or state-changing route behind a constant-time token compare, leaves only health paths public, and fails closed on cross-origin requests in production. The static artifact has no auth in the shipped file: any access control there is a user-experience gate only, never a security boundary. Scope is the enforcement of access. It does not cover where secrets are stored (`configuration-and-secrets`) or the wider posture (`security-hardening`).

## When to use

- Adding or gating a route on the server.
- Comparing a token or configuring CORS.
- Anyone proposes a client-side PIN, flag, or hidden field as protection.

## Prerequisites

- `configuration-and-secrets` (the token comes from the environment).
- `code-architecture` (the createApp factory and request pipeline).

## Procedure

1. **Compare the team token in constant time.** Use `crypto.timingSafeEqual` with a length guard, never `===`, so the comparison does not leak length or position through timing.
   ```js
   function tokenOk(given, expected) {
     const a = Buffer.from(String(given||"")), b = Buffer.from(String(expected||""));
     return a.length === b.length && crypto.timingSafeEqual(a, b);
   }
   ```
2. **Gate every cost-incurring or state-changing route.** Reads of public data may be open; anything that spends money or mutates shared state sits behind the token. Only health paths are public.
3. **Fail closed on CORS in production.** Read `ALLOWED_ORIGIN` from the environment; in production with a token set, a wildcard origin makes the app refuse to start rather than serve an open API.
4. **Leave a single-sign-on (SSO) seam.** The shared team token is one middleware; document it as the single place a per-user identity provider would later attach. Record that the shared-token model is an accepted limitation (`security-hardening`).
5. **Static: never gate in the browser.** A PIN or flag in client code is a convenience, not protection; state this plainly wherever it appears. The shipped artifact has no secret and no real gate.

## Decision rules

- **Is this a security boundary?** If it runs in the browser, no. The server token and server validation are the only boundaries.
- **Does this route cost money or change state?** Then it is behind the token, with no exception for "internal" callers, because the ingress is public.
- **Wildcard origin in production with a token?** Refuse to start; require an explicit `ALLOWED_ORIGIN`.
- **Per-user identity needed later?** Attach SSO at the documented seam; do not bolt per-user logic onto the shared token.

## Standards (checkable assertions)

- The token is compared with `timingSafeEqual` and a length guard, never `===`.
- Every cost-incurring or state-changing route returns 401 without a valid token and 200 with one.
- Health paths are public; nothing else is.
- Production with a token set refuses to start on a wildcard origin.
- No client-side gate is described or relied on as a security control.

## Failure modes and remedies

- **A reviewer treats a browser PIN as access control.** Fix: correct it; the server token is the boundary, the PIN is a user-experience gate.
- **Token compared with `===`.** Fix: switch to `timingSafeEqual` with a length guard.
- **An expensive route is open.** Fix: place it behind the token and the strict rate limiter (`api-and-integration`).
- **App serves an open API in prod.** Fix: set `ALLOWED_ORIGIN`; confirm the wildcard-refusal guard is present.

## Verification

`npm test` exercises 401 without a token and 200 with one on a gated route, and asserts a wildcard-origin production config with a token refuses to start. A grep confirms no `===` token compare and no client-side gate presented as protection.

## Worked example

A new endpoint triggers a paid lookup. Because it costs money it is placed behind the token middleware and the strict rate limiter; the token is checked with `timingSafeEqual`; CORS is restricted to `ALLOWED_ORIGIN`. A test sends a request with no token (401), then with the token (200). The shared-token limitation and the SSO seam are recorded in the security policy.

## Glossary

- **timingSafeEqual:** a constant-time comparison that does not leak length or position through timing.
- **CORS:** Cross-Origin Resource Sharing, the browser rule controlling which origins may call an API.
- **SSO:** Single Sign-On, a per-user identity provider; the shared token leaves a seam where SSO would attach.
- **Security boundary vs user-experience gate:** the server is a boundary; a browser-side check is not.
- Other terms: `glossary`.

## Provenance

Reconstructed and merged from the server bundle's auth middleware (constant-time compare, route gating, CORS fail-closed, SSO seam) referenced across its security-hardening skill, and the static bundle's explicit no-client-side-gate posture.
