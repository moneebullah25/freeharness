#!/usr/bin/env bash
# Build the sdist + wheel from a clean tree and write SHA-256 checksums next to them.
# Run in CI (release workflow) or locally:  ./scripts/release.sh
set -euo pipefail
cd "$(dirname "$0")/.."
git diff --quiet && git diff --cached --quiet || { echo "refusing to build from a dirty tree" >&2; exit 1; }
rm -rf dist build
python3 -m build --sdist --wheel
( cd dist && sha256sum * > SHA256SUMS && cat SHA256SUMS )
echo "verify with:  cd dist && sha256sum -c SHA256SUMS"
