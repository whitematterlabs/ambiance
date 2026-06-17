#!/usr/bin/env bash
# PAI bootstrap: install deps, then provision ~/.pai/.
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv not found. install it from https://docs.astral.sh/uv/ and re-run." >&2
  exit 1
fi

PAI_ROOT="${PAI_ROOT:-$HOME/.pai}"
ENV_FILE="$PAI_ROOT/.env"
CONFIG_FILE="$PAI_ROOT/etc/config.yaml"

interactive=0
if [ -t 0 ] && [ -t 1 ]; then interactive=1; fi

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
  printf "Choose [1/2/3] (default 1): "
  read -r choice
  case "$choice" in
    2) PROVIDER=deepseek;  MODEL=deepseek-v4-pro ;;
    3) PROVIDER=openai;    MODEL=gpt-5.5 ;;
    *) PROVIDER=anthropic; MODEL=claude-opus-4-7 ;;
  esac
  echo "    default model: $MODEL ($PROVIDER)"
else
  echo "==> non-interactive shell — seeding the deepseek default."
fi

echo "==> paifs-init"
if [ -n "$PROVIDER" ] && [ -n "$MODEL" ]; then
  uv run paifs-init --no-setup --default-provider "$PROVIDER" --default-model "$MODEL" "$@"
else
  uv run paifs-init --no-setup "$@"
fi

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
    *) return 0 ;;
  esac
  if [ -n "${!var:-}" ]; then
    echo "    $var found in environment — not stored."
    return 0
  fi
  for f in "$PAI_ROOT/.env.local" "$PAI_ROOT/.env" ".env.local" ".env"; do
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
  read -rs key; echo
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

echo "==> paisetup"
uv run paisetup || true

echo
echo "PAI installed. Runtime root: $PAI_ROOT"
