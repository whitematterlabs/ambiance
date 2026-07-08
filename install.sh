#!/usr/bin/env bash
# PAI bootstrap — the `curl … | sh` install/update target.
#
#   curl -fsSL https://raw.githubusercontent.com/whitematterlabs/pai/main/install.sh | sh
#
# Zero prerequisites on the target machine: this script installs the `uv`
# static binary itself, downloads a prebuilt release tarball (source +
# uv.lock + prebuilt web dist/), and lets `uv sync` pull prebuilt wheels —
# no Node, no compiler, no git required.
#
# Test/dev override: set PAI_LOCAL_TARBALL=/path/to/pai.tar.gz to install from
# a local artifact (e.g. one just built by `pairelease`) instead of fetching a
# published release. PAI_VERSION pins the version dir; otherwise it is read
# from a sibling version.txt, else timestamped.
set -euo pipefail

PAI_ROOT="${PAI_ROOT:-$HOME/.pai}"
ENV_FILE="$PAI_ROOT/.env"
CONFIG_FILE="$PAI_ROOT/etc/config.yaml"
RELEASE_BASE="${PAI_RELEASE_BASE:-https://github.com/whitematterlabs/pai/releases/latest/download}"

# Interactivity for a `curl … | sh` install. The catch: stdin is the *pipe*
# carrying this very script, NOT the user's terminal — so `[ -t 0 ]` is false
# and naive `read`s would silently self-skip. That's the bug where the one-liner
# seeded a keyless default install with no provider choice and no package
# picker. Detect a human by stdout being a tty plus a readable controlling
# terminal, and read every prompt from /dev/tty (never stdin) below.
interactive=0
if [ -t 1 ] && [ -r /dev/tty ]; then interactive=1; fi

# --- Full Disk Access (macOS) --------------------------------------------------
# Asked for up front because the driver setup hooks at the end of this install
# (email archive backfill, iMessage history) read TCC-protected files (Mail's
# Envelope Index, Messages' chat.db). Without FDA those hooks bail with a
# printed hint that scrolls away, and the archives start at install day.
# macOS has no programmatic FDA prompt — the owner must toggle the terminal
# app on in System Settings — so deep-link the pane and wait for the toggle.
# FDA is granted to the terminal app and inherited by everything it spawns,
# including the kernel, so this one grant covers the whole runtime.
_fda_ok() {
  # access(2) lies under TCC, so probe by actually reading a byte of the
  # user-level TCC db — it always exists and is unreadable without FDA.
  head -c1 "$HOME/Library/Application Support/com.apple.TCC/TCC.db" >/dev/null 2>&1
}
if [ "$(uname)" = "Darwin" ] && [ "$interactive" = "1" ] && ! _fda_ok; then
  TERM_APP="${TERM_PROGRAM:-your terminal}"
  echo "==> Full Disk Access"
  echo "    PAI builds on-disk archives of your mail and messages. That needs"
  echo "    Full Disk Access for ${TERM_APP}, and granting it now lets the"
  echo "    history backfill run automatically at the end of this install."
  echo "    Opening: System Settings → Privacy & Security → Full Disk Access"
  echo "    — toggle ${TERM_APP} ON."
  open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles" 2>/dev/null || true
  while true; do
    printf "    Press Enter once granted (or 's' to skip): "
    IFS= read -r _fda_ans < /dev/tty || _fda_ans="s"
    case "$_fda_ans" in
      s|S)
        echo "    skipping — mail/message history will start at install day."
        echo "    Grant FDA later, then run:"
        echo "      cd \"$PAI_ROOT\" && usr/bin/python -m drivers.email.macmail.backfill"
        break
        ;;
    esac
    if _fda_ok; then
      echo "    Full Disk Access: granted."
      break
    fi
    echo "    Still not readable. If you did toggle ${TERM_APP} on, quit it"
    echo "    entirely, reopen it, and re-run this installer — it is safe to re-run."
  done
fi

# --- uv ----------------------------------------------------------------------
# Install the uv static binary if it isn't already reachable. uv drives the
# whole Python provisioning chain (venv + lockfile sync).
if ! command -v uv >/dev/null 2>&1; then
  echo "==> installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv's installer drops the binary in ~/.local/bin (or $XDG_BIN_HOME); make it
  # reachable for the rest of this shell.
  export PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$HOME/.cargo/bin:$PATH"
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv install did not put 'uv' on PATH. Open a new shell or add ~/.local/bin to PATH and re-run." >&2
  exit 1
fi

# --- obtain the release tarball ---------------------------------------------
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
TARBALL="$WORK/pai.tar.gz"

_sha256_of() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    sha256sum "$1" | awk '{print $1}'
  fi
}

