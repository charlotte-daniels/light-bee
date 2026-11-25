#!/usr/bin/env bash
# Build the container from the REAL upload archive and probe it as the App
# Store will: the image contract, the health paths on three PORT settings, the
# version on /readyz, and the logging standard on `docker logs`.
#
# Logging (blocks uploads from 1 December 2026): every stdout line one JSON
# object of the standard shape, stderr empty, no query string anywhere, and
# two requests sent with an X-Request-Id carrying every required field on
# every line written while they were handled, with one audit line each that
# adds the status, event.outcome and http.route. Untagged probes stay quiet.
#
#   bash tools/image_probe.sh                    package this version, then probe
#   bash tools/image_probe.sh ARCHIVE.zip        probe an existing archive
#
# Runs where Docker and python3.12 are (the WSL build clone). grype runs when
# installed or its image is cached; it is skipped, and says so, otherwise.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
tag="light-bee-probe:$$"
cid=""
cleanup() {
  if [ -n "$cid" ]; then docker rm -f "$cid" >/dev/null 2>&1 || true; fi
  docker rmi -f "$tag" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT
fail() { echo "FAIL  $*"; exit 1; }

version="$(python3.12 -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' "$root/pyproject.toml")"
archive="${1:-}"
if [ -z "$archive" ]; then
  python3.12 "$root/tools/package_appstore.py" --out "$work/dist" >/dev/null
  archive="$work/dist/light-bee-appstore-$version.zip"
fi
echo "== archive $(basename "$archive") ($(sha256sum "$archive" | cut -c1-16)...)"
mkdir "$work/ctx"
python3.12 -m zipfile -e "$archive" "$work/ctx"

echo "== build (--pull, from the archive)"
docker build -q --pull -t "$tag" "$work/ctx" >/dev/null
size="$(docker image inspect "$tag" --format '{{.Size}}')"
echo "   size $((size / 1000000)) MB"

echo "== contract"
uid="$(docker run --rm "$tag" id -u)"
[ "$uid" != "0" ] || fail "runs as root"
echo "   uid $uid"
for path in /opt/venv/bin/pip /usr/local/bin/pip /sbin/apk /usr/local/lib/python3.12/sqlite3; do
  if docker run --rm "$tag" test -e "$path"; then fail "$path present"; fi
done
# As root, so find can read every directory (the image user cannot read /root).
suid="$(docker run --rm --user 0 "$tag" find / -xdev \( -perm -4000 -o -perm -2000 \) -type f)"
[ -z "$suid" ] || fail "setuid or setgid files: $suid"
echo "   no pip, apk, sqlite3, setuid or setgid"

free_port() { python3.12 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'; }
wait_up() {
  for _ in $(seq 1 100); do
    if curl -fs "http://127.0.0.1:$1/healthz" >/dev/null 2>&1; then return 0; fi
    sleep 0.2
  done
  fail "no answer on port $1"
}

echo "== health paths and HEAD / on three PORT settings"
for port in 8080 9090 18081; do
  host="$(free_port)"
  cid="$(docker run -d -e PORT="$port" -p "127.0.0.1:$host:$port" "$tag")"
  wait_up "$host"
  for path in / /healthz /readyz /livez /ping; do
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$host$path")"
    [ "$code" = 200 ] || fail "GET $path on PORT $port: $code"
  done
  code="$(curl -s -o /dev/null -w '%{http_code}' -I "http://127.0.0.1:$host/")"
  [ "$code" = 200 ] || fail "HEAD / on PORT $port: $code"
  docker rm -f "$cid" >/dev/null
  cid=""
  echo "   PORT $port: all 200"
done

echo "== /readyz version and the logging standard"
host="$(free_port)"
cid="$(docker run -d -p "127.0.0.1:$host:8080" "$tag")"
wait_up "$host"
curl -fs "http://127.0.0.1:$host/readyz" | grep -q "\"$version\"" || fail "/readyz does not report $version"
echo "   /readyz $version"
# Two tagged requests: one that succeeds, one refused on a templated route.
# The query string carries a marker that must never reach a line.
curl -s -o /dev/null -H "X-Request-Id: probe-ok-1" -H "X-Auth-Request-Email: probe@example.test" \
  -A "lightbee-probe/1.0" "http://127.0.0.1:$host/api/session?password=QSTRSECRET"
curl -s -o /dev/null -H "X-Request-Id: probe-401-2" -H "X-Auth-Request-Email: probe@example.test" \
  -A "lightbee-probe/1.0" "http://127.0.0.1:$host/api/runs/no-such-run?token=QSTRSECRET"
curl -s -o /dev/null "http://127.0.0.1:$host/healthz"
sleep 1
docker stop -t 10 "$cid" >/dev/null
docker logs "$cid" >"$work/out" 2>"$work/err"
docker rm -f "$cid" >/dev/null
cid=""
[ ! -s "$work/err" ] || fail "stderr not empty: $(head -c 300 "$work/err")"

python3.12 - "$work/out" <<'PY'
import json
import sys

FIELDS = {"timestamp", "severity_text", "severity_number", "body", "attributes"}
PAIRS = {"TRACE": 1, "DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17, "FATAL": 21}
REQUIRED = (
    "http.request.header.x-request-id",
    "user.email",
    "url.path",
    "http.request.method",
    "user_agent.original",
)
EXPECTED = {
    "probe-ok-1": ("/api/session", 200, "success", "/api/session"),
    "probe-401-2": ("/api/runs/no-such-run", 401, "failure", "/api/runs/{run_id}"),
}
raw = open(sys.argv[1], encoding="utf-8").read()
problems = []
if "QSTRSECRET" in raw:
    problems.append("a query string reached the log")
parsed = []
for number, text in enumerate(raw.splitlines(), 1):
    if len(text.encode()) >= 64 * 1024:
        problems.append(f"line {number} is 64 KiB or more")
    try:
        line = json.loads(text)
    except ValueError:
        problems.append(f"line {number} is not JSON: {text[:120]}")
        continue
    if not isinstance(line, dict) or not FIELDS <= set(line) <= FIELDS | {"trace_id", "span_id"}:
        problems.append(f"line {number} has the wrong fields")
        continue
    if PAIRS.get(line["severity_text"]) != line["severity_number"]:
        problems.append(f"line {number} has a wrong severity pair")
    if any(not isinstance(v, str | int | float | bool) for v in line["attributes"].values()):
        problems.append(f"line {number} has a nested attribute")
    parsed.append(line)
for request_id, (path, status, result, route) in EXPECTED.items():
    mine = [x["attributes"] for x in parsed
            if x["attributes"].get("http.request.header.x-request-id") == request_id]
    if not mine:
        problems.append(f"{request_id}: no lines")
        continue
    for attributes in mine:
        missing = [key for key in REQUIRED if key not in attributes]
        if missing:
            problems.append(f"{request_id}: a line lacks {missing}")
        elif (attributes["url.path"], attributes["http.request.method"],
              attributes["user_agent.original"], attributes["user.email"]) != (
                  path, "GET", "lightbee-probe/1.0", "probe@example.test"):
            problems.append(f"{request_id}: wrong request fields {attributes}")
    audits = [a for a in mine if a.get("event.name") == "http.request.completed"]
    if len(audits) != 1:
        problems.append(f"{request_id}: {len(audits)} audit lines, expected 1")
    else:
        got = (audits[0].get("http.response.status_code"), audits[0].get("event.outcome"),
               audits[0].get("http.route"))
        if got != (status, result, route):
            problems.append(f"{request_id}: audit {got}, expected {(status, result, route)}")
    print(f"   {request_id}: {len(mine)} lines, every required field; audit {status} {result} {route}")
untagged = [x for x in parsed if x["attributes"].get("url.path") == "/healthz"]
if untagged:
    problems.append("an untagged probe was logged")
if problems:
    for problem in problems:
        print(f"FAIL  {problem}")
    sys.exit(1)
print(f"   {len(parsed)} lines, all JSON of the standard shape; stderr empty; no query string")
PY

echo "== vulnerability scan"
if command -v grype >/dev/null; then
  grype -q --fail-on high "$tag" && echo "   grype: 0 critical, 0 high"
elif docker image inspect anchore/grype:latest >/dev/null 2>&1; then
  docker run --rm -v /var/run/docker.sock:/var/run/docker.sock anchore/grype:latest \
    -q --fail-on high "$tag" && echo "   grype: 0 critical, 0 high"
else
  echo "SKIP  grype (not installed and no cached image)"
fi
echo "IMAGE PROBE: PASS"
