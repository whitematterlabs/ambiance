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

# --- args: <app> [--notarize] -----------------------------------------------
APP=""
NOTARIZE=0
for arg in "$@"; do
    case "$arg" in
        --notarize) NOTARIZE=1 ;;
        *) [ -z "$APP" ] && APP="$arg" ;;
    esac
done
[ -n "$APP" ] || { echo "usage: bundle-runtime.sh /path/to/PAI.app [--notarize]" >&2; exit 1; }
APP="$(cd "$APP" && pwd)"

ENTITLEMENTS="$REPO_ROOT/macos/PAI/PAI.entitlements"
RUNTIME="$APP/Contents/Resources/runtime"
PYDIR="$RUNTIME/python"
PYVER="3.14"
BREW_PREFIX="$(brew --prefix 2>/dev/null || echo /opt/homebrew)"

# Code-signing identity. Default "-" = ad-hoc (no Apple account yet). Set
# SIGN_ID="Developer ID Application: …" to produce a distributable hardened-
# runtime build: that path turns on --options runtime, stamps entitlements onto
# the embedded executables, and enables the (otherwise no-op) notarization step.
# Staged + inert until a cert exists — see macos/project.yml, [[pai-app-consolidation]].
SIGN_ID="${SIGN_ID:--}"
# Notarization reads creds from a stored keychain profile (xcrun notarytool
# store-credentials <name>). Without one the step no-ops with a clear note.
NOTARY_PROFILE="${NOTARY_PROFILE:-PAI}"

step() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

# --- signing helpers --------------------------------------------------------
# Inside-out signing. Ad-hoc ("-") leaves the hardened runtime OFF so the
# unsigned-by-team nested libs load; Developer ID turns it ON (--options
# runtime) and stamps entitlements onto the executables that dlopen/exec.
sign_nested() {  # .dylib / .so — never need entitlements
    if [ "$SIGN_ID" = "-" ]; then
        codesign --force --sign - "$1" 2>/dev/null || true
    else
        codesign --force --options runtime --timestamp --sign "$SIGN_ID" "$1"
    fi
}
sign_binary() {  # python / tmux / CoreLocationCLI — load libs, take entitlements
    if [ "$SIGN_ID" = "-" ]; then
        codesign --force --sign - "$1" 2>/dev/null || true
    else
        codesign --force --options runtime --timestamp \
            --entitlements "$ENTITLEMENTS" --sign "$SIGN_ID" "$1"
    fi
}
sign_app() {
    if [ "$SIGN_ID" = "-" ]; then
        codesign --force --entitlements "$ENTITLEMENTS" --sign - "$1"
    else
        codesign --force --options runtime --timestamp \
            --entitlements "$ENTITLEMENTS" --sign "$SIGN_ID" "$1"
    fi
}

