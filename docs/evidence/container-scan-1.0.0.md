# Container scan evidence for 1.0.0

Local scans of the runtime image before the first upload, 11 November 2026. Scanner: grype 0.121.3 (`anchore/grype:latest`), vulnerability database built 2026-11-11T06:21:32Z, run against the image built from the upload archive's Dockerfile on Docker 29.7.2 under Ubuntu 24.04 (WSL 2). Run with the owner's approval. Container Scan on the platform remains the binding check; its approval rule, learned from DRIFTER on 3 November 2026, is that every critical and high finding must be resolved.

## Decision

The base image moves from `python:3.12-slim` (Debian 13, trixie) to `python:3.12-alpine` (Alpine 3.24), owner's decision, 11 November 2026. Debian cannot pass: 59 of its high findings are marked "won't fix" by Debian, so no upgrade clears them.

## Debian build (commit 4a5d9ff, not uploaded)

Image Python 3.12.14 from a stale locally cached base, 87 dpkg packages, no pip, no setuid or setgid bits, health contract met.

| Severity | Fix available | Won't fix | No fix yet |
|---|---|---|---|
| Critical | 2 | 0 | 0 |
| High | 19 | 59 | 0 |
| Medium | 28 | 59 | 0 |

The criticals were the 3.12.14 interpreter (fixed in 3.12.15 and 3.12.16). The won't-fix highs were in bsdutils, util-linux and its libraries, glibc, the gcc 14 runtime, libacl, ncurses, sqlite and perl-base.

## Bare base images, for the choice

| Base | Python | Critical | High |
|---|---|---|---|
| `python:3.12-slim` | 3.12.16 | 0 | 2 with a fix (zlib), 59 won't fix |
| `python:3.12-alpine` | 3.12.16 | 1 with a fix (zlib) | 9 with a fix (krb5), 4 with no fix (sqlite-libs) |
| `python:3.14-alpine` | 3.14.10 | 1 with a fix (zlib) | 4 with no fix (sqlite-libs) |

The `python:3.14-alpine` row may reflect an older cached image: Docker Hub dropped the connection on its pull.

## Alpine build (the 1.0.0 Dockerfile)

`apk upgrade` clears zlib and krb5. SQLite is removed: Light Bee never imports `sqlite3`, and the base pins `sqlite-libs` only through its `.python-rundeps` virtual package, which the Dockerfile replaces with an identical list minus `libsqlite3`.

| Severity | Count | State |
|---|---|---|
| Critical | 0 | |
| High | 0 | |
| Medium | 6 | Python 3.12.16 interpreter, fixed only in 3.13 or later |
| Medium | 3 | BusyBox 1.37.0-r31 (CVE-2025-60876), no fix yet |
| Low | 1 | |
| Negligible | 1 | |

Image facts: 43.6 MB, Alpine 3.24.4, Python 3.12.16, 37 apk packages, no pip, no apk binary, no setuid or setgid bits, runs as 10001:10001. `/`, `/healthz`, `/readyz`, `/livez` and `/ping` answer 200 with `PORT` injected; `/readyz` reports 1.0.0. numpy 2.4.6 and sgp4 2.27 load from their musllinux wheels and propagate (a GEO element set gives a 42,167 km radius, error code 0); `sqlite3` cannot be imported. hadolint clean.

## Residual risk

The medium findings would not fail the approval rule as recorded. If the platform's scanner rates any of them high, or the BusyBox finding gains a severity change, the fix routes are a newer base on re-upload (the tag floats) or Python 3.14, which clears the interpreter findings.
