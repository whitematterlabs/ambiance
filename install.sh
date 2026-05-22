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

echo "==> web frontend (pnpm)"
if command -v pnpm >/dev/null 2>&1; then
  if ( cd src/usr/libexec/web && pnpm install && pnpm build ); then
    echo "    web surface built — launch with: pai start --web"
  else
    echo "    warning: web frontend build failed; 'pai start --web' unavailable." >&2
  fi
else
  echo "    skipped: pnpm not found (https://pnpm.io)." >&2
  echo "    run 'pnpm install && pnpm build' in src/usr/libexec/web to enable 'pai start --web'." >&2
fi

echo "==> paifs-init"
uv run paifs-init --no-setup "$@"

echo "==> paisetup"
uv run paisetup || true

echo
echo "PAI installed. Runtime root: ${PAI_ROOT:-$HOME/.pai}"
