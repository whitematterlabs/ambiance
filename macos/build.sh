#!/usr/bin/env bash
#
# build.sh — build the consolidated, self-contained PAI.app.
#
#   ./build.sh                  # Debug build + embed runtime
#   ./build.sh Release          # Release build + embed runtime
#   ./build.sh Release --dmg    # …and wrap the result into build/PAI.dmg
#
# Produces a single PAI.app that contains the kernel and ALL its Python
# dependencies and runs the kernel as a child it owns. Copies the result to
# ./build/PAI.app for convenience.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="Debug"
MAKE_DMG=0
for arg in "$@"; do
    case "$arg" in
        --dmg) MAKE_DMG=1 ;;
        *) CONFIG="$arg" ;;
    esac
done
DERIVED="$HERE/build/DerivedData"

step() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

step "Regenerating Xcode project from project.yml"
( cd "$HERE" && xcodegen generate )

step "Building PAI ($CONFIG)"
xcodebuild \
    -project "$HERE/PAI.xcodeproj" \
    -scheme PAI \
    -configuration "$CONFIG" \
    -derivedDataPath "$DERIVED" \
    build

APP="$DERIVED/Build/Products/$CONFIG/PAI.app"
[ -d "$APP" ] || { echo "build produced no app at $APP" >&2; exit 1; }

step "Embedding the Python runtime + kernel"
"$HERE/bundle-runtime.sh" "$APP"

step "Copying to $HERE/build/PAI.app"
rm -rf "$HERE/build/PAI.app"
cp -R "$APP" "$HERE/build/PAI.app"

step "Built: $HERE/build/PAI.app"

if [ "$MAKE_DMG" = "1" ]; then
    step "Packaging .dmg"
    "$HERE/package-dmg.sh" "$HERE/build/PAI.app"
fi
