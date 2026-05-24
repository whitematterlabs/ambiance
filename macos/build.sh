#!/usr/bin/env bash
#
# Compatibility wrapper. The build implementation lives at repo-root
# ./paibuild so it stays dev-only and is never installed into ~/.pai.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

args=()
has_config=0
for arg in "$@"; do
    case "$arg" in
        Debug|Release) has_config=1 ;;
    esac
done
if [ "$has_config" -eq 0 ]; then
    # Preserve the old `macos/build.sh` default while `./paibuild` defaults to
    # Release as the production deliverable path.
    args+=(--debug)
fi

exec "$HERE/../paibuild" "${args[@]}" "$@"
