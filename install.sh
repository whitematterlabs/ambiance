#!/usr/bin/env bash
# PAI bootstrap: install deps, then provision ~/.pai/.
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv not found. install it from https://docs.astral.sh/uv/ and re-run." >&2
  exit 1
fi

echo "==> uv sync"
uv sync

echo "==> paifs-init"
uv run paifs-init --no-setup "$@"

echo "==> paisetup"
uv run paisetup || true

echo
echo "PAI installed. Runtime root: ${PAI_ROOT:-$HOME/.pai}"
