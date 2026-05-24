#!/usr/bin/env bash
#
# package-dmg.sh — wrap the built PAI.app into a drag-install .dmg.
#
#   ./package-dmg.sh [/path/to/PAI.app] [/path/to/out.dmg]
#
# Defaults: build/PAI.app -> build/PAI.dmg (what ./paibuild produces). Pure
# packaging — nothing runs at install; the user drags PAI.app to Applications,
# launches it, and it provisions ~/.pai on first run.
#
# Prefers `create-dmg` (nicer window layout) if installed; otherwise falls back
# to `hdiutil` with an Applications symlink.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="${1:-$HERE/build/PAI.app}"
DMG="${2:-$HERE/build/PAI.dmg}"
VOLNAME="PAI"

step() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

[ -d "$APP" ] || { echo "no app at $APP — run ./paibuild first" >&2; exit 1; }
APP="$(cd "$APP" && pwd)"
mkdir -p "$(dirname "$DMG")"
rm -f "$DMG"

if command -v create-dmg >/dev/null 2>&1; then
    step "Packaging with create-dmg"
    # create-dmg exits non-zero if it can't set the (cosmetic) window layout on
    # a headless machine; the .dmg is still produced, so don't abort on that.
    create-dmg \
        --volname "$VOLNAME" \
        --window-pos 200 120 \
        --window-size 600 400 \
        --icon-size 100 \
        --icon "PAI.app" 150 200 \
        --hide-extension "PAI.app" \
        --app-drop-link 450 200 \
        "$DMG" "$APP" || true
    [ -f "$DMG" ] || { echo "create-dmg produced no $DMG" >&2; exit 1; }
else
    step "create-dmg not found; using hdiutil"
    STAGE="$(mktemp -d)"
    cp -R "$APP" "$STAGE/"
    ln -s /Applications "$STAGE/Applications"
    hdiutil create \
        -volname "$VOLNAME" \
        -srcfolder "$STAGE" \
        -ov -format UDZO \
        "$DMG"
    rm -rf "$STAGE"
fi

SIZE="$(du -sh "$DMG" | cut -f1)"
step "Built $DMG ($SIZE)"
