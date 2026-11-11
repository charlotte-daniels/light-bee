#!/usr/bin/env bash
# The whole local verification loop, in one command, so no step is skipped
# before a commit. Exits non-zero on the first failure.
set -euo pipefail
cd "$(dirname "$0")/.."
py=.venv/bin/python
# One command per line: under `set -e`, a failure on the left of `&&` does not
# stop the script, which once let a lint failure through as a pass.
echo "== ruff";        .venv/bin/ruff check .
                       .venv/bin/ruff format --check . >/dev/null
echo "== mypy";        .venv/bin/mypy
echo "== gates";       "$py" tools/gates.py | tail -1
echo "== proofs";      "$py" tools/prove_gates.py | tail -1
echo "== tests";       .venv/bin/pytest -q | tail -1
echo "== tool tests";  .venv/bin/pytest -q tools/tests --no-cov -o pythonpath="src tools ." | tail -1
if command -v node >/dev/null; then
  echo "== js tests";  node --test tools/js_tests/*.test.mjs | grep -E "^# (pass|fail)"
                       node --test tools/js_tests/*.test.mjs >/dev/null
else
  echo "SKIP  js tests (node not installed)"
fi
smoke=no
if command -v node >/dev/null; then
  if node -e "require.resolve('playwright')" 2>/dev/null; then smoke=yes; fi
fi
if [ "$smoke" = yes ]; then
  echo "== browser smoke"
  PYTHONPATH=src "$py" tools/dev_server.py --port 8799 >/dev/null 2>&1 &
  server=$!
  trap 'kill $server 2>/dev/null || true' EXIT
  sleep 4
  node tools/browser_smoke.mjs http://127.0.0.1:8799 "${TMPDIR:-/tmp}/lightbee-smoke" | tail -1
  kill $server 2>/dev/null || true
  trap - EXIT
else
  echo "SKIP  browser smoke (Playwright not available)"
fi
# hadolint from the project venv or PATH only, never a world-writable location.
lint="$(command -v .venv/bin/hadolint || command -v hadolint || true)"
if [ -n "$lint" ]; then
  echo "== hadolint";  "$lint" Dockerfile
  echo "LOOP: PASS"
else
  echo "SKIP  hadolint (not installed; the Dockerfile gate still ran)"
  echo "LOOP: PASS (hadolint skipped)"
fi