if [ -n "${PAI_LOCAL_TARBALL:-}" ]; then
  echo "==> using local tarball: $PAI_LOCAL_TARBALL"
  cp "$PAI_LOCAL_TARBALL" "$TARBALL"
  if [ -n "${PAI_VERSION:-}" ]; then
    VER="$PAI_VERSION"
  elif [ -f "$(dirname "$PAI_LOCAL_TARBALL")/version.txt" ]; then
    VER="$(tr -d '[:space:]' < "$(dirname "$PAI_LOCAL_TARBALL")/version.txt")"
  else
    VER="local-$(date +%Y%m%d%H%M%S)"
  fi
else
  echo "==> resolving latest version"
  VER="$(curl -fsSL "$RELEASE_BASE/version.txt" | tr -d '[:space:]')"
  if [ -z "$VER" ]; then
    echo "error: could not resolve the latest version from $RELEASE_BASE/version.txt" >&2
    exit 1
  fi
  echo "    version $VER"
  echo "==> downloading pai.tar.gz"
  curl -fsSL "$RELEASE_BASE/pai.tar.gz" -o "$TARBALL"
  curl -fsSL "$RELEASE_BASE/pai.tar.gz.sha256" -o "$WORK/pai.tar.gz.sha256"
  echo "==> verifying checksum"
  EXPECTED="$(awk '{print $1}' "$WORK/pai.tar.gz.sha256")"
  ACTUAL="$(_sha256_of "$TARBALL")"
  if [ "$EXPECTED" != "$ACTUAL" ]; then
    echo "error: checksum mismatch (expected $EXPECTED, got $ACTUAL)" >&2
    exit 1
  fi
fi

# --- extract into the versioned code dir ------------------------------------
VER_DIR="$PAI_ROOT/opt/pai/$VER"
echo "==> extracting to $VER_DIR"
rm -rf "$VER_DIR"
mkdir -p "$VER_DIR"
tar -xzf "$TARBALL" -C "$VER_DIR"
# Repoint the rollback/bookkeeping pointer. Runtime resolution does not depend
# on it (paifs-init runs from the concrete <ver> dir below), but `pai update`
# and rollback use it to find the prior version.
ln -sfn "$VER" "$PAI_ROOT/opt/pai/current"

# --- python env from the lockfile -------------------------------------------
echo "==> uv sync"
( cd "$VER_DIR" && uv sync )

# --- default model -----------------------------------------------------------
# Pick the provider+model the fleet boots on. The choice is baked into the seed
# config.yaml (paifs-init below) and decides which single API key we ask for.
# config.yaml is never overwritten, so on a re-run we reuse the existing choice.
PROVIDER=""
MODEL=""
if [ -f "$CONFIG_FILE" ]; then
  PROVIDER="$(grep -m1 -E '^[[:space:]]*provider:' "$CONFIG_FILE" | awk '{print $2}')"
  echo "==> config.yaml exists — keeping default provider: ${PROVIDER:-unknown}"
elif [ "$interactive" -eq 1 ]; then
  echo
  echo "Which model should PAI use by default?"
  echo "  1) Claude Opus   (Anthropic)"
  echo "  2) DeepSeek"
  echo "  3) GPT-5.5       (OpenAI)"
  echo "  4) GLM-5.2       (z.ai)"
  printf "Choose [1/2/3/4] (default 1): "
  read -r choice < /dev/tty
  case "$choice" in
    2) PROVIDER=deepseek;  MODEL=deepseek-v4-pro ;;
    3) PROVIDER=openai;    MODEL=gpt-5.5 ;;
    4) PROVIDER=zai;       MODEL=glm-5.2 ;;
    *) PROVIDER=anthropic; MODEL=claude-opus-4-8 ;;
  esac
  echo "    default model: $MODEL ($PROVIDER)"
else
  echo "==> non-interactive shell — seeding the default."
fi

# --- provision the FHS -------------------------------------------------------
# Run paifs-init from the concrete version dir so it rewrites usr/src / boot /
# web / doc symlinks, the _pai_src.pth, and the bin/sbin shims to point at
# THIS <ver>. This is also the update mechanism: a new <ver> just re-runs this.
# Send capabilities (email/iMessage) default OFF and are seeded into
# config.yaml's `capabilities:` block; flip + reload later to change.
echo "==> paifs-init"
if [ -n "$PROVIDER" ] && [ -n "$MODEL" ]; then
  ( cd "$VER_DIR" && uv run paifs-init --no-setup --default-provider "$PROVIDER" --default-model "$MODEL" )
else
  ( cd "$VER_DIR" && uv run paifs-init --no-setup )
fi

# --- release marker ----------------------------------------------------------
# Records the installed version and signals `pai update` that this is a tarball
# install (vs a dev git checkout), so it takes the download-and-swap path.
mkdir -p "$PAI_ROOT/var/lib"
printf '%s\n' "$VER" > "$PAI_ROOT/var/lib/.release"
# Record the installed tarball's sha so `pai update` can detect a same-version
# rebuild (the release ships a rolling `latest` under a stable version string).
printf '%s  pai.tar.gz\n' "$(_sha256_of "$TARBALL")" > "$PAI_ROOT/var/lib/.release.sha256"