# Vendor a Mach-O's non-system dylib deps into runtime/lib and rewrite its load
# commands to @executable_path/../lib. Recurses for transitive Homebrew deps;
# idempotent via the dest-exists guard. Run BEFORE signing (it edits the file).
vendor_deps() {
    local f="$1" dep base
    while read -r dep; do
        case "$dep" in
            ""|/usr/lib/*|/System/*|@*) continue ;;
        esac
        base="$(basename "$dep")"
        install_name_tool -change "$dep" "@executable_path/../lib/$base" "$f" 2>/dev/null || true
        if [ ! -f "$RUNTIME/lib/$base" ]; then
            mkdir -p "$RUNTIME/lib"
            cp "$dep" "$RUNTIME/lib/$base"
            chmod u+w "$RUNTIME/lib/$base"
            install_name_tool -id "@executable_path/../lib/$base" "$RUNTIME/lib/$base" 2>/dev/null || true
            vendor_deps "$RUNTIME/lib/$base"
        fi
    done < <(otool -L "$f" | tail -n +2 | awk '{print $1}')
}

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

step "Staging the first-run seed (etc + docs)"
# paifs_init --bundle-mode copies these into ~/.pai instead of symlinking the
# repo (there is none). Mirrors BUNDLE_SEED_CONTENT in src/bin/paifs_init.py:
# <seed>/etc -> etc slots, <seed>/doc -> usr/share/doc.
SEED="$APP/Contents/Resources/seed"
rm -rf "$SEED"
mkdir -p "$SEED"
cp -R "$REPO_ROOT/src/etc" "$SEED/etc"
cp -R "$REPO_ROOT/src/usr/share/doc" "$SEED/doc"

step "Staging the pairegistry into the bundle (paiman reads it via PAIMAN_REGISTRY at first run)"
# Self-contained transport: a friend's fresh Mac has no git, no network promise,
# no access to the public registry remote. Bundle the local checkout in and
# point Provisioner's PAIMAN_REGISTRY at it. Hard-fail when absent — silently
# shipping a registry-less .app is the same bug class as the silent dev fallback.
PAIREGISTRY="${PAIREGISTRY:-$HOME/Projects/pairegistry}"
if [ -d "$PAIREGISTRY" ]; then
    cp -R "$PAIREGISTRY" "$SEED/registry"
    rm -rf "$SEED/registry/.git"
else
    echo "bundle-runtime.sh: pairegistry not found at $PAIREGISTRY" >&2
    exit 1
fi

step "Bundling system binaries the kernel shells out to (tmux, CoreLocationCLI)"
# A Finder-launched, distributed app can't assume Homebrew. Vendor the two
# binaries the kernel needs onto PATH (KernelLauncher prepends runtime/bin).
mkdir -p "$RUNTIME/bin"
# CoreLocationCLI: links only system frameworks — clean copy, no vendoring.
CLL="$BREW_PREFIX/bin/CoreLocationCLI"
if [ -x "$CLL" ]; then
    cp "$CLL" "$RUNTIME/bin/CoreLocationCLI"
    chmod u+w "$RUNTIME/bin/CoreLocationCLI"
else
    echo "warning: CoreLocationCLI not found at $CLL; the per-turn location header will be unavailable" >&2
fi
# tmux: viewer-only (shell_tool spawns a per-PAI viewer). Vendor it + its
# Homebrew dylibs and rewrite load commands to @executable_path/../lib.
TMUX="$BREW_PREFIX/bin/tmux"
if [ -x "$TMUX" ]; then
    cp "$TMUX" "$RUNTIME/bin/tmux"
    chmod u+w "$RUNTIME/bin/tmux"
    vendor_deps "$RUNTIME/bin/tmux"
else
    echo "warning: tmux not found at $TMUX; PAI viewers will degrade gracefully" >&2
fi
# ngrok: the remote-access tunnel (opt-in "Enable remote access"). A standalone
# Go binary that links only system frameworks — clean copy, no vendoring. Dev
# builds fall back to `ngrok` on PATH (TunnelLauncher resolves via env(1)).
NGROK="$(command -v ngrok || echo "$BREW_PREFIX/bin/ngrok")"
if [ -x "$NGROK" ]; then
    cp "$NGROK" "$RUNTIME/bin/ngrok"
    chmod u+w "$RUNTIME/bin/ngrok"
else
    echo "warning: ngrok not found; remote access will be unavailable in this build" >&2
fi

step "Pruning bloat (caches, CPython test suite, unused stdlib)"
find "$PYDIR" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
# Top-level stdlib we never use: GUI (tkinter/turtledemo/idlelib), the pip
# bootstrap (ensurepip — pip itself stays in site-packages), and the 2to3
# machinery. The CPython test suite + any package-level test dirs go too.
for d in test idlelib tkinter turtledemo turtle.py ensurepip lib2to3; do
    rm -rf "$PYDIR/lib/python$PYVER/$d" 2>/dev/null || true
done
find "$PYDIR/lib/python$PYVER" -type d \( -name test -o -name tests \) \
    -prune -exec rm -rf {} + 2>/dev/null || true

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

step "Stripping embedded shared libraries"
# -x strips local (non-global) symbols; safe for loadable libs. MUST run after
# the import verification (strip invalidates the interpreter's signature, so a
# pre-sign python run would be SIGKILL'd) and before signing (signing seals the
# stripped file). Best-effort: a strip failure shouldn't abort the build.
find "$RUNTIME" -type f \( -name "*.dylib" -o -name "*.so" \) \
    -exec strip -x {} + 2>/dev/null || true

step "Re-signing inside-out ($([ "$SIGN_ID" = "-" ] && echo ad-hoc || echo "$SIGN_ID"))"
# Adding runtime/ to Resources invalidated Xcode's seal, so re-sign. Ad-hoc:
# Xcode drops the hardened runtime ("note: Disabling hardened runtime with
# ad-hoc codesigning"), and with it OFF the python child won't enforce library
# validation — so the ad-hoc embedded libpython/.so (no Team ID) load fine.
# Developer ID (SIGN_ID set): sign_* turn --options runtime back ON and stamp
# $ENTITLEMENTS onto python + the bundled binaries so disable-library-validation
# takes effect. See PAI.entitlements.
#
# Sign inside-out: nested Mach-O first, then the executables (the real interpreter
# file — not the python3/python symlinks — plus the bundled tmux/CoreLocationCLI),
# then re-seal the app WITHOUT --deep so the nested signatures are preserved.
while IFS= read -r -d '' lib; do
    sign_nested "$lib"
done < <(find "$RUNTIME" -type f \( -name "*.dylib" -o -name "*.so" \) -print0)
sign_binary "$PYDIR/bin/python$PYVER"
[ -f "$RUNTIME/bin/tmux" ] && sign_binary "$RUNTIME/bin/tmux"
[ -f "$RUNTIME/bin/CoreLocationCLI" ] && sign_binary "$RUNTIME/bin/CoreLocationCLI"
sign_app "$APP"

step "Verifying the bundle signature"
codesign --verify --deep --strict "$APP"

if [ -f "$RUNTIME/bin/tmux" ]; then
    step "Verifying bundled tmux resolves its libs off Homebrew"
    # Scrub Homebrew from PATH and DYLD so a stray system copy can't mask a
    # broken @executable_path rewrite. tmux -V is deterministic (no server).
    if env -i PATH="/usr/bin:/bin" "$RUNTIME/bin/tmux" -V >/dev/null 2>&1; then
        otool -L "$RUNTIME/bin/tmux" | grep -q "@executable_path/../lib/" \
            && echo "    ok: tmux runs and links @executable_path/../lib"
    else
        echo "warning: bundled tmux failed to run with a scrubbed PATH" >&2
    fi
fi

if [ "$NOTARIZE" = "1" ]; then
    if [ "$SIGN_ID" = "-" ]; then
        echo "note: --notarize ignored for an ad-hoc build (needs a Developer ID SIGN_ID)" >&2
    elif ! xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" >/dev/null 2>&1; then
        echo "note: no notarytool credentials for profile '$NOTARY_PROFILE'; skipping." >&2
        echo "      create one with: xcrun notarytool store-credentials $NOTARY_PROFILE" >&2
    else
        step "Notarizing (submit + staple)"
        ZIP="$(dirname "$APP")/$(basename "$APP" .app)-notarize.zip"
        ditto -c -k --keepParent "$APP" "$ZIP"
        xcrun notarytool submit "$ZIP" --keychain-profile "$NOTARY_PROFILE" --wait
        xcrun stapler staple "$APP"
        rm -f "$ZIP"
    fi
fi

SIZE="$(du -sh "$APP" | cut -f1)"
step "Done. PAI.app is self-contained ($SIZE), kernel owned by the app."
