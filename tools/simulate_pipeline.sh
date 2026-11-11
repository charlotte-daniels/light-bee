#!/usr/bin/env bash
# Simulate the App Store python template's test stage against the REAL upload
# archive, not the repository: unzip it into a clean directory, add the CI file
# the platform commits to its checkout, install exactly what the platform
# installs, and run its test command with GITLAB_CI=true. Then confirm the
# coverage report the SonarQube gate reads exists.
#
# Also checks the closure installs and the tests pass on Python 3.11 (the
# interpreter the dependency scanning stage may use), and that every hash-locked
# image wheel resolves for cp312 manylinux, as the image build will need.
#
#   bash tools/simulate_pipeline.sh            build the archive, then simulate
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "== package"
python3.12 "$root/tools/package_appstore.py" --out "$work/dist"
archive="$(ls "$work"/dist/light-bee-appstore-*.zip)"

echo "== checkout (flat archive plus the platform's own CI file)"
mkdir "$work/checkout"
python3.12 -m zipfile -e "$archive" "$work/checkout"
printf 'stages: [test]\n' > "$work/checkout/.gitlab-ci.yml"
test -f "$work/checkout/Dockerfile"

for py in python3.12 python3.11; do
  echo "== install with $py (pip install -r requirements.txt, wheels only)"
  "$py" -m venv "$work/venv-$py"
  "$work/venv-$py/bin/pip" install -q --disable-pip-version-check --only-binary :all: \
    -r "$work/checkout/requirements.txt"
done

echo "== image closure: hash-locked wheels resolve for the image (cp312, manylinux)"
"$work/venv-python3.12/bin/python" -m pip download -q --disable-pip-version-check \
  --require-hashes --only-binary :all: --no-deps --dest "$work/wheels" \
  --python-version 3.12 --implementation cp --abi cp312 --abi abi3 --abi none \
  --platform manylinux_2_17_x86_64 --platform manylinux_2_28_x86_64 --platform any \
  -r "$work/checkout/requirements-runtime.txt"

echo "== test stage on python3.11 (compatibility only)"
(
  cd "$work/checkout"
  GITLAB_CI=true "$work/venv-python3.11/bin/python" -m pytest -q -p no:cacheprovider >/dev/null
  rm -f coverage.xml .coverage
)

echo "== test stage (python3.12, GITLAB_CI=true, platform command)"
(
  cd "$work/checkout"
  GITLAB_CI=true "$work/venv-python3.12/bin/python" -m pytest -q
)

echo "== coverage artefact"
test -s "$work/checkout/coverage.xml"
python3.12 - "$work/checkout/coverage.xml" <<'PY'
import sys
import xml.etree.ElementTree as ET  # parsing our own freshly generated report

rate = float(ET.parse(sys.argv[1]).getroot().get("line-rate", "0")) * 100
print(f"coverage.xml line rate {rate:.1f}%")
sys.exit(0 if rate >= 80 else 1)
PY

echo "SIMULATION: PASS"
