#!/usr/bin/env bash
# Install freeharness for the current user with pipx (isolated environment, `freeharness` on PATH).
#   ./scripts/install.sh            # from a checkout
#   ./scripts/install.sh <wheel>    # from a release wheel (verify its checksum first: see scripts/release.sh)
set -euo pipefail
SRC="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
command -v python3 >/dev/null || { echo "python3 >= 3.10 is required" >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "python >= 3.10 is required" >&2; exit 1; }
if ! command -v pipx >/dev/null; then
  echo "pipx not found; install it first (apt install pipx / brew install pipx / python3 -m pip install --user pipx)" >&2
  exit 1
fi
pipx install --force "$SRC"
freeharness setup
echo
echo "Next: freeharness login    # sign in once"
echo "      freeharness doctor   # verify"
