#!/usr/bin/env bash
# Regenerate requirements-runtime.txt with every published hash for each pin.
# Run after any change to tools/requirements-runtime.in or a pin in requirements.txt.
set -euo pipefail
cd "$(dirname "$0")/.."
header=$(head -4 requirements-runtime.txt)
body=$(uv pip compile tools/requirements-runtime.in --python-version 3.12 \
  --python-platform x86_64-manylinux_2_28 -q --no-header --no-annotate --generate-hashes)
printf '%s\n%s\n' "$header" "$body" > requirements-runtime.txt
echo "requirements-runtime.txt regenerated"
