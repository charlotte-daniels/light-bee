# Container scan, 1.1.0

Local scan of the runtime image before the 1.1.0 upload, 12 November 2026. Scanner: grype 0.121.3 (`anchore/grype:latest`), run against the image built with `docker build --pull` from the Dockerfile inside `light-bee-appstore-1.1.0.zip` (commit 965dbfd), on Docker 29.7.2 under Ubuntu 24.04 (WSL 2). Container Scan on the platform remains the binding check: every critical and high finding must be resolved.

| Severity | Count |
|---|---|
| Critical | 0 |
| High | 0 |
| Medium | 9 |
| Low | 1 |

The mediums are the same families as 1.0.0: six in the Python 3.12.16 interpreter (fixed only in 3.13 or later), and CVE-2025-60876 in BusyBox, `busybox-binsh` and `ssl_client` with no fixed build. The low and the negligible are in the interpreter.

Image facts: 43.6 MB, two layers (flattened `FROM scratch`), user 10001:10001, no pip, no apk binary, no setuid or setgid files, `sqlite3` cannot be imported. hadolint clean. `/`, `/healthz`, `/readyz`, `/livez` and `/ping` answer 200, and `HEAD /` 200, with `PORT` 8080, 9090 and unset; `/readyz` reports 1.1.0 with `started_unix`, `uptime_seconds` and `peak_memory_mb` (about 69 at idle).

Logging, read from `docker logs` across start, probes, one API request carrying `X-Request-Id`, `X-Auth-Request-Email` and a query string, and SIGTERM: every stdout line is one JSON object with exactly `timestamp`, `severity_text`, `severity_number`, `body` and flat `attributes`; standard error is empty; no `uvicorn.access` line and no query string appear; the API request's one `lightbee.http` line carries the request ID and the email; probes are not logged.

The first 1.1 image build (commit 8b823b8) failed this logging check: uvicorn's access lines reached standard output with the query string (`GET /api/session?basis=raw&secret=x`). Fixed in 965dbfd and re-proved above.
