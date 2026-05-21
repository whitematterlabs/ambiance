#!/usr/bin/env bash
#
# bundle-runtime.sh — embed a self-contained Python runtime (interpreter +
# all deps + the kernel package) into a built PAI.app, then re-sign the whole
# bundle inside-out so PAI.app is the single owner of the kernel.
#
# Run AFTER xcodebuild has produced PAI.app. Idempotent: wipes and rebuilds
# Contents/Resources/runtime each time.
#
#   ./bundle-runtime.sh /path/to/PAI.app
#
# Result layout:
#   PAI.app/Contents/Resources/runtime/python/   self-contained CPython 3.14
#       + all pyproject deps + the boot/sbin/bin packages (uv pip install .)
#
# The kernel reads its FHS *state* from ~/.pai at runtime; only code lives in
# the bundle. See memory pai-app-consolidation.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="${1:?usage: bundle-runtime.sh /path/to/PAI.app}"
APP="$(cd "$APP" && pwd)"
ENTITLEMENTS="$REPO_ROOT/macos/PAI/PAI.entitlements"
RUNTIME="$APP/Contents/Resources/runtime"
PYDIR="$RUNTIME/python"
PYVER="3.14"

step() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

[ -d "$APP" ] || { echo "not a bundle: $APP" >&2; exit 1; }

step "Locating a uv-managed standalone CPython $PYVER"
uv python install "$PYVER" >/dev/null 2>&1 || true
# `uv python find` from inside the repo returns the project .venv (a venv that
# symlinks out to the real interpreter — NOT self-contained). sys.base_prefix
# resolves through that symlink to the actual standalone install root, which IS
# relocatable (python-build-standalone).
PYBIN="$(uv python find "$PYVER")"
# realpath: base_prefix is itself a symlink (cpython-3.14- -> cpython-3.14.0-);
# resolve it so `cp -R` copies the real tree instead of re-creating a symlink
# that escapes the bundle (which both fails codesign AND would have us install
# into the shared managed python).
STANDALONE="$("$PYBIN" -c 'import os, sys; print(os.path.realpath(sys.base_prefix))')"
if [ ! -d "$STANDALONE/lib/python$PYVER" ] || [ ! -x "$STANDALONE/bin/python3" ]; then
    echo "could not find a self-contained CPython $PYVER (resolved: $STANDALONE)" >&2
    exit 1
fi
step "Using standalone build at $STANDALONE"

step "Copying interpreter into the bundle"
rm -rf "$RUNTIME"
mkdir -p "$RUNTIME"
# -R preserves the relocatable layout; python-build-standalone is relocatable.
cp -R "$STANDALONE" "$PYDIR"

BUNDLED_PY="$PYDIR/bin/python3"
[ -x "$BUNDLED_PY" ] || BUNDLED_PY="$PYDIR/bin/python$PYVER"
[ -x "$BUNDLED_PY" ] || { echo "no python3 in $PYDIR/bin" >&2; exit 1; }

step "Installing the kernel + all deps into the embedded interpreter"
# Drop uv's PEP-668 marker so the (now copied, no longer uv-managed) interpreter
# accepts installs. Use the bundled interpreter's OWN pip — not `uv pip`, which
# canonicalizes back to the shared managed install — so deps land unambiguously
# in the COPY's site-packages. Installs pyproject deps AND the boot/sbin/bin
# packages (hatchling wheel), so `python3 -m boot.init` needs no PYTHONPATH.
rm -f "$PYDIR/lib/python$PYVER/EXTERNALLY-MANAGED"
"$BUNDLED_PY" -m pip install --no-warn-script-location --no-input "$REPO_ROOT"

step "Pruning bloat (caches, CPython test suite)"
find "$PYDIR" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf "$PYDIR/lib/python$PYVER/test" "$PYDIR/lib/python$PYVER/idlelib" 2>/dev/null || true

step "Verifying the embedded runtime imports the kernel + deps"
# Replicate the runtime env: PAI_ROOT for the FHS, PYTHONPATH so the on-disk
# `drivers` namespace package (under ~/.pai/usr/lib) resolves — exactly what
# KernelLauncher passes the kernel child.
PAI_ROOT="${PAI_ROOT:-$HOME/.pai}" \
PYTHONPATH="${PAI_ROOT:-$HOME/.pai}/usr/lib" \
"$BUNDLED_PY" - <<'PY'
import sys
import boot.entry          # kernel (eagerly imports boot.main -> drivers)
import anthropic, textual, yaml, watchdog, croniter, requests  # heavy deps
import Contacts, EventKit  # pyobjc frameworks the drivers need
print(f"embedded python {sys.version.split()[0]} OK — kernel + deps + drivers import")
PY

step "Re-signing inside-out (ad-hoc) so the bundle seals the embedded runtime"
# Adding runtime/ to Resources invalidated Xcode's seal, so re-sign. We do NOT
# use the hardened runtime here: Xcode already drops it for ad-hoc builds ("note:
# Disabling hardened runtime with ad-hoc codesigning"), and with it OFF the
# python child process won't enforce library validation — so the ad-hoc embedded
# libpython/.so (no Team ID) load fine. On the Developer ID migration, re-enable
# --options runtime AND apply $ENTITLEMENTS to python3.14 too (its process does
# the dlopen'ing), so disable-library-validation takes effect. See entitlements.
#
# Sign inside-out: nested Mach-O first, the interpreter binary (the real file,
# not the python3/python symlinks), then re-seal the app WITHOUT --deep so the
# nested signatures we just made are preserved, not clobbered.
find "$RUNTIME" -type f \( -name "*.dylib" -o -name "*.so" \) \
    -exec codesign --force --sign - {} + 2>/dev/null || true
codesign --force --sign - "$PYDIR/bin/python$PYVER" 2>/dev/null || true
codesign --force --entitlements "$ENTITLEMENTS" --sign - "$APP"

step "Verifying the bundle signature"
codesign --verify --deep --strict "$APP"
SIZE="$(du -sh "$APP" | cut -f1)"
step "Done. PAI.app is self-contained ($SIZE), kernel owned by the app."