# --- API key -----------------------------------------------------------------
# Ask only for the chosen provider's key, and only if it isn't already reachable
# (shell env or a .env PAI already loads). Stored in $PAI_ROOT/.env — the
# precedence-1 location boot/__init__.py reads (and the only one a .app sees).
ensure_api_key() {
  local provider="$1" var=""
  case "$provider" in
    anthropic) var=ANTHROPIC_API_KEY ;;
    deepseek)  var=DEEPSEEK_API_KEY ;;
    openai)    var=OPENAI_API_KEY ;;
    zai)       var=ZAI_API_KEY ;;
    *) return 0 ;;
  esac
  if [ -n "${!var:-}" ]; then
    echo "    $var found in environment — not stored."
    return 0
  fi
  for f in "$PAI_ROOT/.env.local" "$PAI_ROOT/.env"; do
    if [ -f "$f" ] && grep -qE "^${var}=" "$f"; then
      echo "    $var found in $f."
      return 0
    fi
  done
  if [ "$interactive" -ne 1 ]; then
    echo "    warning: $var not set. Add it to $ENV_FILE before starting PAI." >&2
    return 0
  fi
  printf "Enter %s (input hidden, leave blank to skip): " "$var"
  read -rs key < /dev/tty; echo
  if [ -z "$key" ]; then
    echo "    skipped — add $var to $ENV_FILE before starting PAI." >&2
    return 0
  fi
  mkdir -p "$PAI_ROOT"
  printf '%s=%s\n' "$var" "$key" >> "$ENV_FILE"
  chmod 600 "$ENV_FILE" 2>/dev/null || true
  echo "    saved $var to $ENV_FILE"
}

if [ -n "$PROVIDER" ]; then
  echo "==> API key"
  ensure_api_key "$PROVIDER"
fi

# --- ElevenLabs voice key (optional) -----------------------------------------
# Cloud text-to-speech for reading replies aloud. Optional by design: with no
# key PAI uses the macOS system voice ("Siri") instead, and the owner can flip
# between the two anytime from the Voice menu in the web console. Stored in
# $PAI_ROOT/.env alongside the provider key.
ensure_elevenlabs_key() {
  local var=ELEVENLABS_API_KEY
  if [ -n "${!var:-}" ]; then
    echo "    $var found in environment — not stored."
    return 0
  fi
  for f in "$PAI_ROOT/.env.local" "$PAI_ROOT/.env"; do
    if [ -f "$f" ] && grep -qE "^${var}=" "$f"; then
      echo "    $var found in $f."
      return 0
    fi
  done
  if [ "$interactive" -ne 1 ]; then
    echo "    $var not set — PAI will read replies aloud with the macOS voice (Siri)."
    return 0
  fi
  echo "ElevenLabs gives PAI a natural cloud voice for reading replies aloud."
  echo "Skip it and PAI uses the macOS system voice (Siri) instead — switch"
  echo "anytime from the Voice menu in the web console."
  printf "Enter %s (input hidden, leave blank to use Siri): " "$var"
  read -rs key < /dev/tty; echo
  if [ -z "$key" ]; then
    echo "    skipped — PAI will read replies aloud with Siri."
    return 0
  fi
  mkdir -p "$PAI_ROOT"
  printf '%s=%s\n' "$var" "$key" >> "$ENV_FILE"
  chmod 600 "$ENV_FILE" 2>/dev/null || true
  echo "    saved $var to $ENV_FILE"
}

echo "==> ElevenLabs voice key (optional)"
ensure_elevenlabs_key

# --- guided first-run setup --------------------------------------------------
# paisetup's curses picker (and its own key prompt) need the terminal on stdin,
# which under `curl … | sh` is the pipe — so feed it /dev/tty. Skip cleanly when
# there's no terminal at all (true headless / piped-output install).
echo "==> paisetup"
if [ "$interactive" -eq 1 ]; then
  ( cd "$VER_DIR" && uv run paisetup < /dev/tty ) || true
else
  echo "    non-interactive — skipping package picker. Run 'paisetup' later to add packages."
fi

echo
echo "PAI $VER installed. Runtime root: $PAI_ROOT"

# --- start now ---------------------------------------------------------------
# Don't leave the user at a shell wondering what's next: offer to launch PAI
# right here. `pai start` runs the web console in the foreground, so exec hands
# the terminal straight to it.
if [ "$interactive" -eq 1 ]; then
  echo
  printf "Start PAI now? [Y/n]: "
  read -r start_choice < /dev/tty
  case "$start_choice" in
    [Nn]*) echo "Start it later with: pai start" ;;
    *)     echo "==> starting PAI"; cd "$VER_DIR" && exec uv run pai start < /dev/tty ;;
  esac
else
  echo "Start it with: pai start    (or update later with: pai update)"
fi
